# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import re

from gadgethub.agent import Agent, build_tools, tool_name


class Gadget:
    def __init__(self, node_id="homelink-abcdef", commands=None, results=None):
        self.node_id = node_id
        self.display_name = "pi"
        self.commands = commands if commands is not None else {
            "system.run": {"description": "Run a shell command",
                           "required": {"command": {"type": "string", "description": "the command"}},
                           "optional": {"timeout_s": {"type": "integer", "description": "seconds"}}},
        }
        self.results = list(results or [{"ok": True, "payload": {"stdout": "up 3 days\n", "exit_code": 0}}])
        self.calls = []

    async def invoke(self, command, params, timeout_ms=None):
        self.calls.append((command, params))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def run(agent, history):
    texts, statuses = [], []

    async def on_text(t):
        texts.append(t)

    async def on_status(s):
        statuses.append(s)

    asyncio.run(agent.run_turn(history, on_text=on_text, on_status=on_status))
    return texts, statuses


def test_tool_names_are_valid_and_unique():
    pattern = re.compile(r"[A-Za-z0-9_-]{1,64}")
    assert tool_name("homelink-abcdef", "system.run") == "homelink-abcdef__system_run"
    long = [tool_name("x" * 80, "a.b"), tool_name("x" * 80, "a_b")]
    assert all(pattern.fullmatch(n) for n in long) and long[0] != long[1]


def test_tools_follow_the_registered_specs_in_a_stable_order():
    second = Gadget("homelink-zzzzzz", {"b.cmd": {}, "a.cmd": {"description": "A"}})
    tools, routes = build_tools([second, Gadget()])
    assert [t["name"] for t in tools] == [
        "homelink-abcdef__system_run", "homelink-zzzzzz__a_cmd", "homelink-zzzzzz__b_cmd"]
    schema = tools[0]["input_schema"]
    assert schema["required"] == ["command"]
    assert schema["properties"]["timeout_s"] == {"type": "integer", "description": "seconds"}
    assert routes["homelink-zzzzzz__a_cmd"].command == "a.cmd"


def test_a_tool_call_runs_on_the_gadget_and_its_result_goes_back(helpers):
    gadget = Gadget()
    claude = helpers.FakeClaude(
        helpers.reply(helpers.text("Checking."),
                      helpers.tool_use("t1", "homelink-abcdef__system_run", {"command": "uptime"}),
                      stop_reason="tool_use"),
        helpers.reply(helpers.text("It has been up for 3 days.")),
    )
    history = [{"role": "user", "content": "How long has the pi been up?"}]
    texts, statuses = run(Agent(claude, lambda: [gadget]), history)

    assert gadget.calls == [("system.run", {"command": "uptime"})]
    assert texts == ["Checking.", "It has been up for 3 days."]
    assert statuses == ["Running system.run on pi"]
    result = history[2]["content"][0]
    assert result["tool_use_id"] == "t1" and "up 3 days" in result["content"]
    assert "is_error" not in result
    first = claude.requests[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["fallbacks"] == "default" and first["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in first["tools"]] == ["homelink-abcdef__system_run"]


def test_failures_become_error_results(helpers):
    gadget = Gadget(results=[{"ok": False, "error": {"message": "no such file"}},
                             asyncio.TimeoutError(), RuntimeError("boom")])
    claude = helpers.FakeClaude(
        helpers.reply(helpers.tool_use("t1", "homelink-abcdef__system_run", {"command": "a"}),
                      helpers.tool_use("t2", "homelink-abcdef__system_run", {"command": "b"}),
                      helpers.tool_use("t3", "homelink-abcdef__system_run", {"command": "c"}),
                      helpers.tool_use("t4", "gone__system_run", {"command": "d"}),
                      stop_reason="tool_use"),
        helpers.reply(helpers.text("Those failed.")),
    )
    history = [{"role": "user", "content": "go"}]
    run(Agent(claude, lambda: [gadget]), history)
    results = history[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2", "t3", "t4"]
    assert all(r["is_error"] for r in results)
    assert results[0]["content"] == "no such file"
    assert "timed out" in results[1]["content"] and "disconnected" in results[3]["content"]


def test_a_cut_off_tool_call_is_answered_but_not_run(helpers):
    gadget = Gadget()
    claude = helpers.FakeClaude(helpers.reply(
        helpers.tool_use("t1", "homelink-abcdef__system_run", {"command": "rm"}),
        stop_reason="max_tokens"))
    history = [{"role": "user", "content": "go"}]
    run(Agent(claude, lambda: [gadget]), history)
    assert gadget.calls == []
    assert history[-1]["content"][0]["is_error"] is True


def test_no_tools_are_sent_without_gadgets(helpers):
    claude = helpers.FakeClaude(helpers.reply(helpers.text("Hi.")))
    texts, _ = run(Agent(claude, lambda: []), [{"role": "user", "content": "hi"}])
    assert texts == ["Hi."] and "tools" not in claude.requests[0]
