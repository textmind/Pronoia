"""Background, task-local EvoMap evolution for ordinary chat and team runs.

The chat path only writes an immutable episode and schedules this module.  The
worker performs the slower EvoMap calls out of band, so a disconnected browser
or a slow provider cannot delay the user's answer.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any

from .. import config
from .gep import EvoMap, GEPError

ROOT = config.PROJECT_ROOT / "backtesting" / "evomap" / "online"
EPISODES = ROOT / "episodes"
QUEUE = ROOT / "queue"
CANDIDATES = ROOT / "candidates"
RUNTIME = ROOT / "runtime"
_lock = threading.RLock()
_worker: asyncio.Task | None = None
_wake: asyncio.Event | None = None


def _atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    tmp.replace(path)


def _read(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _redact(value: str, limit: int = 1200) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(api[_-]?key|token|authorization|password|secret)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", text)
    text = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text, flags=re.I)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)
    return text[:limit]


def _sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, list):
        return [_sanitize(item) for item in value[:200]]
    if isinstance(value, dict):
        return {str(key): _sanitize(item) for key, item in list(value.items())[:200]}
    return value


def _episode_id(case_id: str, message_id: str, user: str, assistant: str) -> str:
    raw = "\n".join([case_id, message_id, user, assistant])
    return "chat_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _engine() -> EvoMap:
    return EvoMap(RUNTIME)


def engine_for(runtime_root: Path | str | None = None) -> EvoMap:
    """Return an EvoMap engine isolated to one online session or backtest."""
    return EvoMap(Path(runtime_root).resolve() if runtime_root is not None else RUNTIME)


def _candidate_root(runtime_root: Path | str | None = None) -> Path:
    if runtime_root is None or Path(runtime_root).resolve() == RUNTIME.resolve():
        return CANDIDATES
    return Path(runtime_root).resolve().parent / "candidates"


def _bucket_level(value: Any, *, positive: float = 2.0, negative: float = -2.0) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if number >= positive:
        return "strong"
    if number <= negative:
        return "weak"
    return "neutral"


def derive_event_signals(event: Any) -> tuple[list[str], str]:
    """Build deterministic, as-of-only financial buckets for Gene routing."""
    raw = event.to_dict() if hasattr(event, "to_dict") else dict(event or {})
    text = " ".join(str(raw.get(key) or "") for key in ("title", "event_type_l2", "event_text"))
    lowered = text.lower()
    if any(token in text for token in ("业绩预告", "业绩快报", "财报", "利润", "营收", "定期报告")):
        event_kind = "earnings_guidance"
    elif any(token in text for token in ("诉讼", "判决", "仲裁")):
        event_kind = "litigation"
    elif any(token in lowered for token in ("policy", "rate", "利率", "政策")):
        event_kind = "policy"
    else:
        event_kind = "other"
    if any(token in text for token in ("非经常", "一次性", "公允价值", "处置", "减值")):
        quality = "non_recurring"
    elif any(token in text for token in ("主营", "销量", "价格上涨", "订单", "经营")):
        quality = "operating"
    elif any(token in text for token in ("三费", "降本", "成本管控")):
        quality = "cost_cutting"
    else:
        quality = "unknown"
    if any(token in text for token in ("扭亏", "由亏转盈")):
        growth = "turnaround"
    elif any(token in text for token in ("减亏",)):
        growth = "loss_reduction"
    else:
        match = re.search(r"(?:增长|同比|增幅)[^%]{0,24}([0-9]+(?:\.[0-9]+)?)%", text)
        try:
            pct = float(match.group(1)) if match else None
        except (TypeError, ValueError):
            pct = None
        growth = "gte50" if pct is not None and pct >= 50 else "20_50" if pct is not None and pct >= 20 else "lt20" if pct is not None else "unknown"
    features = raw.get("pre_event_features") or {}
    signals = {
        "financial_research", f"market:{str(raw.get('market') or 'unknown').lower()}",
        f"event_type:{event_kind}", f"growth_band:{growth}", f"profit_quality:{quality}",
        f"pre5_relative:{_bucket_level(features.get('excess_return_5d_pct'))}",
        f"pre20_relative:{_bucket_level(features.get('excess_return_20d_pct'))}",
        f"horizon:{raw.get('analysis_horizon') or 't3'}",
    }
    bucket = "|".join(sorted(signals))
    return sorted(signals), bucket


def _seed_gene(engine: EvoMap) -> dict:
    snapshot = engine.snapshot()
    genes = snapshot.get("genes") or []
    if genes:
        return genes[0]
    gene = engine.seal({
        "type": "Gene", "id": "gene_pronoia_chat_observation_v1", "category": "optimize",
        "signals_match": ["financial_research", "research", "observed_problem"],
        "summary": "Preserve evidence boundaries and record measurable outcomes from a completed Pronoia interaction.",
        "strategy": ["Keep the user's evidence boundary and output contract unchanged.", "Record a measurable outcome before treating an interaction as a reusable success."],
        "constraints": {"max_files": 1, "forbidden_paths": ["oracle", "test.labels.jsonl", ".env"]},
        "validation": ["pronoia:episode_immutable"],
    })
    engine.install([gene])
    return gene


def prepare_context(text: str, *, task_id: str = "", runtime_root: Path | str | None = None,
                    extra_signals: list[str] | None = None, bucket_key: str = "") -> dict:
    """Select the current local Gene/Capsule for a new chat turn."""
    engine = _engine() if runtime_root is None else engine_for(runtime_root)
    candidate_root = _candidate_root(runtime_root)
    seed = _seed_gene(engine)
    extracted = engine.call("extract", corpus=_redact(text, 5000))
    signals = sorted(set((extracted.get("signals") or ["research"]) + list(extra_signals or [])))
    snapshot = engine.snapshot()
    active_ids = {seed.get("id")}
    for path in candidate_root.glob("*.json"):
        row = _read(path, {})
        row_signals = set(row.get("signals") or [])
        if row.get("status") == "active" and (not row_signals or row_signals.intersection(signals)) and (row.get("gene") or {}).get("id"):
            active_ids.add(row["gene"]["id"])
    genes = [gene for gene in (snapshot.get("genes") or []) if gene.get("id") in active_ids]
    selected = engine.select(signals, genes=genes or [seed], capsules=snapshot.get("capsules") or [])
    gene = selected.get("selectedGene") or seed
    capsule = selected.get("selectedCapsule")
    receipt = None
    try:
        receipt = engine.prepare_cycle(
            cycle_id=task_id or "online_cycle_" + hashlib.sha256(text.encode()).hexdigest()[:20],
            experiment_hash=_hash("pronoia-online"), dataset_hash="online", model_hash="online-model",
            task_hash=_hash(_redact(text, 5000)), library_hash=_hash({"gene": gene.get("asset_id"), "signals": signals}),
            signals=signals, forced_gene_id=gene.get("asset_id") or gene.get("id"),
        )
    except GEPError:
        # A selector result is still useful when a cycle receipt cannot be
        # prepared (for example while an older local runtime is upgrading).
        receipt = None
    return {"genes": [gene], "capsule": capsule, "signals": signals, "bucket_key": bucket_key,
            "selector": selected.get("selector") or {}, "memory_advice": selected.get("memoryAdvice") or {},
            "task_id": task_id, "library_hash": _hash({"gene": gene.get("asset_id"), "signals": signals}),
            "receipt": receipt}


def quick_context(text: str, *, task_id: str = "") -> dict:
    """Fast cache-only context used on the latency-sensitive chat path.

    The authoritative selector still runs in the background and in backtests;
    this path only exposes already-promoted local Genes, so a cold Node start
    can never delay a user response.
    """
    lowered = str(text or "").lower()
    hints = {"research"}
    if any(word in lowered for word in ("错误", "失败", "不对", "error", "failed")):
        hints.add("observed_problem")
    if any(word in lowered for word in ("股票", "公司", "财报", "利润", "earnings", "market")):
        hints.add("financial_research")
    active = []
    for path in CANDIDATES.glob("*.json"):
        row = _read(path, {})
        gene = row.get("gene") or {}
        if row.get("status") == "active" and gene.get("id"):
            signals = set(row.get("signals") or gene.get("signals_match") or [])
            if signals & hints:
                active.append((int(row.get("support_count", 0)), gene))
    active.sort(key=lambda item: item[0], reverse=True)
    genes = [active[0][1]] if active else []
    return {"genes": genes, "capsule": None, "signals": sorted(hints), "task_id": task_id,
            "library_hash": _hash({"genes": [g.get("asset_id") for g in genes], "signals": sorted(hints)}),
            "receipt": None, "rules": []}


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def capture_episode(*, case_id: str, message_id: str, user_text: str, assistant_text: str,
                    trace: Any = None, mode: str = "team", model_revision: str = "") -> str:
    """Persist an episode and queue it. Safe to call repeatedly for retries."""
    eid = _episode_id(case_id, message_id, user_text, assistant_text)
    episode = {"type": "PronoiaChatEpisode", "schema_version": 1, "episode_id": eid,
               "case_id": case_id, "message_id": message_id, "mode": mode,
               "user": _redact(user_text), "assistant": _redact(assistant_text),
               "trace": _sanitize(trace if isinstance(trace, list) else []), "model_revision": _redact(model_revision, 200),
               "content_hash": _hash([case_id, message_id, user_text, assistant_text])}
    with _lock:
        path = EPISODES / f"{eid}.json"
        existing = _read(path)
        if existing and existing != episode:
            raise ValueError("chat episode identity changed")
        _atomic(path, episode)
        qpath = QUEUE / f"{eid}.json"
        queued = _read(qpath)
        if not queued:
            _atomic(qpath, {"episode_id": eid, "status": "queued", "attempts": 0})
    if _wake is not None:
        _wake.set()
    else:
        start()
    return eid


def _candidate_for(engine: EvoMap, episode: dict, signals: list[str]) -> tuple[dict, dict]:
    signal_digest = hashlib.sha256(json.dumps(sorted(set(signals)), ensure_ascii=False).encode()).hexdigest()
    logical = "gene_pronoia_chat_" + signal_digest[:24]
    gene = engine.seal({
        "type": "Gene", "id": logical, "category": "optimize", "signals_match": sorted(set(signals + ["research"])),
        "summary": "Reusable evidence-preserving interaction strategy derived from a completed Pronoia episode.",
        "strategy": ["For similar requests, preserve the as-of evidence boundary and cite the available evidence.",
                      "Candidate derived from immutable episodes; require later measured feedback before treating it as proven."],
        "constraints": {"max_files": 1, "forbidden_paths": ["oracle", "test.labels.jsonl", ".env"]},
        "validation": ["pronoia:episode_immutable", "pronoia:feedback_required"],
    })
    return gene, {"candidate_id": signal_digest[:24], "gene": gene, "episode_id": episode["episode_id"],
                  "status": "shadow", "support_count": 1, "signals": signals}


def _candidate_for_bucket(engine: EvoMap, *, bucket_key: str, signals: list[str], parent: dict | None = None,
                          rationale: str = "") -> tuple[dict, dict]:
    digest = hashlib.sha256(bucket_key.encode("utf-8")).hexdigest()[:24]
    logical = "gene_pronoia_bucket_" + digest
    strategy = [
        "Use only the supplied as-of packet and preserve the benchmark-relative T+3 contract.",
        "Separate recurring operating evidence from one-off gains, pre-event relative strength, and missing data.",
        "Record the decision rationale and defer reuse until measured feedback proves the strategy.",
    ]
    if rationale:
        strategy.append("Observed pattern: " + _redact(rationale, 280))
    gene = engine.seal({
        "type": "Gene", "id": logical, "category": "optimize",
        "signals_match": sorted(set(signals + ["research"])),
        "summary": "Bucket-specific financial event strategy awaiting measured probation evidence.",
        "strategy": strategy,
        "constraints": {"max_files": 1, "forbidden_paths": ["oracle", "test.labels.jsonl", ".env"]},
        "validation": ["pronoia:episode_immutable", "pronoia:fixed_denominator_accuracy"],
    })
    candidate = {"candidate_id": digest, "bucket_key": bucket_key, "gene": gene,
                 "status": "probation", "support_count": 0, "success_count": 0,
                 "failure_count": 0, "signals": signals, "parent_gene_id": (parent or {}).get("id")}
    return gene, candidate


def ensure_bucket_candidate(*, runtime_root: Path | str, bucket_key: str, signals: list[str],
                            rationale: str = "") -> dict:
    """Create one immutable bucket Gene and expose it during probation."""
    engine = engine_for(runtime_root)
    candidate_root = _candidate_root(runtime_root)
    digest = hashlib.sha256(bucket_key.encode("utf-8")).hexdigest()[:24]
    path = candidate_root / f"{digest}.json"
    existing = _read(path)
    if existing and (existing.get("gene") or {}).get("id"):
        return existing
    gene, candidate = _candidate_for_bucket(engine, bucket_key=bucket_key, signals=signals, rationale=rationale)
    try:
        engine.call("capture_draft", gene=gene, reason=f"probation bucket {bucket_key}")
    except GEPError:
        engine.install([gene])
    candidate.update({"status": "active", "promotion_state": "probation", "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()})
    _atomic(path, candidate)
    return candidate


def record_bucket_outcome(*, runtime_root: Path | str, bucket_key: str, success: bool,
                          gene_id: str, event_id: str) -> dict:
    """Update local probation state and invoke EvoMap's official promoter."""
    candidate_root = _candidate_root(runtime_root)
    digest = hashlib.sha256(bucket_key.encode("utf-8")).hexdigest()[:24]
    path = candidate_root / f"{digest}.json"
    row = _read(path, {})
    if not row:
        return {}
    row["support_count"] = int(row.get("support_count", 0)) + 1
    row["success_count"] = int(row.get("success_count", 0)) + int(bool(success))
    row["failure_count"] = int(row.get("failure_count", 0)) + int(not success)
    row["last_event_id"] = event_id
    if row["failure_count"] > 0:
        row["status"] = "retired"
        row["promotion_state"] = "rejected"
    elif row["success_count"] >= 3:
        row["promotion_state"] = "promoted"
        row["status"] = "active"
    _atomic(path, row)
    try:
        promoted = engine_for(runtime_root).call("promote_probation", min_success=3)
        if gene_id in set(promoted.get("promoted") or []):
            row["promotion_state"] = "promoted"
            _atomic(path, row)
    except GEPError:
        # The local state remains durable; the next mature outcome retries the
        # official review-ledger promotion without losing the experiment.
        pass
    return row


