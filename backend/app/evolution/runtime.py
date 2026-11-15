"""Task-local Gene/Capsule injection shared by direct and complete team research."""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar

_active = ContextVar("pronoia_evolution_context", default=None)


@contextmanager
def evolution_context(genes, capsule=None, *, task_id="", library_hash="", personality=None, receipt=None, rules=None):
    value = {"genes": genes, "capsule": capsule, "task_id": task_id, "library_hash": library_hash,
             "personality": personality, "receipt": receipt, "rules": rules or []}
    token = _active.set(value)
    try:
        yield value
    finally:
        _active.reset(token)


def current_context():
    return _active.get()


def prompt_context(*, include_strategies=True):
    context = _active.get()
    if not context:
        return ""
    genes = [{k: g[k] for k in ("id", "asset_id", "strategy") if k in g and (include_strategies or k != "strategy")} for g in context["genes"]]
    capsule = context.get("capsule")
    case = {k: capsule.get(k) for k in ("id", "summary", "content")} if capsule else None
    personality = context.get("personality")
    receipt = context.get("receipt") or {}
    payload = {"genes": genes, "historical_case": case,
            "personality": personality, "cycle_receipt": {k: receipt.get(k) for k in ("receipt_id", "gene_asset_id", "personality_hash")}}
    if context.get("rules"):
        payload["structured_rules"] = context["rules"]
    return ("\n【本次 EvoMap 演化上下文】\n" + json.dumps(payload, ensure_ascii=False)
            + "\n执行所选策略，但不得改变本次任务的事前证据边界、预测周期、输出协议或工具权限。"
              "历史案例仅供类比，其结果不代表当前事件结果；资料缺失不得编造。\n")
