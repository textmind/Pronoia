"""Fixed denominators, quality gates and a separate exploration incumbent."""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def evaluate(events: list[dict], labels: dict[str, dict], predictions: list[dict]) -> dict:
    ids = [e["event_id"] for e in events]
    if len(ids) != len(set(ids)) or any(eid not in labels for eid in ids):
        raise ValueError("duplicate events or missing frozen labels")
    pred = {p["event_id"]: p for p in predictions}
    if len(pred) != len(predictions) or set(pred) - set(ids):
        raise ValueError("duplicate or foreign predictions")
    correct = available = high_n = high_wrong = audit_bad = 0
    tokens_in = tokens_out = step_ms = 0
    groups: dict[str, dict] = defaultdict(lambda: {"n": 0, "correct": 0, "available": 0})
    confidence_bins = defaultdict(lambda: {"n": 0, "correct": 0, "confidence_sum": 0.0})
    direction_counts = {"actual_up": 0, "actual_down": 0, "predicted_up": 0, "predicted_down": 0}
    outcomes = []
    for event in events:
        eid = event["event_id"]
        truth = labels[eid]["label_t3"]
        if truth not in {"up", "down"}:
            raise ValueError("experiment requires binary eligible labels")
        p = pred.get(eid, {})
        meta = p.get("strategy_metadata", {})
        valid = (not p.get("abstain", True) and p.get("pred_direction") in {"up", "down"}
                 and meta.get("prediction_status") == "available")
        hit = bool(valid and p["pred_direction"] == truth)
        direction_counts["actual_" + truth] += 1
        if valid:
            direction_counts["predicted_" + p["pred_direction"]] += 1
            confidence = float(p.get("confidence") or 0)
            bucket = "<0.4" if confidence < .4 else "0.4-0.6" if confidence < .6 else "0.6-0.7" if confidence < .7 else ">=0.7"
            confidence_bins[bucket]["n"] += 1
            confidence_bins[bucket]["correct"] += hit
            confidence_bins[bucket]["confidence_sum"] += confidence
        correct += hit
        available += valid
        high = bool(valid and float(p.get("confidence") or 0) >= 0.7)
        high_n += high
        high_wrong += high and not hit
        audit_bad += not meta.get("rule_audit", {}).get("valid", False)
        tokens_in += int(p.get("tokens_in") or 0)
        tokens_out += int(p.get("tokens_out") or 0)
        step_ms += int(p.get("step_ms") or 0)
        g = groups[event["market"] + ":" + event["event_type_l2"]]
        g["n"] += 1; g["correct"] += hit; g["available"] += valid
        outcomes.append({"event_id": eid, "date": event["event_time"][:10], "correct": hit})
    n = len(events)
    if not n:
        raise ValueError("empty evaluation universe")
    return {"n": n, "correct": correct, "score": correct / n,
            "available": available, "coverage": available / n,
            "answered_accuracy": correct / available if available else None,
            "missing_predictions": n - len(pred), "high_confidence_n": high_n,
            "high_confidence_wrong": high_wrong,
            "high_confidence_error_rate": high_wrong / high_n if high_n else None,
            "audit_violation_rate": audit_bad / n,
            "tokens_in": tokens_in, "tokens_out": tokens_out, "total_tokens": tokens_in + tokens_out,
            "mean_input_tokens": tokens_in / n, "total_step_ms": step_ms, "mean_step_ms": step_ms / n,
            "groups": dict(groups), "outcomes": outcomes,
            "direction_counts": direction_counts, "confidence_bins": dict(confidence_bins),
            "confidence_interpretation": "uncalibrated evidence confidence; bins diagnostic only"}


def summary(metrics: dict) -> dict:
    """Only aggregate validation feedback crosses the optimizer boundary."""
    return {k: v for k, v in metrics.items() if k != "outcomes"}


def decide(candidate: dict, best: dict, baseline: dict, *, delta: float = 0.005,
           audit_limit: float = 0.10, max_input_ratio: float = 1.25,
           candidate_rule_count: int = 10, best_rule_count: int = 10) -> dict[str, Any]:
    if candidate["n"] != best["n"] or candidate["n"] != baseline["n"]:
        raise ValueError("candidate evaluation universe changed")
    reasons = []
    if candidate["missing_predictions"]:
        reasons.append("missing_predictions")
    if candidate["coverage"] + 1e-12 < baseline["coverage"] - 0.02:
        reasons.append("coverage_regression")
    if candidate["audit_violation_rate"] > audit_limit:
        reasons.append("audit_violations")
    if candidate["mean_input_tokens"] > baseline["mean_input_tokens"] * max_input_ratio:
        reasons.append("input_token_budget")
    # Integer counts avoid rounded display percentages influencing acceptance.
    if not candidate["correct"] > best["correct"] - delta * candidate["n"]:
        reasons.append("below_best_tolerance")
    accepted = not reasons
    better = candidate["correct"] > best["correct"]
    if candidate["correct"] == best["correct"]:
        better = (candidate_rule_count, candidate["mean_input_tokens"]) < (
            best_rule_count, best["mean_input_tokens"])
    return {"accepted_for_exploration": accepted, "new_best": accepted and better,
            "reasons": reasons, "delta": delta}
