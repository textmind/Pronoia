"""Reasoning/body interleaving and durable recovery without provider requests."""
import asyncio
from types import SimpleNamespace as NS

import httpx
import pytest

from .test_direct_model_credentials import direct_client
from .test_research_compatibility import verified_profile


def chunk(text=None, reasoning=None, tools=None, finish=None):
    return NS(choices=[NS(delta=NS(content=text, reasoning_content=reasoning, tool_calls=tools), finish_reason=finish)])


@pytest.mark.parametrize("summary", [False, True])
def test_reasoning_after_body_is_immediate_and_rounds_are_unique(monkeypatch, summary):
    from app import llm
    target = llm.LLMRuntimeTarget("https://fixture.invalid/v1", "synthetic", "test", 30)
    monkeypatch.setattr(llm, "resolve_runtime_target", lambda: target)
    monkeypatch.setattr(llm, "tools_for_agent", lambda _: [])
    tool = NS(index=0, id="tool-1", function=NS(name="not_exposed", arguments="{}"))
    async def create(**_):
        async def stream():
            yield chunk(reasoning="先检查")
            yield chunk(text="**结")
            yield chunk(reasoning="再核对")
            yield chunk(text="论：**完成")
            if summary and "tools" not in _ and "工具轮次已用完" not in str(_["messages"]):
                yield chunk(tools=[tool], finish="tool_calls")
            else:
                yield chunk(finish="stop")
        return stream()
    monkeypatch.setattr(llm, "get_client", lambda: NS(chat=NS(completions=NS(create=create))))

    async def scenario():
        all_ids = []
        for _ in range(2):
            state = {"content": "", "tool_trace": []}
            events = []
            async for event in llm.run_agent("router", [], agent_def={"skills": []}, state=state, max_rounds=1):
                events.append(event)
                if event["type"] == "thinking" and event["delta"] == "再核对":
                    # The later reasoning is visible while the body is incomplete.
                    assert state["content"].endswith("**结")
            expected_rounds = 2 if summary else 1
            ended = [e for e in events if e["type"] == "model_round"]
            assert len(ended) == expected_rounds
            assert [e["round"] for e in ended] == list(range(1, expected_rounds + 1))
            for end in ended:
                channel = [e for e in events if e.get("round_id") == end["round_id"] and e["type"] == "thinking"]
                assert "".join(e["delta"] for e in channel) == "先检查再核对"
            all_ids.extend(e["round_id"] for e in ended)
        assert len(all_ids) == len(set(all_ids))
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["auto", "agent", "team"])
def test_round_channels_survive_refresh_and_cancellation(direct_client, monkeypatch, mode):
    from app import db, llm
    from app.routes import chat
    verified_profile(direct_client)
    monkeypatch.setattr(chat, "resolve_runtime_target", lambda: llm.LLMRuntimeTarget("https://fixture.invalid/v1", "synthetic", "test", 30))

    async def scenario():
        started = asyncio.Event()
        async def events(state):
            for kind, delta in [("thinking", "思考一"), ("token", "**结"), ("thinking", "思考二"), ("token", "论**")]:
                if kind == "token": state["content"] += delta
                yield {"type": kind, "agent": "router", "round_id": "call:1", "round": 1, "delta": delta}
            # Force a checkpoint as tool results do during an ordinary run.
            yield {"type": "agent_step", "phase": "progress", "note": "已保存阶段"}
            started.set()
            await asyncio.Event().wait()
        async def agent(*_a, state, **_k):
            async for e in events(state): yield e
        async def team(_q, _h, state, *_a, **_k):
            async for e in events(state): yield e
        monkeypatch.setattr(chat, "run_agent", agent)
        monkeypatch.setattr(chat, "run_team", team)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=direct_client.app), base_url="http://test") as client:
            case = db.create_case()
            response = await client.post("/api/chat?detach=true", json={"case_id": case["id"], "message": "恢复思考测试", "mode": mode})
            assert response.status_code == 200, response.text
            await asyncio.wait_for(started.wait(), 2)
            # A burst followed by a silent provider must still be recoverable
            # before cancellation or the next token triggers another checkpoint.
            await asyncio.sleep(0.85)
            live = (await client.get(f"/api/cases/{case['id']}")).json()
            saved = next(e for e in live["messages"][-1]["tool_trace"] if e["type"] == "model_round")
            assert saved["reasoning"] == "思考一思考二"
            assert saved["text"] == "**结论**"
            assert saved["status"] == "streaming" and live["generating"]
            await client.post("/api/chat/cancel", json={"case_id": case["id"], "run_id": response.json()["run_id"]})
            detail = (await client.get(f"/api/cases/{case['id']}")).json()
            message = detail["messages"][-1]
            rounds = [e for e in message["tool_trace"] if e["type"] == "model_round"]
            assert len(rounds) == 1
            assert rounds[0]["text"] == message["content"] == "**结论**"
            assert rounds[0]["reasoning"] == "思考一思考二"
            assert rounds[0]["status"] == "interrupted"
            assert not detail["generating"]
    asyncio.run(scenario())


def test_team_review_correction_forwards_provider_thinking(monkeypatch):
    from app.agents import team
    async def complete(system, user, **_):
        if "分析草稿" in user: return {"verdict": "revise", "issues": ["需要修正"], "corrected": "修正建议"}
        return {"tasks": []}
    async def expert(agent_id, *_a, **_k):
        yield {"type": "agent_findings", "agent": agent_id, "findings": "证据", "tool_trace": []}
    async def agent(agent_id, _messages, *, state, emit_thinking=True, **_):
        assert emit_thinking
        yield {"type": "thinking", "agent": agent_id, "delta": "真实接口片段", "round_id": "fix:1"}
        state["content"] += "正文"
        yield {"type": "token", "agent": agent_id, "delta": "正文", "round_id": "fix:1"}
    monkeypatch.setattr(team, "complete_json", complete)
    monkeypatch.setattr(team, "_run_expert_serial", expert)
    monkeypatch.setattr(team, "run_agent", agent)
    async def scenario():
        async def artifact_store(kind, title, payload):
            return {"id": "fixture", "kind": kind, "title": title, "payload": payload}
        events = [e async for e in team.run_team("测试问题", [], {"content": "", "tool_trace": []}, artifact_store, skip_hypothesis=True)]
        header = next(i for i, e in enumerate(events) if e.get("delta") == "\n\n## 复核修正\n")
        assert any(e["type"] == "thinking" and e["delta"] == "真实接口片段" for e in events[header + 1:])
        assert not any(e["type"] == "thinking" and e.get("delta", "").startswith("正在") for e in events)
    asyncio.run(scenario())
