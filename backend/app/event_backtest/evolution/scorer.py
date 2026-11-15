"""Oracle-owning subprocess. Test labels open only after a five-round lock."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .evaluation import evaluate
from .prepare import read_jsonl, write_json
from .rubric import digest


def score(dataset: Path, experiment: Path, split: str, predictions: Path, output: Path):
    manifest = json.loads((dataset / "manifest.json").read_text())
    events = read_jsonl(dataset / f"{split}.events.jsonl")
    if digest(events) != manifest["hashes"][split]["events"]:
        raise ValueError("frozen events changed")
    if split == "test":
        lock = json.loads((experiment / "final_lock.json").read_text())
        if lock.get("completed_rounds") != 5:
            raise ValueError("test labels sealed until five rounds complete")
        for version, rubric_hash in lock["allowed_versions"].items():
            for repetition in range(lock["test_repetitions"]):
                folder = experiment / "runs" / version / "test" / str(repetition)
                stored = read_jsonl(folder / "predictions.jsonl")
                cfg = json.loads((folder / "config.json").read_text())
                if ({r["event_id"] for r in stored} != {r["event_id"] for r in events}
                        or len(stored) != len(events) or cfg["rubric_hash"] != rubric_hash):
                    raise ValueError("all frozen test predictions must complete before scoring")
        for number in range(1, 6):
            decision = json.loads((experiment / "rounds" / f"{number:02d}" / "decision.json").read_text())
            if decision.get("round") != number:
                raise ValueError("incomplete search ledger")
        config = json.loads((predictions.parent / "config.json").read_text())
        if lock["manifest_hash"] != digest(json.loads((experiment / "manifest.json").read_text())):
            raise ValueError("final experiment manifest changed")
        if config.get("rubric_hash") not in lock["allowed_rubric_hashes"]:
            raise ValueError("test candidate not frozen")
        if config.get("replicate") not in [0, 1, 2]:
            raise ValueError("unregistered test repetition")
    labels = read_jsonl(dataset / "oracle" / f"{split}.labels.jsonl")
    if digest(labels) != manifest["hashes"][split]["labels"]:
        raise ValueError("frozen labels changed")
    metrics = evaluate(events, {r["event_id"]: r for r in labels}, read_jsonl(predictions))
    write_json(output, metrics)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("dataset", "experiment", "predictions", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--split", choices=["learn", "validation", "test"], required=True)
    args = parser.parse_args()
    score(args.dataset, args.experiment, args.split, args.predictions, args.output)
