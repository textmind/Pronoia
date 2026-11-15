"""Unified settings APIs. Credential values are write-only."""
from __future__ import annotations

import asyncio
import copy
import math
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .. import db, settings
from ..model_endpoint_security import redact_sensitive_text, validate_registered_model_endpoint
from .external_service import ServiceConfig, _spec, _event

router = APIRouter(prefix="/api/settings", tags=["settings"])


def service_failure_reason(exc: Exception) -> str:
    # Provider bodies can echo credentials or request data; use safe categories.
    if exc.__cause__:
        return service_failure_reason(exc.__cause__)
    if isinstance(exc, (requests.Timeout, TimeoutError)):
        return "请求超时，请检查地址或调整超时"
    if isinstance(exc, (requests.HTTPError, HTTPError)):
        code = getattr(exc, "code", None) or getattr(getattr(exc, "response", None), "status_code", None)
        return {401: "认证失败，请检查密钥", 403: "服务拒绝访问，请检查密钥权限", 404: "服务接口不存在，请检查地址", 429: "服务限流或配额不足，请稍后重试"}.get(code, "服务返回异常状态，请检查服务运行情况")
    if isinstance(exc, (requests.ConnectionError, URLError)):
        return "无法连接服务，请检查地址和网络"
    return "响应内容无效，未收到符合协议的预测结果"


@router.get("/status")
def status():
    return settings.overview()


@router.post("/initialize")
def initialize():
    try:
        return settings.initialize()
    except Exception:
        raise HTTPException(503, detail="配置迁移未完成，请重试；原有配置已保留") from None


class SavedService(ServiceConfig):
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(pattern="^(event|quant)$")
    endpoint: str = Field(default="", max_length=4000)
    is_active: bool = True
    revision: str | None = None


@router.get("/services")
def services():
    return {"items": [settings.public_service(item) for item in settings.services()]}


def save_service(payload: SavedService, identity: str | None):
    with settings._LOCK:
        current = settings.service(identity) if identity else None
        if identity and current is None:
            raise HTTPException(404, detail="连接不存在")
        if current and payload.revision != settings.fingerprint(current):
            raise HTTPException(409, detail="连接已更新，请重新读取后保存")
        headers = {}
        if payload.api_key.get_secret_value() or payload.secret_env_ref:
            spec, _ = _spec(payload)
            headers = spec["headers"]
        elif payload.auth_mode != "none" and current:
            if (payload.endpoint, payload.auth_mode, payload.header_name) != (current.get("endpoint"), current.get("auth_mode"), current.get("header_name")):
                raise HTTPException(422, detail="更换服务地址或认证方式后，请重新填写密钥")
            headers = current.get("headers", {})
        if payload.endpoint:
            validate_registered_model_endpoint(payload.endpoint, label="预测服务")
        item = {"id": identity or db.new_id(), "name": payload.name, "kind": payload.kind, "endpoint": payload.endpoint.strip(),
                "auth_mode": payload.auth_mode, "header_name": payload.header_name, "headers": headers,
                "timeout_seconds": payload.timeout_seconds, "version": payload.version, "is_active": payload.is_active}
        item["configuration_revision"] = (current or {}).get("configuration_revision", 0)
        if current and settings.fingerprint(item) == settings.fingerprint(current):
            item["validation"] = current.get("validation")
        elif current:
            item["configuration_revision"] += 1
        settings.write("services", [item if old["id"] == item["id"] else old for old in settings.services()] if current else [*settings.services(), item])
        return {**settings.public_service(item), "revision": settings.fingerprint(item)}


@router.post("/services")
def create_service(payload: SavedService):
    try:
        return save_service(payload, None)
    except ValueError as exc:
        raise HTTPException(422, detail=redact_sensitive_text(exc, payload.api_key.get_secret_value())) from None


@router.put("/services/{identity}")
def update_service(identity: str, payload: SavedService):
    try:
        return save_service(payload, identity)
    except ValueError as exc:
        raise HTTPException(422, detail=redact_sensitive_text(exc, payload.api_key.get_secret_value())) from None


@router.get("/services/{identity}/spec")
def service_spec(identity: str):
    item = settings.service(identity)
    if not item:
        raise HTTPException(404, detail="连接不存在")
    spec = settings.require_service({"service_connection_id": identity})
    # Opaque references are accepted by the existing strategy serializers;
    # plaintext credentials never leave the secret store.
    return {**spec, "kind": item["kind"]}


