"""Shared settings: draft state, versioned validation, migration and launch gates."""
import asyncio
import json

import pytest

from .test_direct_model_credentials import direct_client, create_direct


def test_unconfigured_chat_rejected_before_writing(direct_client):
    from app import db
    state = direct_client.get("/api/settings/status").json()
    assert state["model"]["state"] == "unset"
    assert state["simulation"]["state"] == "unset"
    response = direct_client.post("/api/chat", json={"message": "test"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "MODEL_NOT_READY"
    assert db.list_cases() == []
    assert direct_client.get("/api/health").json()["ok"] is True


def test_save_incomplete_model_draft(direct_client):
    response = direct_client.post("/api/model-lab/profiles", json={"name": "draft", "provider": "custom"})
    assert response.status_code == 201, response.text
    profile = direct_client.get("/api/model-lab/profiles").json()["items"][0]
    assert profile["configuration"]["state"] == "incomplete"
    assert set(profile["configuration"]["missing"]) == {"API URL", "API Key", "模型 ID"}
    assert direct_client.post(f"/api/model-lab/profiles/{profile['id']}/set-default").status_code == 409


def test_validation_is_versioned_and_name_change_preserves_it(direct_client, monkeypatch):
    from app import settings
    from app.model_lab import repository as repo
    from app.routes import model_lab
    profile = create_direct(direct_client)
    identity = profile["id"]
    monkeypatch.setattr(model_lab, "validate_profile", lambda *_a, **_k: {"ok": True, "message": "OK"})
    assert direct_client.post(f"/api/model-lab/profiles/{identity}/validate").json()["ok"]
    assert direct_client.post(f"/api/model-lab/profiles/{identity}/set-default").status_code == 200
    frozen = repo.get_profile(identity, public=False)
    assert settings.model_status(frozen)["ready"]
    repo.update_profile(identity, {"name": "Renamed"})
    assert settings.model_status(repo.get_profile(identity, public=False))["ready"]
    repo.update_profile(identity, {"model_id": "new-model"})
    assert settings.model_status(repo.get_profile(identity, public=False))["state"] == "unverified"
    assert not settings.record_model_validation(frozen, True, "late response")
    assert settings.require_model(snapshot=frozen)["model_id"] == frozen["model_id"]
    assert direct_client.post("/api/chat", json={"message": "test"}).status_code == 409


def test_incomplete_probe_is_not_success(direct_client, monkeypatch):
    from app.routes import model_lab
    profile = create_direct(direct_client)
    monkeypatch.setattr(model_lab, "validate_profile", lambda *_a, **_k: {"ok": False, "message": "探测预算耗尽"})
    result = direct_client.post(f"/api/model-lab/profiles/{profile['id']}/validate").json()
    assert not result["ok"]
    assert result["profile"]["configuration"]["state"] == "failed"


def test_migration_idempotent_and_backfills_only_placeholder(direct_client, monkeypatch):
    from app import config, db, settings
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://models.example.invalid/v1")
    monkeypatch.setattr(config, "LLM_API_KEY", "synthetic-migration-key")
    monkeypatch.setattr(config, "LLM_MODEL", "migrate-model")
    monkeypatch.setenv("PRONOIA_SIMULATION_GATEWAY_URL", "http://127.0.0.1:5010")
    a, b = db.create_case(), db.create_case("自定义标题")
    db.add_message(a["id"], role="user", content="分析 今天 的行情")
    db.add_message(b["id"], role="user", content="保留原题")
    for _ in range(2):
        response = direct_client.post("/api/settings/initialize")
        assert response.status_code == 200, response.text
    profiles = direct_client.get("/api/model-lab/profiles").json()["items"]
    assert len(profiles) == 1
    assert profiles[0]["configuration"]["state"] == "unverified"
    assert "synthetic-migration-key" not in json.dumps(response.json())
    assert db.get_case(a["id"])["title"] == "分析今天的行情"
    assert db.get_case(b["id"])["title"] == "自定义标题"
    assert settings.gateway_status()["state"] == "unverified"


def test_service_draft_and_validation_cas(direct_client, monkeypatch):
    from app import settings
    from app.routes import settings as routes
    draft = direct_client.post("/api/settings/services", json={"name": "draft", "kind": "event", "auth_mode": "none"})
    assert draft.status_code == 200, draft.text
    item = draft.json()
    assert item["configuration"]["state"] == "incomplete"
    assert direct_client.get(f"/api/settings/services/{item['id']}/spec").status_code == 409
    result = direct_client.put(f"/api/settings/services/{item['id']}", json={"name": "test", "kind": "event", "endpoint": "https://models.example.invalid/predict", "auth_mode": "none", "revision": item["revision"]})
    assert result.status_code == 200
    old = settings.service(item["id"])
    from app.event_backtest import external_strategy
    monkeypatch.setattr(external_strategy, "build_event_request", lambda *_: {})
    monkeypatch.setattr(external_strategy, "_post", lambda *_: {"direction": "up"})
    monkeypatch.setattr(external_strategy, "_prediction", lambda *_a, **_k: None)
    assert direct_client.post(f"/api/settings/services/{item['id']}/validate").json()["ok"]
    spec = direct_client.get(f"/api/settings/services/{item['id']}/spec").json()
    assert spec["endpoint"] == old["endpoint"]
    assert "api_key" not in spec
    stale = direct_client.put(f"/api/settings/services/{item['id']}", json={"name": "late", "kind": "event", "endpoint": old["endpoint"], "auth_mode": "none", "revision": "stale"})
    assert stale.status_code == 409


def test_quant_and_imported_runs_do_not_need_llm(direct_client):
    from app.settings import require_run
    for spec in ({"type": "quant"}, {"type": "event", "adapter": "imported_decisions"}, {"type": "event", "runner": "baseline"}):
        require_run({"config": {"strategy_spec": spec}})


def test_gateway_no_silent_default_and_revision(direct_client, monkeypatch):
    from app import settings
    from app.routes import settings as routes
    assert direct_client.get("/api/settings/simulation").json()["configuration"]["state"] == "unset"
    result = direct_client.put("/api/settings/simulation", json={"endpoint": "http://127.0.0.1:5010", "timeout_seconds": 15})
    assert result.status_code == 200, result.text
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"ok": True}
    monkeypatch.setattr(routes.requests, "get", lambda *_a, **_k: Response())
    assert direct_client.post("/api/settings/simulation/validate").json()["ok"]
    assert settings.require_gateway()["endpoint"] == "http://127.0.0.1:5010"


def test_title_saved_even_if_main_generation_fails(direct_client, monkeypatch):
    from app import db
    from app.routes import chat
    from app.schemas import ChatRequest
    async def failing(*_a, **_k):
        raise RuntimeError("synthetic failure")
        yield
    monkeypatch.setattr(chat, "run_agent", failing)
    asyncio.run(chat._run_chat(ChatRequest(message="检查 新研究标题"), asyncio.Queue()))
    assert db.list_cases()[0]["title"] == "检查新研究标题"


def test_title_optimization_never_overwrites_custom_title(direct_client, monkeypatch):
    from app import db
    from app.routes import chat
    case = db.create_case("手动标题")
    async def title(_): return "模型标题"
    monkeypatch.setattr(chat, "_gen_title", title)
    asyncio.run(chat._improve_title(case["id"], "question", "本地兜底"))
    assert db.get_case(case["id"])["title"] == "手动标题"


def test_reverting_model_still_requires_new_validation(direct_client):
    from app import settings
    from app.model_lab import repository as repo
    created = create_direct(direct_client)
    profile = repo.get_profile(created["id"], public=False)
    assert settings.record_model_validation(profile, True, "original success")
    repo.update_profile(profile["id"], {"model_id": "other"})
    repo.update_profile(profile["id"], {"model_id": profile["model_id"]})
    assert not settings.record_model_validation(profile, True, "stale success")
    assert settings.model_status(repo.get_profile(profile["id"], public=False))["state"] == "unverified"


def test_gateway_jobs_keep_creation_endpoint(direct_client, monkeypatch):
    from app import settings
    from app.routes import simulations
    connection = {"endpoint": "http://127.0.0.1:5010", "timeout_seconds": 15}
    job = {"request_payload": {"_gateway_connection": connection}}
    settings.write("simulation", {"endpoint": "http://127.0.0.1:7777", "timeout_seconds": 30})
    assert simulations._job_connection(job) == connection


def test_migration_failure_retry_does_not_duplicate_profiles(direct_client, monkeypatch):
    from app import config, settings
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://models.example.invalid/v1")
    monkeypatch.setattr(config, "LLM_API_KEY", "synthetic-migration-key")
    monkeypatch.setattr(config, "LLM_MODEL", "migrate-model")
    original = settings.backfill_titles
    def fail(): raise RuntimeError("controlled")
    monkeypatch.setattr(settings, "backfill_titles", fail)
    assert direct_client.post("/api/settings/initialize").status_code == 503
    assert not settings.read("initialized", False)
    monkeypatch.setattr(settings, "backfill_titles", original)
    assert direct_client.post("/api/settings/initialize").status_code == 200
    assert len(direct_client.get("/api/model-lab/profiles").json()["items"]) == 1


def test_title_empty_response_uses_fallback(direct_client, monkeypatch):
    from app.routes import chat
    async def empty(*_a, **_k): return ""
    monkeypatch.setattr(chat, "complete_text", empty)
    assert asyncio.run(chat._gen_title("分析 今天 行情")) == "分析今天行情"


def test_only_affected_features_require_configuration(direct_client):
    from app import db
    assert direct_client.get("/api/cases").status_code == 200
    assert direct_client.get("/api/bt/runs").status_code == 200
    assert direct_client.get("/api/suggestions").json()["fallback"]
    case = db.create_case()
    response = direct_client.post(f"/api/cases/{case['id']}/simulations", json={"source_graph_artifact_id": "none"})
    assert response.status_code == 409
    assert response.json()["detail"]["settings_url"] == "/settings/simulation"


@pytest.mark.parametrize("failure,expected", [(401, "认证失败"), (404, "接口不存在"), (429, "配额"), ("timeout", "超时"), ("empty", "响应内容无效")])
def test_service_probe_failure_is_readable_and_never_verified(direct_client, monkeypatch, failure, expected):
    import io
    from urllib.error import HTTPError
    from app.event_backtest import external_strategy
    item = direct_client.post("/api/settings/services", json={"name": "probe", "kind": "event", "endpoint": "https://models.example.invalid/predict", "auth_mode": "none"}).json()
    def fail(*_a, **_k):
        if isinstance(failure, int):
            try:
                raise HTTPError("https://models.example.invalid/predict", failure, "error", {}, io.BytesIO(b"credential-must-stay-hidden"))
            except HTTPError as cause:
                raise ValueError("unsafe-provider-body") from cause
        if failure == "timeout": raise TimeoutError("unsafe-provider-body")
        return {}
    monkeypatch.setattr(external_strategy, "_post", fail)
    result = direct_client.post(f"/api/settings/services/{item['id']}/validate").json()
    assert result["ok"] is False
    assert expected in result["message"]
    assert "unsafe-provider-body" not in result["message"]
    assert "credential-must-stay-hidden" not in result["message"]
    assert direct_client.get(f"/api/settings/services/{item['id']}/spec").status_code == 409


def test_title_optimizer_timeout_preserves_local_title(direct_client, monkeypatch):
    from app.routes import chat
    async def text(*_a, **_k): return "late title"
    async def timeout(awaitable, *, timeout):
        assert timeout == 10
        awaitable.close()
        raise asyncio.TimeoutError
    monkeypatch.setattr(chat, "complete_text", text)
    monkeypatch.setattr(chat.asyncio, "wait_for", timeout)
    assert asyncio.run(chat._gen_title("首轮失败也保留标题")) == "首轮失败也保留标题"


@pytest.mark.parametrize("code,expected", [(401, "API Key"), (403, "权限"), (404, "模型 ID"), (429, "额度"), (503, "暂时不可用")])
def test_runtime_provider_errors_explain_cause_without_echoing_body(code, expected):
    from app.llm import friendly_runtime_error
    error = RuntimeError(f"Error code: {code} - unsafe-provider-body")
    message = friendly_runtime_error(error)
    assert expected in message
    assert "unsafe-provider-body" not in message
