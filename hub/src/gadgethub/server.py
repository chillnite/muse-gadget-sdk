# SPDX-License-Identifier: Apache-2.0

"""The hub's HTTP server.

Gadget-facing, the same API the gadgets already call:

* ``GET /fetch_vms``: one "VM", this hub, with a short-lived bearer for it.
* ``POST /device_token/refresh``: rotates a gadget's tokens.
* ``GET /v1/noise``: the WebSocket a gadget's Noise session runs on.
* ``POST /hub/pair``: pairs a gadget with a one-time code (``musegadget pair-hub``).

Admin-facing, with the admin token as bearer: ``/api/pair-codes``,
``/api/devices``, ``/api/chat`` and ``/api/events``. ``/`` serves the web chat.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
from importlib import resources
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from gadgethub.chat import Chat
from gadgethub.link import DeviceLink, LinkClosed, voice_items
from gadgethub.store import Store

log = logging.getLogger(__name__)

VM_ID = "hub"
_NODE_ID = re.compile(r"[A-Za-z0-9:._-]{1,128}")
_SESSION_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
MAX_WS_MESSAGE = 1024 * 1024
MAX_MESSAGE_CHARS = 32_000
VOICE_REPLY = ("This hub can't understand voice notes yet. Send me a text message, "
               "or ask in the hub's web chat.")
_TERMINAL = {"completed", "failed"}


class Hub:
    def __init__(self, store: Store, chat: Chat, public_url: str) -> None:
        self.store = store
        self.chat = chat
        self.public_url = public_url.rstrip("/")
        self.links: dict[str, DeviceLink] = {}

    @property
    def noise_host(self) -> str:
        return urlsplit(self.public_url).netloc

    def gadgets(self) -> list[DeviceLink]:
        return [link for link in self.links.values() if link.registered.is_set()]

    # -- Gadget-facing ----------------------------------------------------------

    async def fetch_vms(self, request: web.Request) -> web.Response:
        device = self.store.device_for_access_token(_bearer(request))
        if device is None:
            return _json({"error_title": "unauthorized"}, 401)
        return _json({"vm_list": [{
            "vm_id": VM_ID,
            "vm_name": "Claude hub",
            "vm_ws_url": f"wss://{self.noise_host}/v1/noise",
            "vm_auth_token": self.store.new_vm_token(device.node_id),
            "default": True,
        }]})

    async def refresh_token(self, request: web.Request) -> web.Response:
        body = await _body(request)
        node_id = body.get("device_id") if isinstance(body.get("device_id"), str) else ""
        tokens = self.store.refresh(_bearer(request), node_id)
        if tokens is None:
            return _json({"error": "refresh token rejected"}, 401)
        return _json({"access_token": tokens.access_token, "refresh_token": tokens.refresh_token})

    async def pair(self, request: web.Request) -> web.Response:
        body = await _body(request)
        code, node_id = body.get("code"), body.get("node_id")
        name = body.get("display_name") if isinstance(body.get("display_name"), str) else ""
        if not isinstance(code, str) or not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id):
            return _json({"error": "expected code and node_id"}, 400)
        tokens = self.store.pair(code, node_id, name[:64])
        if tokens is None:
            return _json({"error": "unknown or expired pairing code"}, 403)
        log.info("paired %s (%s)", node_id, name or "no name")
        return _json({"access_token": tokens.access_token, "refresh_token": tokens.refresh_token,
                      "api_url": self.public_url, "noise_host": self.noise_host})

    async def noise(self, request: web.Request) -> web.StreamResponse:
        device = self.store.device_for_vm_token(_bearer(request))
        if device is None:
            return web.Response(status=401)
        ws = web.WebSocketResponse(max_msg_size=MAX_WS_MESSAGE)
        await ws.prepare(request)
        link = DeviceLink(_Socket(ws), device, on_chat=self._gadget_chat,
                          subscribe=self.chat.subscribe, unsubscribe=self.chat.unsubscribe)
        previous = self.links.get(device.node_id)
        if previous is not None:
            await previous.ws.close()
        self.links[device.node_id] = link
        log.info("%s connected", device.node_id)
        try:
            await link.run()
        except (LinkClosed, ConnectionError, asyncio.TimeoutError) as exc:
            log.info("%s disconnected: %s", device.node_id, exc or type(exc).__name__)
        except Exception:
            log.exception("%s: session failed", device.node_id)
        finally:
            if self.links.get(device.node_id) is link:
                del self.links[device.node_id]
            await ws.close()
        return ws

    async def _gadget_chat(self, link: DeviceLink, body: dict) -> dict:
        text = body.get("message") if isinstance(body.get("message"), str) else ""
        session_id = body.get("session_id")
        if not (isinstance(session_id, str) and _SESSION_ID.fullmatch(session_id)):
            session_id = None
        origin = f"gadget:{link.node_id}"
        if voice_items(body) and not text.strip():
            return self.chat.answer_directly("(voice note)", VOICE_REPLY,
                                             session_id=session_id, origin=origin)
        prefix = f"[Message from the gadget {link.display_name!r} ({link.node_id})]\n"
        return self.chat.submit(text[:MAX_MESSAGE_CHARS], session_id=session_id,
                                origin=origin, prefix=prefix)

    # -- Admin-facing -----------------------------------------------------------

    def _admin(self, request: web.Request) -> None:
        if not _same(_bearer(request), self.store.admin_token()):
            raise web.HTTPUnauthorized(text='{"error":"admin token required"}',
                                       content_type="application/json")

    async def pair_code(self, request: web.Request) -> web.Response:
        self._admin(request)
        return _json({"code": self.store.new_pair_code(), "hub_url": self.public_url})

    async def devices(self, request: web.Request) -> web.Response:
        self._admin(request)
        return _json({"devices": [{
            "node_id": d.node_id,
            "name": self.links[d.node_id].display_name if d.node_id in self.links else d.name,
            "connected": d.node_id in self.links,
            "commands": sorted(self.links[d.node_id].commands) if d.node_id in self.links else [],
        } for d in sorted(self.store.devices.values(), key=lambda d: d.node_id)]})

    async def remove_device(self, request: web.Request) -> web.Response:
        self._admin(request)
        node_id = request.match_info["node_id"]
        link = self.links.get(node_id)
        if not self.store.unpair(node_id):
            return _json({"error": "no such device"}, 404)
        if link is not None:
            try:
                await link.send_unpaired()
            except ConnectionError:
                pass
            await link.ws.close()
        return _json({"ok": True})

    async def send_chat(self, request: web.Request) -> web.StreamResponse:
        """Post a message and stream that turn's events as NDJSON until it ends."""
        self._admin(request)
        body = await _body(request)
        text = body.get("message")
        session_id = body.get("session_id")
        if not isinstance(text, str) or not text.strip():
            return _json({"error": "expected a message"}, 400)
        if session_id is not None and not (isinstance(session_id, str)
                                           and _SESSION_ID.fullmatch(session_id)):
            return _json({"error": "session_id must be letters, digits and dashes"}, 400)
        queue = self.chat.subscribe(session_id or "main")
        try:
            ack = self.chat.submit(text[:MAX_MESSAGE_CHARS], session_id=session_id)
            response = await _ndjson(request)
            await _write(response, {"type": "ack", **ack})
            while True:
                event = await queue.get()
                payload = event["payload"]
                if payload.get("reply_to_message_id") != ack["message_id"]:
                    continue
                await _write(response, event)
                if event["event"] == "task.status" and payload.get("status") in _TERMINAL:
                    return response
        finally:
            self.chat.unsubscribe(queue)

    async def events(self, request: web.Request) -> web.StreamResponse:
        self._admin(request)
        queue = self.chat.subscribe(request.query.get("session_id"))
        try:
            response = await _ndjson(request)
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), 25)
                except asyncio.TimeoutError:
                    event = {"type": "keepalive"}
                await _write(response, event)
        finally:
            self.chat.unsubscribe(queue)

    async def index(self, request: web.Request) -> web.Response:
        page = resources.files("gadgethub").joinpath("static/index.html").read_text("utf-8")
        return web.Response(text=page, content_type="text/html",
                            headers={"Content-Security-Policy": "default-src 'self'; "
                                     "script-src 'unsafe-inline'; style-src 'unsafe-inline'",
                                     "Cache-Control": "no-store"})

    def app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_WS_MESSAGE)
        app.add_routes([
            web.get("/", self.index),
            web.get("/fetch_vms", self.fetch_vms),
            web.post("/device_token/refresh", self.refresh_token),
            web.post("/hub/pair", self.pair),
            web.get("/v1/noise", self.noise),
            web.post("/api/pair-codes", self.pair_code),
            web.get("/api/devices", self.devices),
            web.delete("/api/devices/{node_id}", self.remove_device),
            web.post("/api/chat", self.send_chat),
            web.get("/api/events", self.events),
        ])

        async def shutdown(_app) -> None:
            for link in list(self.links.values()):
                await link.ws.close()
            await self.chat.close()

        app.on_shutdown.append(shutdown)
        return app


class _Socket:
    """An aiohttp WebSocket with the two calls DeviceLink makes."""

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self._ws = ws

    async def send_bytes(self, data: bytes) -> None:
        await self._ws.send_bytes(data)

    async def receive_bytes(self) -> bytes:
        while True:
            message = await self._ws.receive()
            if message.type == WSMsgType.BINARY:
                return message.data
            if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED,
                                WSMsgType.ERROR):
                raise LinkClosed("the WebSocket closed")

    async def close(self) -> None:
        await self._ws.close()


def _bearer(request: web.Request) -> str:
    header = request.headers.get("Authorization", "")
    return header[7:].strip() if header[:7].lower() == "bearer " else ""


def _same(a: str, b: str) -> bool:
    return bool(a) and hmac.compare_digest(a.encode(), b.encode())


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def _json(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


async def _ndjson(request: web.Request) -> web.StreamResponse:
    response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson",
                                           "Cache-Control": "no-store"})
    await response.prepare(request)
    return response


async def _write(response: web.StreamResponse, event: dict) -> None:
    await response.write(json.dumps(event, separators=(",", ":")).encode() + b"\n")