@router.post("/services/{identity}/validate")
async def validate_service(identity: str):
    item = copy.deepcopy(settings.service(identity))
    if not item:
        raise HTTPException(404, detail="连接不存在")
    revision = settings.fingerprint(item)
    def probe():
        from ..event_backtest import external_strategy
        spec = {"endpoint": item["endpoint"], "headers": item.get("headers", {}), "timeout_seconds": item["timeout_seconds"]}
        if item["kind"] == "event":
            event, _ = _event(None)
            payload = external_strategy.build_event_request(event, spec, "t3")
            response = external_strategy._post(spec["endpoint"], payload, spec)
            row = response.get("decision", response) if isinstance(response, dict) else response
            external_strategy._prediction(row, event_id=event.event_id, run_id="connection-test", model_version="probe", target_horizon="t3")
        else:
            from ..quant_backtest.strategies import ExternalHTTPStrategy
            strategy = ExternalHTTPStrategy(endpoint=spec["endpoint"], headers=spec["headers"], timeout_seconds=spec["timeout_seconds"])
            stamp = "2025-01-10"
            response = strategy._post({"schema_version": "pronoia.quant-model.v1", "decision_timing": "signal_at_bar_close_execute_next_bar_open", "point_in_time_enforced": False,
                                       "dataset": {"symbol": "SPY", "market": "US", "frequency": "1d"}, "bars": [{"timestamp": stamp, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}], "context": [], "parameters": {}})
            signals = response.get("signals") if isinstance(response, dict) else None
            if not isinstance(signals, list) or not signals:
                raise ValueError("响应须包含非空 signals 数组")
            for row in signals:
                if str(row.get("timestamp", ""))[:10] != stamp or not math.isfinite(float(row["target_weight"])):
                    raise ValueError("响应时间戳或 target_weight 无效")
    try:
        await asyncio.to_thread(probe)
        ok, message = True, "验证成功，已收到有效预测响应（使用虚拟样例）"
    except Exception as exc:
        ok, message = False, "验证失败：" + service_failure_reason(exc)
    with settings._LOCK:
        current = settings.service(identity)
        if not current or settings.fingerprint(current) != revision:
            return {"ok": False, "message": "配置已修改，请重新验证"}
        current["validation"] = {"revision": revision, "ok": ok, "message": message, "at": db.now_iso()}
        settings.write("services", [current if old["id"] == identity else old for old in settings.services()])
        if ok:
            settings.write("verified-service:" + revision, {"endpoint": item["endpoint"], "headers": item.get("headers", {}), "timeout_seconds": item["timeout_seconds"], "version": item.get("version", ""), "service_connection_id": item["id"], "service_revision": revision})
    return {"ok": ok, "message": message}


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(default="", max_length=4000)
    timeout_seconds: float = Field(default=15, gt=0, le=300)
    is_active: bool = True
    revision: str | None = None


def validate_gateway_url(url: str):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(422, detail="请输入不含凭据和查询参数的 HTTP(S) 网关地址")
    # The simulation gateway is explicitly a local/private deployment service.
    validate_registered_model_endpoint(url, label="推演网关", allow_private=True)


@router.get("/simulation")
def gateway():
    value = settings.read("simulation") or {}
    return {**value, "revision": settings.fingerprint(value), "configuration": settings.gateway_status()}


@router.put("/simulation")
def save_gateway(payload: GatewayConfig):
    if payload.endpoint:
        validate_gateway_url(payload.endpoint)
    with settings._LOCK:
        current = settings.read("simulation") or {}
        if current and payload.revision != settings.fingerprint(current):
            raise HTTPException(409, detail="网关配置已更新，请重新读取")
        item = payload.model_dump(exclude={"revision"})
        item["endpoint"] = item["endpoint"].strip().rstrip("/")
        item["configuration_revision"] = current.get("configuration_revision", 0)
        if settings.fingerprint(item) == settings.fingerprint(current):
            item["validation"] = current.get("validation")
        elif current:
            item["configuration_revision"] += 1
        settings.write("simulation", item)
    return gateway()


@router.post("/simulation/validate")
async def validate_gateway():
    value = settings.read("simulation") or {}
    revision = settings.fingerprint(value)
    try:
        validate_gateway_url(value.get("endpoint", ""))
        response = await asyncio.to_thread(requests.get, value["endpoint"].rstrip("/") + "/health", timeout=min(value.get("timeout_seconds", 15), 15), allow_redirects=False)
        response.raise_for_status()
        data = response.json()
        ok = isinstance(data, dict) and (data.get("ok") is True or data.get("status") in {"ok", "healthy"})
        message = "网关验证成功" if ok else "网关健康接口未返回可用状态"
    except Exception:
        ok, message = False, "网关验证失败，请检查地址和服务运行状态"
    with settings._LOCK:
        if settings.fingerprint(settings.read("simulation") or {}) != revision:
            return {"ok": False, "message": "配置已修改，请重新验证"}
        value["validation"] = {"revision": revision, "ok": ok, "message": message, "at": db.now_iso()}
        settings.write("simulation", value)
    return {"ok": ok, "message": message}
