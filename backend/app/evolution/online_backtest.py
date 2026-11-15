"""Sequential 100-event comparison for team_full with online EvoMap feedback.

This runner intentionally keeps the baseline and evolving runs in separate
directories and only releases a historical label when its recorded maturity
time has passed the next event's as-of time.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
from collections import defaultdict
from contextlib import nullcontext
from pathlib import Path

from ..event_backtest.engine import _event_prompt, run_team_full_one_event
from ..event_backtest.models import EventRecord, TeamPrediction
from ..llm import model_profile_context
from . import online
from .runtime import evolution_context
from .gep import GEPError


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in rows))


def _metrics(rows: list[dict], labels: dict[str, dict]) -> dict:
    correct = sum(1 for row in rows if row.get("pred_direction") in {"up", "down"}
                 and row.get("pred_direction") == labels[row["event_id"]].get("label_t3"))
    n = len(rows)
    valid = sum(1 for row in rows if row.get("pred_direction") in {"up", "down"})
    return {"correct": correct, "n": n, "accuracy": correct / n if n else None,
            "coverage": valid / n if n else 0, "invalid_or_abstain": n - valid,
            "total_tokens": sum(int(row.get("tokens_in") or 0) + int(row.get("tokens_out") or 0) for row in rows)}


def _paired(baseline: list[dict], evolved: list[dict], labels: dict[str, dict]) -> dict:
    b = {row["event_id"]: row for row in baseline}
    e = {row["event_id"]: row for row in evolved}
    pairs = []
    for eid, label in labels.items():
        if eid not in b or eid not in e:
            continue
        truth = label.get("label_t3")
        bp = b[eid].get("pred_direction") == truth
        ep = e[eid].get("pred_direction") == truth
        pairs.append({"event_id": eid, "baseline_pass": bp, "evolved_pass": ep})
    evolved_only = sum(int(x["evolved_pass"] and not x["baseline_pass"]) for x in pairs)
    baseline_only = sum(int(x["baseline_pass"] and not x["evolved_pass"]) for x in pairs)
    deltas = [int(x["evolved_pass"]) - int(x["baseline_pass"]) for x in pairs]
    # Date blocks are supplied by the caller through event ids when available;
    # the exact paired count remains the primary result for this 100-row run.
    delta = sum(deltas) / len(deltas) if deltas else 0.0
    rng = random.Random(20261113)
    draws = []
    for _ in range(2000):
        draws.append(sum(rng.choice(deltas) for _ in deltas) / len(deltas) if deltas else 0.0)
    draws.sort()
    return {"n": len(pairs), "evolved_only": evolved_only, "baseline_only": baseline_only,
            "delta": delta, "delta_pp": delta * 100,
            "paired_ci_95": [draws[50] * 100, draws[1949] * 100] if draws else [0, 0],
            "pairs": pairs}


def _event_asset(eid: str, gene: dict, signals: list[str], score: float, status: str) -> dict:
    return {"type": "EvolutionEvent", "id": "event_bt_" + eid,
            "intent": "optimize", "signals": signals, "genes_used": [gene["id"]],
            "mutation_id": "mutation_bt_" + eid, "blast_radius": {"files": 0, "lines": 0},
            "outcome": {"status": status, "score": score}, "source_type": "generated",
            "meta": {"task_id": eid, "split": "test"}}


def _commit_or_feedback(runtime_root: Path, gene: dict, label: dict, event_asset: dict, receipt: dict | None) -> None:
    """Persist mature feedback even when an older cycle receipt is stale.

    EvoMap intentionally rejects a receipt created under an older personality
    state.  In a sequential backtest later cycles may legitimately update that
    state before an earlier label matures, so the measured event falls back to
    the idempotent feedback API while preserving the same asset identity.
    """
    engine = online.engine_for(runtime_root)
    if receipt:
        try:
            execution = {k: receipt[k] for k in ("experiment_hash", "dataset_hash", "model_hash", "task_hash", "library_hash")}
            execution.update({"status": event_asset["outcome"]["status"], "score": event_asset["outcome"]["score"],
                              "n": 1, "task_id": event_asset["id"], "strong_evidence": True})
            engine.commit_cycle(receipt, execution, summary="Sequential team_full label matured")
            return
        except GEPError as error:
            if "Personality changed" not in str(error):
                raise
    capsule = None
    if event_asset["outcome"]["status"] == "success":
        capsule = engine.seal({
            "type": "Capsule", "id": "capsule_bt_" + event_asset["id"],
            "trigger": event_asset["signals"], "gene": gene["id"],
            "summary": "Measured Team Full event outcome",
            "confidence": 0.7, "blast_radius": {"files": 0, "lines": 0},
            "outcome": event_asset["outcome"],
            "execution_trace": [{"stage": "validate", "task_id": event_asset["id"],
                                 "metric": "direction_accuracy", "n": 1,
                                 "correct": int(event_asset["outcome"]["score"] > 0)}],
        })
    engine.call("feedback", event=event_asset, gene=gene, capsule=capsule,
                action_id="act_bt_" + event_asset["id"], memory_eligible=True)


def _cached_prediction(event: EventRecord, out: Path, run_id: str) -> dict | None:
    """Recover a completed Team Full event after an interrupted batch."""
    path = out / "trajectories" / f"{event.event_id}.json"
    try:
        payload = json.loads(path.read_text())
        extracted = payload["structured_extract"]
        stats = payload.get("llm_trajectory_stats") or {}
        return TeamPrediction(
            event_id=event.event_id,
            pred_direction=extracted.get("direction"),
            run_id=run_id,
            model_version=str(payload.get("model_version") or ""),
            confidence=extracted.get("confidence"),
            rationale=extracted.get("rationale") or "",
            abstain=bool(extracted.get("abstain")),
            horizon=extracted.get("target_horizon") or "t3",
            strategy_metadata={"prediction_status": extracted.get("prediction_status"),
                               "output_validation_errors": extracted.get("output_validation_errors") or [],
                               "recovered_from_trajectory": True},
            tokens_in=int(stats.get("tokens_in") or 0),
            tokens_out=int(stats.get("tokens_out") or 0),
            step_ms=int(float(payload.get("wall_seconds") or 0) * 1000),
            expected_return_pct=extracted.get("expected_return_pct"),
        ).to_dict()
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


async def _run_group(events, labels, out: Path, *, evolving: bool, profile: dict, concurrency: int) -> list[dict]:
    rows: list[dict] = []
    out.mkdir(parents=True, exist_ok=True)

    async def run_one(event: EventRecord, index: int, context: dict | None) -> tuple[dict, dict | None]:
        cached = _cached_prediction(event, out, out.name)
        if cached is not None:
            cached["index"] = index
            return cached, context
        with model_profile_context(profile), evolution_context(
                (context or {}).get("genes", []), (context or {}).get("capsule"),
                task_id=f"bt:{event.event_id}", library_hash=(context or {}).get("library_hash", "")):
            prediction = await run_team_full_one_event(
                event, run_id=out.name, model_version=profile["model_id"],
                trajectory_ckpt_dir=out / "trajectories",
            )
        row = prediction.to_dict()
        row["index"] = index
        return row, context

    if not evolving:
        # Baseline has no mutable evolution state and can safely fan out.
        sem = asyncio.Semaphore(max(1, int(concurrency or 1)))

        async def bounded(raw: dict, index: int) -> dict:
            async with sem:
                row, _ = await run_one(EventRecord.from_dict(raw), index, None)
                return row

        rows = list(await asyncio.gather(*(bounded(raw, index)
                                           for index, raw in enumerate(events, start=1))))
        rows.sort(key=lambda row: int(row.get("index", 0)))
        _write(out / "predictions.jsonl", rows)
        return rows

    # Evolving predictions run in bounded online batches. Contexts are prepared
    # in event order, mature feedback is released before each context, and the
    # batch's newly observed outcomes become eligible only for the next batch.
    runtime_root = out / "runtime" if evolving else None
    pending: list[tuple[dict, dict, dict, dict | None]] = []
    batch_size = max(1, min(int(concurrency or 1), 16))
    for start in range(0, len(events), batch_size):
        batch = []
        for index, raw in list(enumerate(events, start=1))[start:start + batch_size]:
            event = EventRecord.from_dict(raw)
            cutoff = str(event.available_time or event.event_time)
            for item in list(pending):
                if str(item[1].get("label_available_at") or "") <= cutoff:
                    try:
                        _commit_or_feedback(runtime_root, item[0], item[1], item[2], item[3])
                        online.record_bucket_outcome(runtime_root=runtime_root, bucket_key=item[2]["meta"]["bucket_key"],
                                                     success=item[2]["outcome"]["status"] == "success",
                                                     gene_id=item[0]["id"], event_id=item[2]["id"])
                    finally:
                        pending.remove(item)
            signals, bucket_key = online.derive_event_signals(event)
            candidate = online.ensure_bucket_candidate(runtime_root=runtime_root, bucket_key=bucket_key,
                                                       signals=signals, rationale=event.title)
            context = await asyncio.to_thread(
                online.prepare_context, _event_prompt(event), task_id=f"bt:{event.event_id}",
                runtime_root=runtime_root, extra_signals=signals, bucket_key=bucket_key)
            if candidate.get("gene") and not context.get("genes"):
                context["genes"] = [candidate["gene"]]
            context["bucket_key"] = bucket_key
            batch.append((index, event, context))

        results = await asyncio.gather(*(run_one(event, index, context) for index, event, context in batch))
        for (index, event, context), (row, _) in sorted(zip(batch, results), key=lambda pair: pair[0][0]):
            rows.append(row)
            selected = (context or {}).get("genes") or []
            if selected:
                signals = (context or {}).get("signals") or ["research"]
                truth = labels[event.event_id].get("label_t3")
                score = float(row.get("pred_direction") == truth)
                event_asset = _event_asset(event.event_id, selected[0], signals, score,
                                           "success" if score else "failed")
                event_asset["meta"]["bucket_key"] = context.get("bucket_key") or "unknown"
                pending.append((selected[0], labels[event.event_id], event_asset,
                                (context or {}).get("receipt")))
        rows.sort(key=lambda row: int(row.get("index", 0)))
        _write(out / "predictions.jsonl", rows)

    for gene, label, event_asset, receipt in pending:
        _commit_or_feedback(runtime_root, gene, label, event_asset, receipt)
        online.record_bucket_outcome(runtime_root=runtime_root, bucket_key=event_asset["meta"]["bucket_key"],
                                     success=event_asset["outcome"]["status"] == "success",
                                     gene_id=gene["id"], event_id=event_asset["id"])
    return rows


async def run(dataset: Path, out: Path, *, profile: dict, concurrency: int = 1) -> dict:
    events = sorted(_read(dataset / "test.events.jsonl"), key=lambda x: str(x.get("event_time", "")))
    if len(events) != 100:
        raise ValueError(f"online 100-run requires exactly 100 test events, got {len(events)}")
    labels = {row["event_id"]: row for row in _read(dataset / "oracle" / "test.labels.jsonl")}
    if set(labels) != {row["event_id"] for row in events}:
        raise ValueError("test labels do not match frozen test events")
    baseline = await _run_group(events, labels, out / "baseline", evolving=False, profile=profile, concurrency=concurrency)
    evolved = await _run_group(events, labels, out / "evolution", evolving=True, profile=profile, concurrency=concurrency)
    report = {"protocol": "sequential_online_team_full_v1", "dataset": str(dataset), "n": 100,
              "baseline": _metrics(baseline, labels), "evolution": _metrics(evolved, labels),
              "paired": _paired(baseline, evolved, labels)}
    with_assets = online.engine_for(out / "evolution" / "runtime").snapshot()
    report["evolution_assets"] = {"genes": len(with_assets.get("genes") or []),
                                  "capsules": len(with_assets.get("capsules") or []),
                                  "events": len(with_assets.get("events") or []),
                                  "memory": len(with_assets.get("memory") or []),
                                  "runtime": str(out / "evolution" / "runtime")}
    paired = report["paired"]
    report["improvement_proven"] = bool(paired["paired_ci_95"][0] > 0 and report["evolution"]["coverage"] >= report["baseline"]["coverage"] - .02)
    report["verdict"] = "improved" if report["improvement_proven"] else ("not_improved" if paired["delta"] <= 0 else "insufficient_evidence")
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--concurrency", type=int, default=1)
    args = parser.parse_args()
    from .. import settings
    profile = settings.require_model()
    if args.model:
        profile = {**profile, "model_id": args.model}
    result = asyncio.run(run(args.dataset.resolve(), args.out.resolve(), profile=profile, concurrency=args.concurrency))
    print(json.dumps({k: result[k] for k in ("baseline", "evolution", "paired", "improvement_proven", "verdict")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
