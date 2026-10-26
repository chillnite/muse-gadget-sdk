# SPDX-License-Identifier: Apache-2.0

"""Claude, with every connected gadget's commands as tools.

The tool list is rebuilt for each request from the gadgets connected at that
moment, so this runs its own tool-use loop rather than the SDK's tool runner.
History is append-only: assistant turns go back exactly as the API returned
them, thinking and compaction blocks included.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Protocol

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 16000
MAX_STEPS = 24
MAX_RESULT_CHARS = 100_000
BETAS = ["server-side-fallback-2026-07-01", "compact-2026-01-12"]
_TOOL_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
_JSON_TYPES = {"string", "integer", "number", "boolean", "object", "array"}

SYSTEM_PROMPT = """\
You are Claude, running on a home hub that the user set up themselves. Gadgets \
connect to the hub: ESP32 boards and Linux computers that the user built or \
owns. Each connected gadget's commands are available to you as tools, named \
<gadget id>__<command>, and each tool's description says which gadget it \
belongs to. The set of tools changes as gadgets connect and disconnect.

Messages reach you from the user's hub chat, or from a gadget. A gadget's \
message starts with a line naming the gadget, and its answer may be shown on \
a small screen or read out, so keep those replies short and in plain text, \
without Markdown.

Use the tools when they help with what the user asked. A command that changes \
something in the physical world, deletes data, or runs a shell command with \
side effects should match what the user asked for; if a request is ambiguous \
about something like that, ask first. If a tool fails, say what failed rather \
than guessing at the result."""


class Gadget(Protocol):
    node_id: str
    display_name: str
    commands: dict

    async def invoke(self, command: str, params: dict, timeout_ms: int | None = None) -> dict: ...


@dataclass(frozen=True)
class Route:
    gadget: Gadget
    command: str


def tool_name(node_id: str, command: str) -> str:
    """A tool name the API accepts (letters, digits, _ and -; 64 at most)."""
    name = f"{_TOOL_NAME_UNSAFE.sub('-', node_id)}__{_TOOL_NAME_UNSAFE.sub('_', command)}"
    if len(name) <= 64:
        return name
    digest = hashlib.sha256(f"{node_id}\0{command}".encode()).hexdigest()[:8]
    return f"{name[:55]}_{digest}"


def _property(spec) -> dict:
    spec = spec if isinstance(spec, dict) else {}
    prop: dict = {}
    if spec.get("type") in _JSON_TYPES:
        prop["type"] = spec["type"]
    if isinstance(spec.get("description"), str):
        prop["description"] = spec["description"]
    return prop


def build_tools(gadgets: Iterable[Gadget]) -> tuple[list[dict], dict[str, Route]]:
    """Tool definitions for the gadgets' registered commands, in a stable order
    so the prompt cache keeps hitting while the same gadgets stay connected."""
    tools, routes = [], {}
    for gadget in sorted(gadgets, key=lambda g: g.node_id):
        for command in sorted(gadget.commands):
            spec = gadget.commands[command] if isinstance(gadget.commands[command], dict) else {}
            required = spec.get("required") if isinstance(spec.get("required"), dict) else {}
            optional = spec.get("optional") if isinstance(spec.get("optional"), dict) else {}
            name = tool_name(gadget.node_id, command)
            routes[name] = Route(gadget, command)
            tools.append({
                "name": name,
                "description": (f"Gadget {gadget.display_name!r} ({gadget.node_id}), command "
                                f"{command}: {spec.get('description') or 'no description given'}"),
                "input_schema": {
                    "type": "object",
                    "properties": {k: _property(v) for k, v in {**optional, **required}.items()},
                    "required": sorted(required),
                },
            })
    return tools, routes


StatusCallback = Callable[[str], Awaitable[None]]
TextCallback = Callable[[str], Awaitable[None]]


class Agent:
    def __init__(self, client, gadgets: Callable[[], Iterable[Gadget]], *,
                 model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT) -> None:
        self._client = client
        self._gadgets = gadgets
        self.model = model
        self.effort = effort

    async def run_turn(self, history: list, *, on_text: TextCallback,
                       on_status: StatusCallback) -> None:
        """Answer the last user message in ``history``, appending to it."""
        for _ in range(MAX_STEPS):
            tools, routes = build_tools(self._gadgets())
            request = {
                "model": self.model,
                "max_tokens": MAX_TOKENS,
                "system": SYSTEM_PROMPT,
                "messages": history,
                "output_config": {"effort": self.effort},
                "cache_control": {"type": "ephemeral"},
                "betas": BETAS,
                # On a safety decline, the API retries the request on a fallback model.
                "fallbacks": "default",
                "context_management": {"edits": [{"type": "compact_20260112"}]},
            }
            if tools:
                request["tools"] = tools
            response = await self._client.beta.messages.create(**request)
            history.append({"role": "assistant", "content": response.content})

            text = "\n".join(b.text for b in response.content if b.type == "text").strip()
            if text:
                await on_text(text)
            if response.stop_reason == "pause_turn":
                continue
            uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use":
                # A tool call cut off at max_tokens, or in a refused reply, is never
                # run, but it still needs a result for the history to stay valid.
                if uses:
                    history.append({"role": "user", "content": [
                        _error(use.id, "Not run: the reply was cut off.") for use in uses]})
                if response.stop_reason == "refusal" and not text:
                    await on_text("I can't help with that request.")
                return

            results = await asyncio.gather(*(self._call(use, routes, on_status) for use in uses))
            history.append({"role": "user", "content": list(results)})
        await on_text("I stopped there: that took more steps than I'm allowed in one turn.")

    async def _call(self, use, routes: dict[str, Route], on_status: StatusCallback) -> dict:
        route = routes.get(use.name)
        if route is None:
            return _error(use.id, f"{use.name} is not available: its gadget disconnected.")
        params = use.input if isinstance(use.input, dict) else {}
        await on_status(f"Running {route.command} on {route.gadget.display_name}")
        try:
            result = await route.gadget.invoke(route.command, params)
        except asyncio.TimeoutError:
            return _error(use.id, f"{route.command} timed out on {route.gadget.display_name}.")
        except ConnectionError as exc:
            return _error(use.id, f"{route.gadget.display_name} disconnected: {exc}")
        except Exception as exc:
            # Every tool_use needs a result, or the next request is rejected.
            log.exception("invoking %s failed", route.command)
            return _error(use.id, f"The hub failed to run {route.command}: {type(exc).__name__}")
        if result.get("ok"):
            payload = result.get("payload")
            if payload is None and isinstance(result.get("payload_json"), str):
                payload = result["payload_json"]
            content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            return {"type": "tool_result", "tool_use_id": use.id,
                    "content": _clip(content or "ok")}
        error = result.get("error")
        message = error.get("message") if isinstance(error, dict) else error
        return _error(use.id, str(message or "the command failed"))


def _error(tool_use_id: str, message: str) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": _clip(message),
            "is_error": True}


def _clip(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + f"\n[cut: {len(text) - MAX_RESULT_CHARS} more characters]"
