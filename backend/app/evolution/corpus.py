"""Immutable task corpus with separate predictor and feedback views."""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
from pathlib import Path

from ..event_backtest.evolution.rubric import digest


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    temporary.replace(path)


class Corpus:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def record(self, *, task_id: str, split: str, event: dict, prediction: dict, genes: list[dict], trace: dict,
               model_revision: str, generation: int):
        if split not in {"learn", "validation", "test"} or not task_id or len(task_id) > 240:
            raise ValueError("invalid corpus task identity")
        # A corpus record cannot carry Oracle answers or secret connection data.
        forbidden = {"actual_direction", "actual_label", "label_t3", "car_t3", "oracle", "api_key", "secret_env_ref"}
        def check(value):
            if isinstance(value, dict):
                if forbidden.intersection(value) or {str(k).lower().replace('-', '_') for k in value}.intersection({"api_key", "apikey", "authorization", "access_token", "password", "secret_env_ref", "cookie"}):
                    raise ValueError("label or credential material in predictor corpus")
                for item in value.values():
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)
        record = {"type": "PronoiaTask", "schema_version": 1, "task_id": task_id, "split": split,
                  "event": event, "prediction": prediction, "genes": [{"id": g["id"], "asset_id": g["asset_id"]} for g in genes],
                  "trace": trace, "model_revision": model_revision, "generation": generation}
        check(record)
        record["content_hash"] = digest(record)
        filename = hashlib.sha256(task_id.encode()).hexdigest() + ".json"
        path = self.root / "tasks" / split / filename
        with (self.root / ".corpus.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.exists():
                existing = json.loads(path.read_text())
                if existing != record:
                    raise ValueError("task records are immutable")
                return existing
            atomic_json(path, record)
        return record

    def load(self, task_id: str, split: str):
        path = self.root / "tasks" / split / (hashlib.sha256(task_id.encode()).hexdigest() + ".json")
        record = json.loads(path.read_text())
        if record["content_hash"] != digest({k: v for k, v in record.items() if k != "content_hash"}):
            raise ValueError("corpus task content was changed")
        return record

    def learning_feedback(self, task: dict, label: dict, *, label_hash: str):
        if task["split"] != "learn":
            raise ValueError("only learning answers may enter the evolution corpus")
        if label["event_id"] != task["event"]["event_id"]:
            raise ValueError("feedback label belongs to another event")
        observed = label.get("label_available_at")
        if not observed:
            raise ValueError("feedback must have an observable maturity timestamp")
        timestamp = dt.datetime.fromisoformat(observed)
        if timestamp.tzinfo is None:
            raise ValueError("feedback maturity must include timezone")
        if timestamp > dt.datetime.now(dt.timezone.utc):
            raise ValueError("future outcomes cannot enter feedback memory")
        record = {"type": "PronoiaFeedback", "task_id": task["task_id"], "task_hash": task["content_hash"],
                  "label": label, "label_hash": label_hash, "outcome_observed_at": observed,
                  "correct": not task["prediction"].get("abstain", True) and task["prediction"].get("pred_direction") == label["label_t3"]}
        path = self.root / "feedback" / (hashlib.sha256(task["task_id"].encode()).hexdigest() + ".json")
        with (self.root / ".corpus.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.exists() and json.loads(path.read_text()) != record:
                raise ValueError("feedback records are immutable")
            atomic_json(path, record)
        return record

    def feedback_before(self, as_of: str):
        """Historical cases become eligible only after their outcomes were observable."""
        cutoff = dt.datetime.fromisoformat(as_of)
        if cutoff.tzinfo is None:
            raise ValueError("as-of cutoff must include timezone")
        rows = [json.loads(path.read_text()) for path in sorted((self.root / "feedback").glob("*.json"))]
        return [row for row in rows if dt.datetime.fromisoformat(row["outcome_observed_at"]) <= cutoff]
