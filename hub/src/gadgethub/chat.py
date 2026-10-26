# SPDX-License-Identifier: Apache-2.0

"""Conversations with Claude, and the chat events subscribers see.

Each chat (``session_id``; ``"main"`` unless a gadget names a side chat) has
one history, and its turns run one at a time. Events use the shape the
gadgets already read from ``/chat/subscribe``:

* ``message.user``: a message someone sent (gadgets ignore it; the web chat shows it).
* ``task.status``: ``running``, then ``completed`` or ``failed``, for each turn.
* ``agent.status``: progress, such as a command running on a gadget.
* ``delta.message_done``: one of Claude's replies, complete.

Every event names the chat and the user message it answers
(``reply_to_message_id``), so a gadget can tell its own answers apart.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict

import anthropic

log = logging.getLogger(__name__)

MAIN = "main"
SUBSCRIBER_QUEUE = 256


class Chat:
    def __init__(self, agent) -> None:
        self._agent = agent
        self._histories: dict[str, list] = defaultdict(list)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._subscribers: dict[asyncio.Queue, str | None] = {}
        self._tasks: set[asyncio.Task] = set()

    # -- Subscribers ------------------------------------------------------------

    def subscribe(self, session_id: str | None = None) -> asyncio.Queue:
        """Events for one chat, or for every chat when ``session_id`` is None."""
        queue: asyncio.Queue = asyncio.Queue(SUBSCRIBER_QUEUE)
        self._subscribers[queue] = session_id
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.pop(queue, None)

    def _publish(self, name: str, session_id: str, **payload) -> None:
        event = {"type": "event", "event": name, "payload": {"session_id": session_id, **payload}}
        for queue, wanted in list(self._subscribers.items()):
            if wanted is not None and wanted != session_id:
                continue
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("dropping a chat event for a subscriber that fell behind")

    # -- Turns ------------------------------------------------------------------

    def submit(self, text: str, *, session_id: str | None = None, origin: str = "user",
               prefix: str = "") -> dict:
        """Queue a message for Claude; returns the acknowledgement at once.

        ``prefix`` goes in front of the text Claude sees (a gadget's name), and
        isn't shown to subscribers.
        """
        session = session_id or MAIN
        message_id = f"msg_{uuid.uuid4().hex}"
        self._publish("message.user", session, role="user", message_id=message_id,
                      text=text, origin=origin)
        task = asyncio.ensure_future(self._turn(session, message_id, prefix + text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return {"message_id": message_id, "session_id": session, "is_thread": session != MAIN}

    def answer_directly(self, text: str, reply: str, *, session_id: str | None = None,
                        origin: str = "user") -> dict:
        """Acknowledge a message and answer it without asking Claude."""
        session = session_id or MAIN
        message_id = f"msg_{uuid.uuid4().hex}"
        task_id = f"task_{uuid.uuid4().hex}"
        self._publish("message.user", session, role="user", message_id=message_id,
                      text=text, origin=origin)
        self._publish("delta.message_done", session, role="assistant",
                      message_id=f"msg_{uuid.uuid4().hex}", display_text=reply,
                      reply_to_message_id=message_id)
        self._publish("task.status", session, task_id=task_id, status="completed",
                      reply_to_message_id=message_id)
        return {"message_id": message_id, "session_id": session, "is_thread": session != MAIN}

    async def _turn(self, session: str, message_id: str, text: str) -> None:
        task_id = f"task_{uuid.uuid4().hex}"
        async with self._locks[session]:
            history = self._histories[session]
            history.append({"role": "user", "content": text})
            self._publish("task.status", session, task_id=task_id, status="running",
                          reply_to_message_id=message_id)
            self._publish("agent.status", session, activity_code="thinking",
                          activity_text="Thinking", reply_to_message_id=message_id)

            async def on_text(reply: str) -> None:
                self._publish("delta.message_done", session, role="assistant",
                              message_id=f"msg_{uuid.uuid4().hex}", display_text=reply,
                              reply_to_message_id=message_id)

            async def on_status(status: str) -> None:
                self._publish("agent.status", session, activity_code="executing",
                              activity_text=status, reply_to_message_id=message_id)

            status = "completed"
            try:
                await self._agent.run_turn(history, on_text=on_text, on_status=on_status)
            except anthropic.APIError as exc:
                log.error("Claude request failed: %s", exc)
                await on_text(f"I couldn't reach Claude: {_describe(exc)}")
                status = "failed"
            except Exception:
                log.exception("turn failed")
                await on_text("Something went wrong on the hub; its log has the details.")
                status = "failed"
            self._publish("task.status", session, task_id=task_id, status=status,
                          reply_to_message_id=message_id)
            self._publish("agent.status", session, activity_code="idle", activity_text="",
                          reply_to_message_id=message_id)

    def history_length(self, session_id: str = MAIN) -> int:
        return len(self._histories.get(session_id, []))

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


def _describe(exc: anthropic.APIError) -> str:
    if isinstance(exc, anthropic.AuthenticationError):
        return "the API key was rejected."
    if isinstance(exc, anthropic.RateLimitError):
        return "rate limited; try again shortly."
    if isinstance(exc, anthropic.APIStatusError):
        return f"HTTP {exc.status_code}."
    if isinstance(exc, anthropic.APIConnectionError):
        return "no connection to the API."
    return type(exc).__name__