def process_episode(eid: str) -> dict:
    qpath = QUEUE / f"{eid}.json"
    q = _read(qpath)
    episode = _read(EPISODES / f"{eid}.json")
    if not q or not episode:
        raise FileNotFoundError(eid)
    if q.get("status") == "complete":
        return q
    q.update(status="running", attempts=int(q.get("attempts", 0)) + 1)
    _atomic(qpath, q)
    try:
        engine = _engine()
        extracted = engine.call("extract", corpus=episode["user"] + "\n" + episode["assistant"])
        signals = sorted(set(extracted.get("signals") or ["research"]))
        gene, candidate = _candidate_for(engine, episode, signals)
        engine.install([gene])
        # A completed interaction is an observed episode, not proof of a
        # successful strategy.  Record it as a non-memory-eligible event so
        # EvoMap keeps the audit trail without allowing unverified experience
        # to influence selection.
        event = {"type": "EvolutionEvent", "id": "event_" + eid, "intent": "optimize",
                 "signals": signals, "genes_used": [gene["id"]],
                 "mutation_id": "mutation_" + eid, "blast_radius": {"files": 0, "lines": 0},
                 "outcome": {"status": "failed", "score": 0}, "source_type": "generated",
                 "meta": {"episode_id": eid, "feedback_required": True}}
        engine.call("feedback", event=event, gene=gene, action_id="act_" + eid, memory_eligible=False)
        previous = _read(CANDIDATES / f"{candidate['candidate_id']}.json")
        if previous:
            candidate["support_count"] = int(previous.get("support_count", 0)) + 1
            candidate["status"] = "active" if candidate["support_count"] >= 3 else "shadow"
            candidate["first_episode_id"] = previous.get("first_episode_id", previous.get("episode_id"))
        else:
            candidate["first_episode_id"] = eid
        _atomic(CANDIDATES / f"{candidate['candidate_id']}.json", candidate)
        q.update(status="complete", candidate_id=candidate["candidate_id"], gene_id=gene["id"], signals=signals)
        _atomic(qpath, q)
        return q
    except Exception as exc:  # keep queue durable for the next retry
        q.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        _atomic(qpath, q)
        raise


