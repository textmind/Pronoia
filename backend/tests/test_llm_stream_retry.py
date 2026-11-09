from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from httpx import RemoteProtocolError

from app import llm


class _BrokenStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise RemoteProtocolError("incomplete chunked read")


class _CompletedStream:
    def __init__(self):
        self.sent = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        return SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(
                    content="重试成功",
                    reasoning_content=None,
                    tool_calls=None,
                ),
                finish_reason="stop",
            )]
        )


class _Completions:
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        return _BrokenStream() if self.calls == 1 else _CompletedStream()


class LlmStreamRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_timeout_still_closes_stream(self):
        closed = []
        class Stream(_CompletedStream):
            async def close(self):
                closed.append(True)
        target = llm.LLMRuntimeTarget("https://fixture.invalid/v1", "synthetic", "test", 0)
        with patch.object(llm, "resolve_runtime_target", return_value=target):
            chunks = [chunk async for chunk in llm._stream_with_hard_timeout(Stream())]
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].choices[0].delta.content, "重试成功")
        self.assertEqual(closed, [True])

    async def test_remote_protocol_error_retries_once(self):
        completions = _Completions()
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        seen_tool_scopes = []

        def fake_tools(scope):
            seen_tool_scopes.append(scope)
            return []

        state = {"content": "", "tool_trace": [], "rounds": 0}
        with (
            patch.object(llm, "get_client", return_value=client),
            patch.object(llm, "tools_for_agent", side_effect=fake_tools),
        ):
            events = [
                event
                async for event in llm.run_agent(
                    "predictor",
                    [{"role": "user", "content": "test"}],
                    agent_def={"skills": []},
                    state=state,
                    max_rounds=1,
                    emit_thinking=False,
                )
            ]

        self.assertEqual(completions.calls, 2)
        self.assertEqual(seen_tool_scopes, [[]])
        self.assertEqual(state["content"], "重试成功")
        self.assertEqual([e["delta"] for e in events if e["type"] == "token"], ["重试成功"])
        self.assertEqual(events[-1]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
