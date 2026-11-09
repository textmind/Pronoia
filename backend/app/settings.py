"""Shared connection state, migration and launch checks. Reads never probe services."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from urllib.parse import urlsplit

from fastapi import HTTPException

from . import config, db
from .model_lab import repository as models
from .model_lab.secret_store import secret_reference_configured, store_secret

_LOCK = threading.RLock()
LABELS = {"unset": "未设置", "incomplete": "配置不完整", "unverified": "未验证", "verified": "验证成功", "failed": "验证失败", "disabled": "已停用"}
MODEL_FIELDS = ("provider", "base_url", "model_id", "secret_env_ref", "max_output_tokens", "timeout_seconds", "thinking_mode", "configuration_revision")


def read(key: str, default=None):
    with db._lock:
        row = db._get_conn().execute("SELECT value_json FROM ml_settings WHERE key=?", ("settings:" + key,)).fetchone()
    return json.loads(row[0]) if row else default


def write(key: str, value) -> None:
    with db._lock:
        db._get_conn().execute(
            "INSERT INTO ml_settings(key,value_json,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
            ("settings:" + key, json.dumps(value, ensure_ascii=False), db.now_iso()),
        )
        db._get_conn().commit()


def fingerprint(value: dict, fields=None) -> str:
    payload = {key: value.get(key) for key in fields} if fields else {key: val for key, val in value.items() if key not in {"name", "id", "validation", "is_active", "source"}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def model_revision(profile: dict) -> str:
    return fingerprint(profile, MODEL_FIELDS)


def status(value: dict | None, missing: list[str], validation: dict | None, revision: str, url: str) -> dict:
    if value is None:
        state = "unset"
    elif not value.get("is_active", True):
        state = "disabled"
    elif missing:
        state = "incomplete"
    elif not validation or validation.get("revision") != revision:
        state = "unverified"
    else:
        state = "verified" if validation.get("ok") else "failed"
    return {"state": state, "label": LABELS[state], "ready": state == "verified", "missing": missing, "settings_url": url,
            "message": (validation or {}).get("message", "") if validation and validation.get("revision") == revision else "",
            "validated_at": (validation or {}).get("at") if validation and validation.get("revision") == revision else None}


def model_status(profile: dict | None) -> dict:
    missing = []
    if profile:
        missing = [label for key, label in (("base_url", "API URL"), ("model_id", "模型 ID")) if not profile.get(key)]
        if not profile.get("secret_configured"):
            missing.append("API Key")
    return status(profile, missing, read("model-validation:" + str((profile or {}).get("id"))), model_revision(profile or {}), "/settings/models")


def record_model_validation(profile: dict, ok: bool, message: str) -> bool:
    with _LOCK:
        current = models.get_profile(profile["id"], public=False)
        if not current or model_revision(current) != model_revision(profile):
            return False
        write("model-validation:" + profile["id"], {"revision": model_revision(profile), "ok": ok, "message": message[:500], "at": db.now_iso()})
        models.record_validation(profile["id"], ok=ok, message=message)
        if ok:
            write("verified-model:" + model_revision(profile), True)
    return True


def public_model(profile: dict) -> dict:
    from .model_lab.capabilities import connection_capabilities
    return {**{key: val for key, val in profile.items() if key != "secret_env_ref"}, "configuration": model_status(profile),
            "capabilities": connection_capabilities(profile),
            "thinking_supported": urlsplit(profile.get("base_url") or "").hostname == "api.deepseek.com"}


def require_model(profile_id: str | None = None, snapshot: dict | None = None) -> dict:
    from .model_lab.platform_default import PLATFORM_DEFAULT_SELECTOR, is_platform_profile_id
    if snapshot:
        profile = dict(snapshot)
        profile_id = str(profile.get("id") or profile_id or "")
        # Frozen snapshots may continue using a previously verified revision.
        validation = read("verified-model:" + model_revision(profile))
        if validation:
            from .model_lab.providers import resolve_profile_secret
            try:
                if resolve_profile_secret(profile):
                    return profile
            except Exception:
                pass
    if not profile_id or profile_id == PLATFORM_DEFAULT_SELECTOR or is_platform_profile_id(profile_id):
        profile_id = models.get_default_profile_id()
    profile = models.get_profile(profile_id, public=False) if profile_id else None
    state = model_status(profile)
    if not state["ready"]:
        reject("MODEL_NOT_READY", state)
    return profile


def reject(code: str, state: dict):
    reason = state["label"] + ("：" + "、".join(state["missing"]) if state.get("missing") else "")
    if state.get("state") == "failed" and state.get("message"):
        reason += "（" + state["message"] + "）"
    raise HTTPException(409, detail={"code": code, "message": "连接" + reason + "，请前往设置完成配置与验证", "settings_url": state["settings_url"], "state": state["state"]})


def service_status(value: dict | None) -> dict:
    missing = []
    if value:
        if not value.get("endpoint"):
            missing.append("服务地址")
        headers = value.get("headers") or {}
        if value.get("auth_mode", "none") != "none" and not headers:
            missing.append("API Key")
        from .strategy_credentials import resolve_strategy_secret_reference
        for ref in headers.values():
            try:
                if ref.startswith("local-strategy:"):
                    resolve_strategy_secret_reference(ref, label="服务密钥")
                elif ref.startswith("env:"):
                    from .model_endpoint_security import resolve_secret_env_reference
                    resolve_secret_env_reference(ref, namespace="strategy", label="服务密钥")
                else:
                    missing.append("有效的密钥引用")
            except Exception:
                missing.append("API Key")
    return status(value, missing, (value or {}).get("validation"), fingerprint(value or {}), "/settings/services")


def public_service(value: dict) -> dict:
    return {**{key: val for key, val in value.items() if key not in {"headers", "validation"}}, "revision": fingerprint(value), "secret_configured": bool(value.get("headers")), "configuration": service_status(value)}


def services() -> list[dict]:
    return read("services", [])


def service(identity: str) -> dict | None:
    return next((item for item in services() if item["id"] == identity), None)


def require_service(spec: dict) -> dict:
    frozen = read("verified-service:" + str(spec.get("service_revision") or ""))
    if isinstance(frozen, dict):
        return {**spec, **frozen}
    value = service(str(spec.get("service_connection_id") or ""))
    if not value:
        value = next((item for item in services() if item.get("endpoint") == spec.get("endpoint") and item.get("headers", {}) == spec.get("headers", {})), None)
    state = service_status(value)
    if not state["ready"]:
        reject("SERVICE_NOT_READY", state)
    expected = spec.get("service_revision")
    if expected and expected != fingerprint(value):
        # The frozen spec has its own previously verified version.
        reject("SERVICE_NOT_READY", {**state, "state": "unverified", "label": "未验证"})
    return {**spec, "endpoint": value["endpoint"], "headers": value.get("headers", {}), "timeout_seconds": value.get("timeout_seconds", 30), "version": value.get("version", ""), "service_connection_id": value["id"], "service_revision": fingerprint(value)}


def gateway_status() -> dict:
    value = read("simulation")
    missing = ["网关地址"] if value is not None and not value.get("endpoint") else []
    return status(value, missing, (value or {}).get("validation"), fingerprint(value or {}), "/settings/simulation")


def require_gateway() -> dict:
    state = gateway_status()
    if not state["ready"]:
        reject("SIMULATION_NOT_READY", state)
    return read("simulation")


def overview() -> dict:
    profile_id = models.get_default_profile_id()
    profile = models.get_profile(profile_id, public=False) if profile_id else None
    model = {**model_status(profile), "profile_id": profile_id, "name": (profile or {}).get("name"), "model_id": (profile or {}).get("model_id")}
    from .model_lab.capabilities import connection_capabilities
    model["research_ready"] = model["ready"] and connection_capabilities(profile or {})["tools"]["state"] != "unsupported"
    if model["state"] == "unset":
        model["settings_url"] = "/settings/defaults"
    return {"initialized": bool(read("initialized", False)), "model": model, "simulation": gateway_status(), "services": [public_service(item) for item in services()]}


def backfill_titles() -> None:
    from .routes.chat import fallback_title
    with db._lock:
        rows = db._get_conn().execute("SELECT c.id,(SELECT content FROM messages m WHERE m.case_id=c.id AND m.role='user' AND trim(m.content)!='' ORDER BY m.created_at,m.rowid LIMIT 1) AS question FROM cases c WHERE c.title='新研究'").fetchall()
        for row in rows:
            if row["question"]:
                db._get_conn().execute("UPDATE cases SET title=? WHERE id=? AND title='新研究'", (fallback_title(row["question"]), row["id"]))
        db._get_conn().commit()


def initialize() -> dict:
    with _LOCK:
        if read("initialized", False):
            return overview()
        existing = models.list_profiles(public=False)
        if config.LLM_BASE_URL and config.LLM_MODEL and config.LLM_API_KEY:
            match = next((item for item in existing if item["base_url"].rstrip("/") == config.LLM_BASE_URL.rstrip("/") and item["model_id"] == config.LLM_MODEL), None)
            if not match:
                from .model_lab.defaults import DEFAULT_MAX_OUTPUT_TOKENS
                created = models.create_profile({"name": "环境配置 · " + config.LLM_MODEL + " · " + db.new_id()[:4], "provider": "custom", "base_url": config.LLM_BASE_URL, "model_id": config.LLM_MODEL, "secret_env_ref": store_secret(config.LLM_API_KEY), "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS, "timeout_seconds": config.LLM_TIMEOUT})
                match = created
            if not models.get_default_profile_id():
                models.set_default_profile(match["id"])
        if not read("simulation") and os.getenv("PRONOIA_SIMULATION_GATEWAY_URL"):
            write("simulation", {"endpoint": config.SIMULATION_GATEWAY_URL, "timeout_seconds": config.SIMULATION_GATEWAY_TIMEOUT, "is_active": True, "source": "environment"})
        # Import saved external specifications without altering historical runs.
        with db._lock:
            rows = db._get_conn().execute("SELECT config_json FROM bt_runs").fetchall()
            saved_rows = db._get_conn().execute("SELECT definition_json FROM bt_saved_models").fetchall()
        imported = services()
        for row in [*rows, *saved_rows]:
            try:
                cfg = json.loads(row[0] or "{}")
                spec = cfg.get("strategy_spec") or {}
                if not spec.get("endpoint") or spec.get("adapter") != "external_http":
                    continue
                item = {"kind": "quant" if spec.get("type") == "api" else "event", "endpoint": spec["endpoint"], "headers": spec.get("headers") or {}, "timeout_seconds": spec.get("timeout_seconds", 30), "version": spec.get("version", ""), "is_active": True, "auth_mode": "header" if spec.get("headers") else "none"}
                if any(fingerprint(old) == fingerprint(item) for old in imported):
                    continue
                item.update(id=db.new_id(), name="已迁入服务 " + str(len(imported) + 1))
                imported.append(item)
            except (ValueError, TypeError):
                continue
        write("services", imported)
        backfill_titles()
        write("initialized", True)
        return overview()


def require_run(run: dict) -> None:
    cfg = run.get("config") or {}
    spec = cfg.get("strategy_spec") or run.get("strategy_spec") or {}
    if spec.get("adapter") == "external_http" or spec.get("endpoint"):
        require_service(spec)
    elif spec.get("type") == "quant" or spec.get("type") == "api" or spec.get("adapter") in {"imported_decisions", "imported_predictions", "signal_file"} or run.get("runner") in {"baseline", "tool_only", "imported_predictions", "provided_analysis"} or spec.get("runner") in {"baseline", "provided_analysis"}:
        return
    else:
        require_model(cfg.get("model_profile_id"), cfg.get("model_profile_snapshot"))
