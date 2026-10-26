# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

import pytest


def text(value: str):
    return SimpleNamespace(type="text", text=value)


def tool_use(id_: str, name: str, input_: dict):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def reply(*content, stop_reason="end_turn"):
    return SimpleNamespace(content=list(content), stop_reason=stop_reason)


class FakeClaude:
    """Stands in for AsyncAnthropic: plays back scripted replies and records requests.

    A script entry may be a callable taking the request, for replies that depend
    on the tools offered.
    """

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **request):
        # Copy the history: the agent keeps appending to the same list.
        self.requests.append({**request, "messages": list(request["messages"])})
        if not self.script:
            raise AssertionError("Claude was asked more often than the test expected")
        step = self.script.pop(0)
        return step(request) if callable(step) else step


@pytest.fixture
def helpers():
    return SimpleNamespace(text=text, tool_use=tool_use, reply=reply, FakeClaude=FakeClaude)
