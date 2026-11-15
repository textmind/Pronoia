"""Oracle-owning scorer. Test answers are inaccessible before the final lock."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from ..event_backtest.evolution.evaluation import evaluate, summary
from ..event_backtest.evolution.prepare import read_jsonl
from ..event_backtest.evolution.rubric import digest
from .corpus import atomic_json
from .identity import implementation_hash


def score(dataset: Path, experiment: Path, split: str, predictions: Path, output: Path):
    dataset, experiment, predictions = dataset.resolve(), experiment.resolve(), predictions.resolve()
    if not predictions.is_relative_to(experiment):
        raise ValueError("prediction journal must belong to this experiment")
    manifest = json.loads((dataset / "manifest.json").read_text())
    config = json.loads(predictions.with_name("config.json").read_text())
    experiment_manifest = json.loads((experiment / "manifest.json").read_text())
    if experiment_manifest["implementation_hash"] != implementation_hash():
        raise ValueError("experiment implementation changed")
    if config["dataset_hash"] != digest(manifest) or config["experiment_hash"] != digest(experiment_manifest):
        raise ValueError("frozen data/model/run identity changed")
    if split == "test":
        lock = json.loads((experiment / "final_lock.json").read_text())
        if lock["experiment_hash"] != digest(experiment_manifest) or config["version"] not in lock["versions"]:
            raise ValueError("test prediction was not registered before evaluation")
        if lock["versions"][config["version"]] != config["library_hash"]:
            raise ValueError("test policy differs from the final frozen policy")
    all_events = read_jsonl(dataset / f"{split}.events.jsonl")
    if digest(all_events) != manifest["hashes"][split]["events"]:
        raise ValueError("frozen event packets changed")
    all_labels = read_jsonl(dataset / "oracle" / f"{split}.labels.jsonl")
    if digest(all_labels) != manifest["hashes"][split]["labels"]:
        raise ValueError("frozen labels changed")
    evaluation_ids = config.get("evaluation_event_ids")
    if evaluation_ids is not None:
        if len(evaluation_ids) != len(set(evaluation_ids)) or not set(evaluation_ids) <= {row["event_id"] for row in all_events}:
            raise ValueError("invalid registered evaluation subset")
        selected = set(evaluation_ids)
        events = [row for row in all_events if row["event_id"] in selected]
        labels = [row for row in all_labels if row["event_id"] in selected]
    else:
        events, labels = all_events, all_labels
    predictions_data = read_jsonl(predictions)
    result = evaluate(events, {r["event_id"]: r for r in labels}, predictions_data)
    actual = Counter(r["label_t3"] for r in labels)
    pred_by_id = {r["event_id"]: r for r in predictions_data}
    recalls = []
    for direction in ("up", "down"):
        eligible = [r for r in labels if r["label_t3"] == direction]
        if eligible:
            recalls.append(sum(not pred_by_id.get(r["event_id"], {}).get("abstain", True)
                               and pred_by_id[r["event_id"]].get("strategy_metadata", {}).get("prediction_status") == "available"
                               and pred_by_id[r["event_id"]]["pred_direction"] == direction for r in eligible) / len(eligible))
    result.update(balanced_accuracy=sum(recalls) / 2 if len(recalls) == 2 else None,
                  always_up_accuracy=actual["up"] / len(events), always_down_accuracy=actual["down"] / len(events),
                  dataset_hash=config["dataset_hash"], library_hash=config["library_hash"], version=config["version"],
                  repetition=config.get("repetition"), unique_n=len(events))
    atomic_json(output, result)
    output.chmod(0o600)
    public = summary(result)
    atomic_json(output.with_name("metrics.json"), public)
    return public


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    for arg in ("dataset", "experiment", "predictions", "output"):
        p.add_argument("--" + arg, type=Path, required=True)
    p.add_argument("--split", choices=["learn", "validation", "test"], required=True)
    args = p.parse_args()
    print(json.dumps(score(args.dataset, args.experiment, args.split, args.predictions, args.output), ensure_ascii=False))
