# SPDX-License-Identifier: Apache-2.0

"""The real Linux gadget client against the real hub server, with a scripted Claude."""

from __future__ import annotations

import asyncio
import json

import aiohttp
from aiohttp.test_utils import TestServer

from musegadget.link_client import DeviceDescription, LinkSession
from musegadget.muse_turn import MuseTurn, Reply, TaskFinished

from gadgethub.agent import Agent
from gadgethub.chat import Chat
from gadgethub.server import Hub
from gadgethub.store import Store

NODE_ID = "homelink-abcdef"
COMMANDS = {"system.run": {
    "description": "Run a shell command",
    "required": {"command": {"type": "string", "description": "the command"}},
    "optional": {},
}}


class ClientSocket:
    """An aiohttp client WebSocket with the calls LinkSession makes."""

    def __init__(self, ws) -> None:
        self._ws = ws

    async def send(self, data: bytes) -> None:
        await self._ws.send_bytes(data)

    async def recv(self) -> bytes:
        message = await self._ws.receive()
        if message.type != aiohttp.WSMsgType.BINARY:
            raise ConnectionError("closed")
        return message.data

    async def close(self) -> None:
        await self._ws.close()


async def start(tmp_path, claude):
    hub = None
    agent = Agent(claude, lambda: hub.gadgets())
    hub = Hub(Store.load(tmp_path), Chat(agent), "https://hub.test")
    server = TestServer(hub.app())
    await server.start_server()
    return hub, server, str(server.make_url("")).rstrip("/")


async def lines(response) -> list[dict]:
    return [json.loads(line) async for line in response.content if line.strip()]


def test_pair_connect_and_let_claude_run_a_command(tmp_path, helpers):
    ran = []

    def run_command(command, params, timeout_ms):
        ran.append((command, params, timeout_ms))
        return {"ok": True, "payload": {"stdout": "up 3 days\n", "exit_code": 0}}

    claude = helpers.FakeClaude(
        helpers.reply(helpers.tool_use("t1", f"{NODE_ID}__system_run", {"command": "uptime"}),
                      stop_reason="tool_use"),
        helpers.reply(helpers.text("Up for 3 days.")),
        helpers.reply(helpers.text("Hello, pi.")),
    )

    async def scenario():
        hub, server, base = await start(tmp_path, claude)
        admin = {"Authorization": f"Bearer {hub.store.admin_token()}"}
        async with aiohttp.ClientSession() as http:
            async def get(path, **kw):
                async with http.get(base + path, **kw) as r:
                    return r.status, await r.json()

            async def post(path, **kw):
                async with http.post(base + path, **kw) as r:
                    return r.status, await r.json()

            assert (await post("/api/pair-codes"))[0] == 401
            _, made = await post("/api/pair-codes", headers=admin)
            status, paired = await post("/hub/pair", json={
                "code": made["code"], "node_id": NODE_ID, "display_name": "pi"})
            assert status == 200 and paired["noise_host"] == "hub.test"
            assert (await post("/hub/pair", json={"code": made["code"], "node_id": NODE_ID}))[0] == 403

            assert (await get("/fetch_vms", headers={"Authorization": "Bearer nope"}))[0] == 401
            _, vms = await get("/fetch_vms", headers={"Authorization": f"Bearer {paired['access_token']}"})
            vm = vms["vm_list"][0]
            assert vm["default"] and vm["vm_ws_url"] == "wss://hub.test/v1/noise"

            async with http.get(base + "/v1/noise", headers={"Authorization": "Bearer nope"}) as r:
                assert r.status == 401

            async def connect(url, headers):
                assert url.startswith("wss://hub.test/v1/noise?vm_id=")
                return ClientSocket(await http.ws_connect(base + "/v1/noise", headers=headers))

            session = LinkSession(
                noise_host=paired["noise_host"], vm_id=vm["vm_id"], vm_auth_token=vm["vm_auth_token"],
                device=DeviceDescription(node_id=NODE_ID, display_name="raspberrypi", version="0.1.0",
                                         commands=COMMANDS),
                run_command=run_command, connect=connect,
            )
            stop = asyncio.Event()
            task = asyncio.ensure_future(session.run(stop))
            await asyncio.wait_for(session.registered.wait(), 5)
            _, devices = await get("/api/devices", headers=admin)
            # The name given at pairing wins over the hostname the gadget reports.
            assert devices["devices"] == [{"node_id": NODE_ID, "name": "pi", "connected": True,
                                           "commands": ["system.run"]}]

            # The user asks in the hub chat; Claude runs a command on the gadget.
            async with http.post(base + "/api/chat", headers=admin,
                                 json={"message": "How long has the pi been up?"}) as r:
                events = await lines(r)
            assert ran == [("system.run", {"command": "uptime"}, 30000)]
            replies = [e["payload"]["display_text"] for e in events if e.get("event") == "delta.message_done"]
            assert replies == ["Up for 3 days."]
            assert events[-1]["payload"]["status"] == "completed"

            # The gadget messages Claude and follows the answer the way the client does.
            received = []

            async def follow():
                async for event in session.subscribe_chat():
                    received.append(event)

            follower = asyncio.ensure_future(follow())
            await asyncio.wait_for(session.chat_subscribed.wait(), 5)
            ack = await session.send_chat("hello from the pi")
            assert ack["ok"]
            turn = MuseTurn()
            seen = turn.acknowledge(ack["response"])
            deadline = asyncio.get_running_loop().time() + 5
            while not any(isinstance(e, TaskFinished) for e in seen):
                assert asyncio.get_running_loop().time() < deadline
                while received:
                    seen += turn.feed(received.pop(0))
                await asyncio.sleep(0.01)
            assert [e.text for e in seen if isinstance(e, Reply) and e.done] == ["Hello, pi."]
            assert claude.requests[-1]["messages"][-1]["content"].startswith(
                f"[Message from the gadget 'pi' ({NODE_ID})]\n")

            # Unpairing tells the gadget, which then stops.
            async with http.delete(f"{base}/api/devices/{NODE_ID}", headers=admin) as r:
                assert r.status == 200
            outcome = await asyncio.wait_for(task, 5)
            assert outcome.value == "unpaired"
            follower.cancel()
            await asyncio.gather(follower, return_exceptions=True)
        await server.close()

    asyncio.run(scenario())


def test_a_voice_note_gets_an_answer_without_claude(tmp_path, helpers):
    claude = helpers.FakeClaude()

    async def scenario():
        hub, server, _ = await start(tmp_path, claude)
        queue = hub.chat.subscribe()

        class Link:
            node_id, display_name = NODE_ID, "pi"

        ack = await hub._gadget_chat(Link(), {"message": "", "items": [
            {"type": "file", "mime_type": "audio/wav", "data_base64": "UklGRg=="}]})
        events = [queue.get_nowait() for _ in range(queue.qsize())]
        assert [e["event"] for e in events] == ["message.user", "delta.message_done", "task.status"]
        assert events[1]["payload"]["reply_to_message_id"] == ack["message_id"]
        await server.close()

    asyncio.run(scenario())
    assert claude.requests == []
