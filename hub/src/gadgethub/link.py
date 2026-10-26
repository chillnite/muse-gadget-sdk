# SPDX-License-Identifier: Apache-2.0

"""The hub's side of one gadget's Noise session.

The gadget opens a WebSocket to ``/v1/noise``, runs the Noise XX handshake as
initiator, then sends HTTP-like requests as service frames over it:

* ``POST /link-control``: a long-lived stream of length-prefixed JSON. The
  gadget sends ``link.register`` (its commands), ``link.result`` for each
  invoke, and heartbeats. The hub sends the register reply and
  ``link.invoke`` requests.
* ``POST /chat/stream``: a message from the gadget to Claude. The hub answers
  with the acknowledgement at once and runs the turn in the background.
* ``POST /chat/subscribe``: an NDJSON stream of chat events.

Anything else gets a 404, which the gadgets treat as a missing feature (the
home-network tunnel, text-to-speech).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from musegadget.link_client import MessageDecoder, encode_message, printable
from musegadget.noise import (
    ApplicationResponse,
    BodyChunk,
    Header,
    NoiseFrameDecoder,
    NoiseXXResponder,
    ServiceFrame,
    encode_noise_frames,
)
from musegadget.noise.transport import decode_request_envelope, encode_response_envelope

from gadgethub.store import Device

log = logging.getLogger(__name__)

CONTROL_PATH = "/link-control"
CHAT_PATH = "/chat/stream"
SUBSCRIBE_PATH = "/chat/subscribe"
DEFAULT_TIMEOUT_MS = 30_000
MAX_REQUEST_BODY = 4 * 1024 * 1024
HANDSHAKE_TIMEOUT_S = 20


class WebSocket(Protocol):
    async def send_bytes(self, data: bytes) -> None: ...
    async def receive_bytes(self) -> bytes: ...


class LinkClosed(ConnectionError):
    pass


@dataclass
class _Stream:
    stream_id: int
    path: str
    body: bytearray = field(default_factory=bytearray)


class DeviceLink:
    """One connected gadget. ``invoke`` runs one of its commands."""

    def __init__(
        self,
        ws: WebSocket,
        device: Device,
        *,
        on_chat: Callable[[DeviceLink, dict], Awaitable[dict]],
        subscribe: Callable[[str | None], asyncio.Queue],
        unsubscribe: Callable[[asyncio.Queue], None],
    ) -> None:
        self.ws = ws
        self.device = device
        self.commands: dict = {}
        self.display_name = device.name
        self.registered = asyncio.Event()
        self._on_chat = on_chat
        self._subscribe = subscribe
        self._unsubscribe = unsubscribe
        self._send_lock = asyncio.Lock()
        self._decoder = NoiseFrameDecoder()
        self._streams: dict[int, _Stream] = {}
        self._control_id: int | None = None
        self._control = MessageDecoder()
        self._pending: dict[str, asyncio.Future] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._background: set[asyncio.Task] = set()
        self._closed = False

    @property
    def node_id(self) -> str:
        return self.device.node_id

    # -- Session ----------------------------------------------------------------

    async def run(self) -> None:
        """Serve the session until the gadget disconnects."""
        try:
            await asyncio.wait_for(self._handshake(), HANDSHAKE_TIMEOUT_S)
            log.info("%s: Noise session established", self.node_id)
            while True:
                plain = self._recv.decrypt_with_ad(b"", await self.ws.receive_bytes())
                assembled = self._decoder.decode(plain)
                if assembled is not None:
                    await self._on_frame(decode_request_envelope(assembled))
        finally:
            self._closed = True
            self.registered.clear()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(LinkClosed("the gadget disconnected"))
            for task in [*self._tasks.values(), *self._background]:
                task.cancel()

    async def _handshake(self) -> None:
        responder = NoiseXXResponder()
        responder.initialize()
        await self.ws.send_bytes(responder.read_message1_and_write_message2(await self.ws.receive_bytes()))
        responder.read_message3(await self.ws.receive_bytes())
        self._send, self._recv = responder.split()

    async def _on_frame(self, frame: ServiceFrame) -> None:
        sid = frame.stream_id
        if frame.kind == "request":
            stream = self._streams[sid] = _Stream(sid, frame.value.path.split("?", 1)[0])
            if stream.path == CONTROL_PATH and frame.value.verb == "POST":
                self._control_id = sid
                await self._respond(sid, 200, end=False)
            data, ended = frame.value.body, frame.value.end_body
        elif frame.kind == "body_chunk":
            stream = self._streams.get(sid)
            if stream is None:
                return
            data, ended = frame.value.data, frame.value.end_body
        elif frame.kind == "reset":
            self._streams.pop(sid, None)
            task = self._tasks.pop(sid, None)
            if task is not None:
                task.cancel()
            return
        else:
            return

        if sid == self._control_id:
            for message in self._control.feed(data):
                await self._on_control(message)
            return
        stream.body += data
        if len(stream.body) > MAX_REQUEST_BODY:
            self._streams.pop(sid, None)
            await self._respond(sid, 413, b'{"error":"request too large"}')
            return
        if ended:
            self._streams.pop(sid, None)
            self._start(sid, self._serve(stream))

    def _start(self, stream_id: int, coroutine) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks[stream_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(stream_id, None))

    # -- Control stream ---------------------------------------------------------

    async def _on_control(self, message: dict) -> None:
        method = message.get("method")
        if method == "link.register":
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            commands = params.get("commands_v2") or params.get("commands") or {}
            self.commands = commands if isinstance(commands, dict) else {}
            # The name given at pairing wins over the one the gadget reports.
            if self.device.name == self.device.node_id and params.get("display_name"):
                self.display_name = str(params["display_name"])[:64]
            await self.send_control({"type": "res", "id": message.get("id"), "ok": True,
                                     "result": {"status": "registered"}})
            self.registered.set()
            log.info("%s: registered with %d commands", self.node_id, len(self.commands))
        elif method == "link.result":
            future = self._pending.pop(str(message.get("id")), None)
            if future is not None and not future.done():
                future.set_result(message)

    async def send_control(self, message: dict) -> None:
        if self._control_id is None:
            raise LinkClosed("the gadget has no control stream")
        await self._send_frame(ServiceFrame.body_chunk(
            self._control_id, BodyChunk(data=encode_message(message))))

    async def invoke(self, command: str, params: dict, timeout_ms: int | None = None) -> dict:
        """Run one of the gadget's commands; returns its ``link.result``."""
        if self._closed:
            raise LinkClosed("the gadget is not connected")
        spec = self.commands.get(command) if isinstance(self.commands.get(command), dict) else {}
        timeout_ms = timeout_ms or spec.get("timeout_ms") or DEFAULT_TIMEOUT_MS
        invoke_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self._pending[invoke_id] = future
        log.info("%s: invoke %s", self.node_id, printable(command))
        try:
            await self.send_control({"method": "link.invoke", "id": invoke_id, "command": command,
                                     "params": params, "timeout_ms": timeout_ms})
            return await asyncio.wait_for(future, timeout_ms / 1000 + 5)
        finally:
            self._pending.pop(invoke_id, None)

    async def send_unpaired(self) -> None:
        await self.send_control({"type": "event", "event": "link.unpaired"})

    # -- Requests ---------------------------------------------------------------

    async def _serve(self, stream: _Stream) -> None:
        try:
            if stream.path == CHAT_PATH:
                await self._serve_chat(stream)
            elif stream.path == SUBSCRIBE_PATH:
                await self._serve_subscribe(stream)
            else:
                await self._respond(stream.stream_id, 404, b'{"error":"not supported by this hub"}')
        except (asyncio.CancelledError, LinkClosed):
            raise
        except Exception:
            log.exception("%s: %s failed", self.node_id, stream.path)
            await self._respond(stream.stream_id, 500, b'{"error":"internal error"}')

    async def _serve_chat(self, stream: _Stream) -> None:
        try:
            body = json.loads(stream.body or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            await self._respond(stream.stream_id, 400, b'{"error":"expected a JSON object"}')
            return
        ack = await self._on_chat(self, body)
        await self._respond(stream.stream_id, 200, json.dumps(ack).encode(),
                            headers=[Header("Content-Type", "application/json")])

    async def _serve_subscribe(self, stream: _Stream) -> None:
        try:
            body = json.loads(stream.body or b"{}")
        except ValueError:
            body = {}
        session_id = body.get("session_id") if isinstance(body, dict) else None
        queue = self._subscribe(session_id if isinstance(session_id, str) else None)
        try:
            await self._respond(stream.stream_id, 200, end=False,
                                headers=[Header("Content-Type", "application/x-ndjson")])
            while True:
                event = await queue.get()
                line = json.dumps(event, separators=(",", ":")).encode() + b"\n"
                await self._send_frame(ServiceFrame.body_chunk(stream.stream_id, BodyChunk(data=line)))
        finally:
            self._unsubscribe(queue)

    # -- Sending ----------------------------------------------------------------

    async def _respond(self, stream_id: int, status: int, body: bytes = b"", *, end: bool = True,
                       headers: list[Header] | None = None) -> None:
        await self._send_frame(ServiceFrame.response(stream_id, ApplicationResponse(
            status=status, headers=headers or [], body=body, end_body=end)))

    async def _send_frame(self, frame: ServiceFrame) -> None:
        # Encrypt under the lock so frames reach the gadget in nonce order.
        async with self._send_lock:
            if self._closed:
                raise LinkClosed("the gadget disconnected")
            for chunk in encode_noise_frames(encode_response_envelope(frame)):
                await self.ws.send_bytes(self._send.encrypt_with_ad(b"", chunk))


def voice_items(body: dict) -> list[bytes]:
    """The audio attachments of a chat request, decoded."""
    items = body.get("items")
    if not isinstance(items, list):
        return []
    audio = []
    for item in items:
        if (isinstance(item, dict) and str(item.get("mime_type", "")).startswith("audio/")
                and isinstance(item.get("data_base64"), str)):
            try:
                audio.append(base64.b64decode(item["data_base64"], validate=True))
            except ValueError:
                continue
    return audio
