"""Incremental 500-event Team Full online test stream.

All 500 frozen historical events are treated as one test population.  The
first 100 events from the completed online v2 run are reused as a frozen
prefix; the remaining 400 are executed once and appended to the same
evolution runtime, so restarts never reissue a completed model call.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import shutil
from collections import defaultdict
from pathlib import Path

from .. import settings
from . import online
from .online_backtest import _paired, _read, _run_group


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def _confidence_metrics(rows: list[dict], labels: dict[str, dict]) -> dict:
    bins = {"<0.4": {"n": 0, "correct": 0, "confidence_sum": 0.0},
            "0.4-0.6": {"n": 0, "correct": 0, "confidence_sum": 0.0},
            "0.6-0.7": {"n": 0, "correct": 0, "confidence_sum": 0.0},
            ">=0.7": {"n": 0, "correct": 0, "confidence_sum": 0.0}}
    high_n = high_wrong = 0
    valid = 0
    correct = 0
    for row in rows:
        try:
            confidence = max(0.0, min(1.0, float(row.get("confidence"))))
        except (TypeError, ValueError):
            confidence = 0.0
        bucket = "<0.4" if confidence < .4 else "0.4-0.6" if confidence < .6 else "0.6-0.7" if confidence < .7 else ">=0.7"
        hit = row.get("pred_direction") in {"up", "down"} and row.get("pred_direction") == labels[row["event_id"]].get("label_t3")
        bins[bucket]["n"] += 1
        bins[bucket]["correct"] += int(hit)
        bins[bucket]["confidence_sum"] += confidence
        if row.get("pred_direction") in {"up", "down"}:
            valid += 1
            correct += int(hit)
        if confidence >= .7 and row.get("pred_direction") in {"up", "down"}:
            high_n += 1
            high_wrong += int(not hit)
    for value in bins.values():
        value["accuracy"] = value["correct"] / value["n"] if value["n"] else None
        value["mean_confidence"] = value["confidence_sum"] / value["n"] if value["n"] else None
    return {
        "mean_confidence": sum(v["confidence_sum"] for v in bins.values()) / len(rows) if rows else None,
        "confidence_bins": bins,
        "high_confidence_n": high_n,
        "high_confidence_wrong": high_wrong,
        "high_confidence_error_rate": high_wrong / high_n if high_n else None,
        "valid_n": valid,
        "correct": correct,
        "confidence_interpretation": "uncalibrated evidence confidence; bins are diagnostic only",
    }


def _metrics(rows: list[dict], labels: dict[str, dict]) -> dict:
    n = len(rows)
    valid = sum(row.get("pred_direction") in {"up", "down"} for row in rows)
    correct = sum(row.get("pred_direction") in {"up", "down"} and row.get("pred_direction") == labels[row["event_id"]].get("label_t3") for row in rows)
    return {
        "correct": correct, "n": n, "accuracy": correct / n if n else None,
        "coverage": valid / n if n else 0, "invalid_or_abstain": n - valid,
        "total_tokens": sum(int(row.get("tokens_in") or 0) + int(row.get("tokens_out") or 0) for row in rows),
        "confidence": _confidence_metrics(rows, labels),
    }


def _copy_prefix(source: Path, target: Path) -> None:
    if target.exists():
        return
    shutil.copytree(source, target)


def _load_events(dataset: Path) -> tuple[list[dict], dict[str, dict]]:
    events = []
    labels = {}
    # The files retain their preparation names, but this experiment deliberately
    # treats all 500 rows as one test population.  No row is used as a train or
    # validation objective; only event-time maturity controls feedback release.
    for split in ("test", "validation", "learn"):
        events.extend(_read(dataset / f"{split}.events.jsonl"))
        labels.update({row["event_id"]: row for row in _read(dataset / "oracle" / f"{split}.labels.jsonl")})
    if len(events) != 500 or len(labels) != 500:
        raise ValueError(f"expected 500 events and labels, got {len(events)}/{len(labels)}")
    return events, labels


async def run(dataset: Path, out: Path, *, prefix: Path, profile: dict, concurrency: int) -> dict:
    events, labels = _load_events(dataset)
    old_events = {row["event_id"] for row in _read(dataset / "test.events.jsonl")}
    _copy_prefix(prefix, out)
    baseline_dir, evolution_dir = out / "baseline", out / "evolution"
    frozen_dir = out / "frozen_prefix"
    frozen_dir.mkdir(parents=True, exist_ok=True)
    frozen_baseline_path = frozen_dir / "baseline_predictions.jsonl"
    frozen_evolution_path = frozen_dir / "evolution_predictions.jsonl"
    if not frozen_baseline_path.exists():
        shutil.copyfile(baseline_dir / "predictions.jsonl", frozen_baseline_path)
    if not frozen_evolution_path.exists():
        shutil.copyfile(evolution_dir / "predictions.jsonl", frozen_evolution_path)
    old_baseline = {row["event_id"]: row for row in _read(frozen_baseline_path)}
    old_evolution = {row["event_id"]: row for row in _read(frozen_evolution_path)}
    if set(old_baseline) != old_events or set(old_evolution) != old_events:
        raise ValueError("prefix predictions do not exactly match the frozen test split")
    new_events = [row for row in events if row["event_id"] not in old_events]
    new_events.sort(key=lambda row: (str(row.get("event_time", "")), row["event_id"]))
    # The existing 100-row test run is a frozen prefix.  Its runtime and
    # predictions are reused; the 400 new rows continue from that checkpoint.
    new_baseline = await _run_group(new_events, labels, baseline_dir, evolving=False, profile=profile, concurrency=concurrency)
    new_evolution = await _run_group(new_events, labels, evolution_dir, evolving=True, profile=profile, concurrency=concurrency)
    baseline = [old_baseline[eid] for eid in old_events] + new_baseline
    evolution = [old_evolution[eid] for eid in old_events] + new_evolution
    baseline.sort(key=lambda row: row["event_id"])
    evolution.sort(key=lambda row: row["event_id"])
    _write_json(out / "baseline_metrics.json", _metrics(baseline, labels))
    _write_json(out / "evolution_metrics.json", _metrics(evolution, labels))
    paired = _paired(baseline, evolution, labels)
    report = {
        "protocol": "incremental_online_team_full_500_v1",
        "dataset": str(dataset), "n": 500,
        "reused_prefix": {"n": 100, "source": str(prefix), "split": "test", "rerun": False},
        "test_population": {"n": 500, "source_files": ["test.events.jsonl", "validation.events.jsonl", "learn.events.jsonl"],
                            "data_split_semantics": "none; all rows are scored as one online test stream"},
        "new_events": {"n": len(new_events), "rerun": False},
        "continuation_order": "frozen_100_prefix_then_400_new_events",
        "baseline": _metrics(baseline, labels),
        "evolution": _metrics(evolution, labels),
        "paired": paired,
        "paired_ci_95": paired["paired_ci_95"],
    }
    assets = online.engine_for(evolution_dir / "runtime").snapshot()
    report["evolution_assets"] = {"genes": len(assets.get("genes") or []), "capsules": len(assets.get("capsules") or []),
                                   "events": len(assets.get("events") or []), "memory": len(assets.get("memory") or []),
                                   "runtime": str(evolution_dir / "runtime")}
    report["improvement_proven"] = bool(paired["paired_ci_95"][0] > 0 and report["evolution"]["coverage"] >= report["baseline"]["coverage"] - .02)
    report["verdict"] = "improved" if report["improvement_proven"] else ("not_improved" if paired["delta"] <= 0 else "insufficient_evidence")
    _write_json(out / "result.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--concurrency", type=int, default=16)
    args = parser.parse_args()
    profile = settings.require_model()
    if args.model:
        profile = {**profile, "model_id": args.model}
    result = asyncio.run(run(args.dataset.resolve(), args.out.resolve(), prefix=args.prefix.resolve(), profile=profile, concurrency=args.concurrency))
    print(json.dumps({k: result[k] for k in ("baseline", "evolution", "paired", "improvement_proven", "verdict")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
