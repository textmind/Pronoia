"""Research navigation protocol and provider compatibility; no real model calls."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from .test_direct_model_credentials import direct_client, create_direct


def verified_profile(client):
    from app import settings
    from app.model_lab import repository
    saved = create_direct(client)
    profile = repository.get_profile(saved["id"], public=False)
    settings.record_model_validation(profile, True, "synthetic fixture")
    repository.set_default_profile(profile["id"])
    return profile


def test_detached_run_busy_cancel_and_stale_cancel_are_case_bound(direct_client, monkeypatch):
    from app import db, llm
    from app.routes import chat
    verified_profile(direct_client)
    monkeypatch.setattr(chat, "resolve_runtime_target", lambda: llm.LLMRuntimeTarget("https://fixture.invalid/v1", "synthetic", "test", 30))
    async def scenario():
        began, ended = asyncio.Event(), asyncio.Event()
        async def agent(*_a, state, **_k):
            try:
                state["content"] = "已保存部分内容"
                yield {"type": "token", "delta": state["content"], "agent": "router"}
                began.set()
                await asyncio.Event().wait()
            finally:
                ended.set()
        monkeypatch.setattr(chat, "run_agent", agent)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=direct_client.app), base_url="http://test") as client:
            a, b = db.create_case(), db.create_case("其他案例")
            response = await client.post("/api/chat?detach=true", json={"case_id": a["id"], "message": "研究 A"})
            assert response.status_code == 200, response.text
            run = response.json()
            await asyncio.wait_for(began.wait(), 2)
            status = (await client.get("/api/chat/status")).json()["runs"]
            assert status[0]["run_id"] == run["run_id"]
            assert (await client.get(f"/api/cases/{b['id']}")).json()["generating"] is False
            blocked = await client.post("/api/chat", json={"case_id": b["id"], "message": "不能重复启动"})
            assert blocked.status_code == 409
            assert blocked.json()["detail"]["code"] == "RESEARCH_BUSY"
            assert db.list_messages(b["id"]) == []
            assert (await client.post("/api/chat/cancel", json={"case_id": a["id"], "run_id": "stale"})).status_code == 409
            assert not ended.is_set()
            assert (await client.post("/api/chat/cancel", json={"case_id": a["id"], "run_id": run["run_id"]})).json()["cancelled"]
            assert ended.is_set()
            assert not (await client.get("/api/chat/status")).json()["runs"]
            assert db.list_messages(a["id"])[-1]["content"] == "已保存部分内容"
    asyncio.run(scenario())


def test_capabilities_expire_with_connection_revision_and_preserve_frozen_snapshot(direct_client):
    from app import settings
    from app.model_lab import repository
    from app.model_lab.capabilities import connection_capabilities, record_capability
    from app.model_lab.providers import profile_snapshot
    profile = verified_profile(direct_client)
    revision = settings.model_revision(profile)
    assert record_capability(profile["id"], revision, "tools", "supported", "synthetic")
    assert connection_capabilities(profile)["tools"]["state"] == "supported"
    frozen = profile_snapshot(profile)
    repository.update_profile(profile["id"], {"model_id": "changed"})
    changed = repository.get_profile(profile["id"], public=False)
    assert connection_capabilities(changed)["tools"]["state"] == "unknown"
    assert not record_capability(profile["id"], revision, "tools", "supported", "late result")
    assert frozen["capabilities"]["tools"]["state"] == "supported"


def test_unknown_search_skips_network_and_rejected_search_does_not_repeat(direct_client, monkeypatch):
    from app import settings, llm
    profile = verified_profile(direct_client)
    calls = []
    async def reject(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("Error code: 400 unsupported web_search")
    monkeypatch.setattr(llm, "get_client", lambda: SimpleNamespace(responses=SimpleNamespace(create=reject)))
    async def scenario():
        target = llm.LLMRuntimeTarget("https://fixture.invalid/v1", "synthetic", "test", 30, profile_id=profile["id"], revision=settings.model_revision(profile))
        with llm.runtime_target_context(target):
            assert await llm.complete_json_with_web_search("json", "synthetic query") is None
        assert calls == []
        target.capabilities["web_search"] = {"state": "supported", "protocol": "responses"}
        with llm.runtime_target_context(target):
            assert await llm.complete_json_with_web_search("json", "synthetic query") is None
            assert await llm.complete_json_with_web_search("json", "synthetic query") is None
        assert len(calls) == 1
        assert calls[0]["tools"] == [{"type": "web_search"}]
    asyncio.run(scenario())


def test_large_composite_result_remains_valid_json_with_sources_and_units():
    from app.skills.registry import serialize_tool_result
    value = {"ok": True, "meta": {"source": "fixture.source", "units": "原始单位", "as_of": "2026-11-06"}, "data": {"data_points": [[{"name": "银行", "value": i, "rationale": "说明" * 500} for i in range(50)]], "notes": "很长" * 2000}, "artifacts": [{"payload": "duplicate" * 1000}]}
    text = serialize_tool_result(value, 1800)
    assert len(text) <= 1800
    decoded = json.loads(text)
    assert decoded["truncated"] is True
    assert decoded["meta"] == value["meta"]
    assert "artifacts" not in decoded






def test_incomplete_capability_probe_cannot_be_marked_supported(direct_client, monkeypatch):
    from app import llm
    from app.model_lab import capabilities
    profile = verified_profile(direct_client)
    monkeypatch.setattr(llm, "_target_from_profile", lambda _: llm.LLMRuntimeTarget("https://fixture.invalid/v1", "synthetic", "test", 30))
    async def empty(**_kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))])
    monkeypatch.setattr(llm, "get_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=empty))))
    result = asyncio.run(capabilities.probe_capability(profile, "json"))
    assert not result["ok"]
    assert capabilities.connection_capabilities(profile)["json"]["state"] != "supported"


def test_sse_unsubscribe_keeps_background_generation(direct_client, monkeypatch):
    from app import db, llm
    from app.routes import chat
    from app.schemas import ChatRequest
    verified_profile(direct_client)
    monkeypatch.setattr(chat, "resolve_runtime_target", lambda: llm.LLMRuntimeTarget("https://fixture.invalid/v1", "synthetic", "test", 30))
    async def title(*_a, **_k): return "测试标题"
    monkeypatch.setattr(chat, "complete_text", title)
    async def scenario():
        gate, started = asyncio.Event(), asyncio.Event()
        async def agent(*_a, state, **_k):
            started.set(); await gate.wait()
            state["content"] = "后台完成"
            yield {"type": "token", "delta": "后台完成"}
        monkeypatch.setattr(chat, "run_agent", agent)
        case = db.create_case()
        response = await chat.chat(ChatRequest(case_id=case["id"], message="后台研究"))
        await response.body_iterator.__anext__()
        await response.body_iterator.aclose()
        await started.wait()
        task = chat._ACTIVE_RUNS[case["id"]]
        assert not task.done()
        gate.set(); await task
        assert db.list_messages(case["id"])[-1]["content"] == "后台完成"
    asyncio.run(scenario())


def test_agent_body_is_incremental_and_partial_failure_never_duplicates(direct_client, monkeypatch):
    from app import llm
    from httpx import RemoteProtocolError
    target = llm.LLMRuntimeTarget("https://fixture.invalid/v1", "synthetic", "test", 30)
    monkeypatch.setattr(llm, "resolve_runtime_target", lambda: target)
    monkeypatch.setattr(llm, "tools_for_agent", lambda _: [])
    calls, closed = [], []
    class Stream:
        step = 0
        def __aiter__(self): return self
        async def __anext__(self):
            self.step += 1
            if self.step == 1:
                return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="第一段", reasoning_content=None, tool_calls=None), finish_reason=None)])
            raise RemoteProtocolError("synthetic broken stream")
        async def close(self): closed.append(True)
    async def create(**kwargs): calls.append(kwargs); return Stream()
    monkeypatch.setattr(llm, "get_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    async def scenario():
        state = {"content": "", "tool_trace": []}
        generator = llm.run_agent("router", [], agent_def={"skills": []}, state=state)
        first = await generator.__anext__()
        assert {k: first[k] for k in ("type", "agent", "delta")} == {"type": "token", "agent": "router", "delta": "第一段"}
        assert first["round_id"] and first["round"] == 1
        assert state["content"] == "第一段"
        with pytest.raises(RemoteProtocolError): await generator.__anext__()
        assert len(calls) == 1 and closed
    asyncio.run(scenario())


def test_tool_arguments_rejected_before_skill_execution():
    from app.skills.registry import validate_tool_arguments
    schema = {"type": "object", "properties": {"kind": {"type": "array", "items": {"type": "string", "enum": ["news", "global", "announcement"]}}}, "additionalProperties": False}
    with pytest.raises(ValueError): validate_tool_arguments(schema, {"kind": ["policy"]})
    with pytest.raises(ValueError): validate_tool_arguments(schema, {"period": "today"})
    validate_tool_arguments(schema, {"kind": ["global"]})