async def _run_worker() -> None:
    while True:
        for path in sorted(QUEUE.glob("*.json")):
            item = _read(path, {})
            if item.get("status") in {"queued", "failed"} and int(item.get("attempts", 0)) < 3:
                with suppress(Exception):
                    await asyncio.to_thread(process_episode, path.stem)
        if _wake is not None:
            _wake.clear()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_wake.wait(), timeout=5)
        else:
            await asyncio.sleep(5)


def start() -> None:
    global _worker, _wake
    if _worker is not None and not _worker.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _wake = asyncio.Event()
    _worker = loop.create_task(_run_worker(), name="pronoia-online-evolution")


def stop() -> None:
    global _worker
    if _worker is not None and not _worker.done():
        _worker.cancel()
    _worker = None


def status() -> dict:
    rows = [_read(p, {}) for p in sorted(QUEUE.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)]
    counts = {state: sum(1 for row in rows if row.get("status") == state) for state in ("queued", "running", "complete", "failed")}
    candidates = [_read(p, {}) for p in sorted(CANDIDATES.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)]
    return {"running": bool(_worker and not _worker.done()), "counts": counts, "queue": rows[:100],
            "candidates": candidates[:100], "root": str(ROOT)}


def rollback(candidate_id: str) -> dict:
    path = CANDIDATES / f"{candidate_id}.json"
    row = _read(path)
    if not row:
        raise FileNotFoundError(candidate_id)
    row["status"] = "retired"
    _atomic(path, row)
    return row
