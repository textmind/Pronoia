"""Resumable GEP experiment: actual tasks -> corpus -> mutations -> measured feedback."""
from __future__ import annotations

import argparse
import asyncio
import copy
import datetime as dt
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from contextlib import nullcontext

from .. import db, settings, config
from ..llm import model_profile_context, complete_json_diagnostic, friendly_runtime_error
from ..model_lab.providers import profile_snapshot, resolve_profile_secret
from ..model_lab.secret_store import store_secret
from ..model_lab.platform_default import is_platform_profile_id
from ..event_backtest.engine import run_team_prompt, run_team_full_one_event, _event_prompt
from ..event_backtest.models import EventRecord
from ..event_backtest.evolution.rubric import digest, BASE_PROMPT
from ..event_backtest.evolution.structured import catalog
from ..event_backtest.evolution.controller_v2 import ATTRIBUTION_V2, MUTATION_V2, balanced_sample, subtype
from ..event_backtest.evolution.prepare import read_jsonl
from .corpus import Corpus, atomic_json
from .policy import GeneLibrary
from .runtime import evolution_context
from .identity import implementation_hash
from .meter import UsageMeter, meter_context


class Experiment:
    def __init__(self, dataset: Path, root: Path, *, rounds=3, concurrency=2, max_tokens=50_000_000,
                 mode="team_prompt", repetitions=2, refresh_count=50, baseline_from: Path | None = None):
        self.dataset, self.root = dataset.resolve(), root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if (not 1 <= rounds <= 20 or not 1 <= concurrency <= 12 or not 100_000 <= max_tokens <= 50_000_000
                or mode not in {"team_prompt", "team_full"} or not 1 <= repetitions <= 5 or not 1 <= refresh_count <= 500):
            raise ValueError("invalid evolution execution limits")
        self.rounds, self.concurrency, self.max_tokens, self.mode = rounds, concurrency, max_tokens, mode
        self.repetitions, self.refresh_count = repetitions, refresh_count
        self.dataset_manifest = json.loads((self.dataset / "manifest.json").read_text())
        self.library = GeneLibrary(self.root / "assets")
        self.corpus = Corpus(self.root / "corpus")
        self.semaphore = asyncio.Semaphore(concurrency)
        self.rate_lock, self.budget_lock = asyncio.Lock(), asyncio.Lock()
        self.next_request, self.reserved = 0.0, 0
        self.meter = UsageMeter(self.root / "request_usage", max_tokens)
        existing = json.loads((self.root / "manifest.json").read_text()) if (self.root / "manifest.json").exists() else {}
        self.baseline_from = Path(baseline_from or existing.get("baseline_from")).resolve() if baseline_from or existing.get("baseline_from") else None
        private = self.root / "model.private.json"
        if private.exists():
            self.profile = json.loads(private.read_text())
        elif self.baseline_from:
            from .baseline import source_manifest
            source_manifest(self.baseline_from)
            self.profile = json.loads((self.baseline_from / "model.private.json").read_text())
            atomic_json(private, self.profile)
            private.chmod(0o600)
        else:
            db.init_db()
            profile = settings.require_model()
            self.profile = profile_snapshot(profile)
            self.profile["secret_env_ref"] = store_secret(resolve_profile_secret(profile))
            if is_platform_profile_id(self.profile.get("id")):
                self.profile["id"] = "evolution:" + self.profile["id"]
            atomic_json(private, self.profile)
            private.chmod(0o600)
        public_model = {k: self.profile.get(k) for k in ("id", "name", "model_id", "provider", "max_output_tokens", "timeout_seconds", "thinking_mode", "configuration_revision", "capabilities")}
        self.model_revision = digest(public_model)
        manifest = {"schema_version": 2, "dataset_hash": digest(self.dataset_manifest), "dataset_path": str(self.dataset),
                    "model": public_model, "model_revision": self.model_revision, "engine": self.library.engine.call("inspect"),
                    "rounds": rounds, "concurrency": concurrency, "max_tokens": max_tokens, "mode": mode,
                    "team_execution_profile": "bounded_full_v2_packet_team" if mode == "team_full" else "structured_prompt_v1",
                    "final_repetitions": repetitions, "accepted_learning_refresh_count": refresh_count,
                    "maximum_worker_wall_seconds": 43200,
                    "baseline_from": str(self.baseline_from) if self.baseline_from else None,
                    "base_prompt_hash": digest(BASE_PROMPT), "selection_max_genes": 4,
                    "implementation_hash": implementation_hash(), "endpoint_hash": digest(self.profile["base_url"]),
                    "validation_minimum_gain": 0, "coverage_tolerance": .02,
                    "max_input_ratio": 1.5, "maximum_audit_rate": .10,
                    "final_pairing": "same 100 unique events; two repeated Team runs per arm; 14-day clustered bootstrap",
                    "test_use": "only after final lock; never used for mutation or promotion"}
        path = self.root / "manifest.json"
        if path.exists() and json.loads(path.read_text()) != manifest:
            raise ValueError("experiment identity changed; resume refused")
        atomic_json(path, manifest)
        self.manifest, self.experiment_hash = manifest, digest(manifest)
        if self.baseline_from:
            from .baseline import validate_source
            validate_source(self)

    def emit(self, **fields):
        value = {"at": dt.datetime.now(dt.timezone.utc).isoformat(), **fields}
        atomic_json(self.root / "status.json", value)
        with (self.root / "progress.jsonl").open("a") as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        print(json.dumps(value, ensure_ascii=False), flush=True)

    def usage(self):
        if self.meter.root.exists():
            return self.meter.total()
        total = 0
        for filename in self.root.glob("runs/*/*/tasks/*.json"):
            p = json.loads(filename.read_text())["prediction"]
            total += int(p.get("tokens_in") or 0) + int(p.get("tokens_out") or 0)
        for filename in self.root.glob("rounds/*/*.usage.json"):
            total += int(json.loads(filename.read_text()).get("total_tokens") or 0)
        return total

    async def throttle(self):
        async with self.rate_lock:
            await asyncio.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + max(0, float(getattr(config, "LLM_RPS_INTERVAL_S", 1.15)))

    def events(self, split):
        events = read_jsonl(self.dataset / f"{split}.events.jsonl")
        if digest(events) != self.dataset_manifest["hashes"][split]["events"]:
            raise ValueError("frozen events changed")
        return events

    async def predict(self, version, library, split, *, baseline=False, repetition=None,
                      event_ids=None, namespace=None, schedule=None):
        folder = self.root / "runs" / version / split
        if repetition is not None:
            folder = folder / f"repeat_{repetition + 1}"
        elif namespace:
            folder = folder / namespace
        folder.mkdir(parents=True, exist_ok=True)
        events = self.events(split)
        if event_ids is not None:
            requested = set(event_ids)
            events = [event for event in events if event["event_id"] in requested]
            if len(events) != len(requested):
                raise ValueError("registered prediction subset contains unknown events")
        identity = {"version": version, "library_hash": library["hash"], "dataset_hash": digest(self.dataset_manifest),
                    "experiment_hash": self.experiment_hash, "repetition": repetition,
                    "evaluation_event_ids": [event["event_id"] for event in events] if event_ids is not None else None}
        if split == "test":
            lock = json.loads((self.root / "final_lock.json").read_text())
            if lock["versions"].get(version) != library["hash"]:
                raise ValueError("unregistered test prediction")
        if (folder / "config.json").exists() and json.loads((folder / "config.json").read_text()) != identity:
            raise ValueError("cached prediction configuration changed")
        atomic_json(folder / "config.json", identity)
        snapshots = await asyncio.to_thread(self.library.engine.call, "snapshot")
        capsules = snapshots["capsules"]
        personality = (snapshots.get("personality") or {}).get("current")

        async def one(event):
            filename = folder / "tasks" / (digest(event["event_id"]) + ".json")
            if filename.exists():
                saved = json.loads(filename.read_text())
                if saved["identity"] != identity or saved["packet_hash"] != digest(event):
                    raise ValueError("cached task belongs to another frozen input")
                return saved["prediction"]
            if schedule:
                await asyncio.sleep(max(0, float(schedule.get(event["event_id"], 0))))
            async with self.semaphore:
                reserve = max(20000, min(400000, int(self.profile["max_output_tokens"] or 65536) * 4))
                async with self.budget_lock:
                    if self.usage() + self.reserved + reserve > self.max_tokens:
                        raise RuntimeError("token budget reached; experiment remains incomplete")
                    self.reserved += reserve
                route = {"genes": [], "rules": [r["rule"] for r in library["records"]], "capsule": None, "signals": [], "selector_decisions": []}
                repeat_tag = f":r{repetition + 1}" if repetition is not None else (f":{namespace}" if namespace else "")
                task_id = f"{self.root.name}:{version}:{split}{repeat_tag}:{event['event_id']}"
                attempt = None
                started = time.monotonic()
                try:
                    request_path = folder / "requests" / filename.name
                    if request_path.exists():
                        request = json.loads(request_path.read_text())
                        if request["identity"] != identity or request["packet_hash"] != digest(event):
                            raise ValueError("frozen task request changed")
                        route, attempt = request["route"], request["attempt"]
                    elif not baseline:
                        route = await asyncio.to_thread(self.library.route, library, event, capsules=capsules)
                        attempt = await asyncio.to_thread(self.library.engine.call, "attempt", signals=route["signals"], gene=route["genes"][0], observations={"task_id": task_id, "split": split})
                    atomic_json(request_path, {"identity": identity, "packet_hash": digest(event), "route": route, "attempt": attempt})
                    await self.throttle()
                    with model_profile_context(self.profile), meter_context(self.meter, task_id=task_id), (evolution_context(
                            route["genes"], route["capsule"], task_id=task_id, library_hash=library["hash"],
                            personality=personality, rules=route.get("rules")) if not baseline else nullcontext()):
                        record = EventRecord.from_dict(event)
                        if self.mode == "team_prompt":
                            predictions = await run_team_prompt([record], run_id=task_id, model_version=self.profile["model_id"],
                                concurrency=1, rubric_rules=route["rules"], rubric_version=library["hash"])
                            prediction = predictions[0].to_dict()
                        else:
                            prediction = (await run_team_full_one_event(record, run_id=task_id, model_version=self.profile["model_id"],
                                trajectory_ckpt_dir=folder / "trajectories", bounded_full=True)).to_dict()
                            prediction.setdefault("strategy_metadata", {}).setdefault("rule_audit", {
                                "valid": not prediction.get("abstain", True), "source": "full_team_output_and_trajectory",
                                "causal_rule_following_proven": False})
                            prediction["strategy_metadata"].setdefault("prediction_status", "available" if not prediction.get("abstain") else "insufficient_data")
                    measured_tokens = self.meter.task_total(task_id)
                    if measured_tokens:
                        prediction["tokens_in"] = max(int(prediction.get("tokens_in") or 0), measured_tokens)
                    prediction["step_ms"] = max(int(prediction.get("step_ms") or 0), int((time.monotonic() - started) * 1000))
                    metadata = prediction.setdefault("strategy_metadata", {})
                    metadata["evolution"] = {"library_hash": library["hash"], "gene_ids": [g["id"] for g in route["genes"]],
                        "gene_asset_ids": [g["asset_id"] for g in route["genes"]], "capsule_id": (route.get("capsule") or {}).get("id"),
                        "selector_decisions": route["selector_decisions"], "action_id": (attempt or {}).get("actionId"),
                        "personality": personality, "repetition": repetition}
                    saved = {"identity": identity, "packet_hash": digest(event), "prediction": prediction, "route": route, "task_id": task_id}
                    self.corpus.record(task_id=task_id, split=split, event=event, prediction=prediction, genes=route["genes"],
                        trace={"routing": metadata["evolution"], "packet_hash": digest(json.loads(_event_prompt(record)))}, model_revision=self.model_revision, generation=library["generation"])
                    atomic_json(filename, saved)
                    self.emit(stage="predicting", version=version, split=split, completed=len(list((folder / "tasks").glob("*.json"))), total=len(events), tokens=self.usage())
                    return prediction
                finally:
                    async with self.budget_lock:
                        self.reserved -= reserve

        workers = [asyncio.create_task(one(event)) for event in events]
        try:
            predictions = await asyncio.gather(*workers)
        except BaseException:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        target = folder / "predictions.jsonl"
        temporary = target.with_suffix(".tmp")
        temporary.write_text("".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in predictions))
        temporary.replace(target)
        return predictions, target

    async def score(self, prediction_path, split):
        output = prediction_path.with_name("metrics.private.json")
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "app.evolution.score", "--dataset", str(self.dataset),
            "--experiment", str(self.root), "--split", split, "--predictions", str(prediction_path), "--output", str(output),
            cwd=Path(__file__).resolve().parents[2], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError("independent scorer rejected run: " + stderr.decode()[-400:])
        return json.loads(stdout)

    async def agent(self, filename, system, payload):
        if filename.exists():
            return json.loads(filename.read_text())
        if (self.root / "final_lock.json").exists():
            raise ValueError("optimizer is closed after the final test lock")
        request = {"system": system, "input": payload}
        atomic_json(filename.with_suffix(".request.json"), request)
        if self.usage() + 250000 > self.max_tokens:
            raise RuntimeError("optimizer token budget reached")
        with model_profile_context(self.profile), meter_context(self.meter):
            # The optimizer must spend its bounded output on the requested
            # JSON.  DeepSeek-compatible gateways can otherwise consume the
            # entire allowance in reasoning_content and return empty content.
            result = await complete_json_diagnostic(
                system,
                json.dumps(payload, ensure_ascii=False),
                max_tokens=16384,
                reasoning_effort="none",
            )
        atomic_json(filename.with_suffix(".usage.json"), result.usage)
        if result.value is None:
            raise ValueError("optimizer returned no usable JSON; request retained for diagnosis")
        atomic_json(filename, result.value)
        return result.value

    def learning_feedback(self, version, library, predictions, *, namespace=None):
        events = {e["event_id"]: e for e in self.events("learn")}
        labels = read_jsonl(self.dataset / "oracle/learn.labels.jsonl")
        if digest(labels) != self.dataset_manifest["hashes"]["learn"]["labels"]:
            raise ValueError("learning Oracle changed")
        predictions = {p["event_id"]: p for p in predictions}
        wrong, right, feedback_items = [], [], []
        for label in labels:
            if label["event_id"] not in predictions:
                continue
            event, pred = events[label["event_id"]], predictions[label["event_id"]]
            task_id = f"{self.root.name}:{version}:learn{':' + namespace if namespace else ''}:{event['event_id']}"
            task = self.corpus.load(task_id, "learn")
            feedback = self.corpus.learning_feedback(task, label, label_hash=self.dataset_manifest["hashes"]["learn"]["labels"])
            example = {"event_id": event["event_id"], "group_id": event.get("group_id", event["event_id"]), "date": event["event_time"],
                       "packet": json.loads(_event_prompt(EventRecord.from_dict(event))), "actual_direction": label["label_t3"],
                       "prediction": {k: pred.get(k) for k in ("pred_direction", "confidence", "rationale", "abstain")},
                       "correct": bool(feedback["correct"]), "audit_errors": pred.get("strategy_metadata", {}).get("rule_audit", {}).get("errors", [])}
            (right if example["correct"] else wrong).append(example)
            task_root = self.root / "runs" / version / "learn"
            if namespace:
                task_root /= namespace
            saved = json.loads((task_root / "tasks" / (digest(event["event_id"]) + ".json")).read_text())
            genes = saved["route"]["genes"]
            if not genes:
                continue
            capsule = None
            identity = digest(task_id)[:24]
            if example["correct"]:
                capsule = {"type": "Capsule", "id": "capsule_case_" + identity, "trigger": saved["route"]["signals"], "gene": genes[0]["id"],
                    "summary": event["event_type_l2"] + "；同类事前推理案例（单例，不代表策略整体改善）", "confidence": min(.7, max(0., float(pred.get("confidence") or .5))),
                    "blast_radius": {"files": 0, "lines": 0}, "outcome": {"status": "success", "score": 1.0},
                    "content": {"source_split": "learn", "event_id": event["event_id"], "event_time": event["event_time"],
                                "outcome_observed_at": label["label_available_at"], "facts": event["event_text"][:700],
                                "reasoning": str(pred.get("rationale") or "")[:500], "known_case_result": label["label_t3"],
                                "evidence_scope": "one historical learning case; not a policy improvement claim"},
                    "execution_trace": [{"stage": "validate", "task_id": task_id, "task_hash": task["content_hash"], "n": 1, "correct": 1}]}
            event_asset = {"type": "EvolutionEvent", "id": "event_case_" + identity, "intent": "optimize", "signals": saved["route"]["signals"],
                "genes_used": [g["id"] for g in genes], "mutation_id": "case_observation_" + identity, "blast_radius": {"files": 0, "lines": 0},
                "outcome": {"status": "success" if example["correct"] else "failed", "score": float(example["correct"])}, "source_type": "generated",
                "capsule_id": capsule["id"] if capsule else None,
                "meta": {"task_id": task_id, "split": "learn", "evidence_scope": "case_outcome", "memory_eligible": False,
                         "attribution": "co-used Genes; case correctness is not causal Gene credit"}}
            feedback_items.append({"event": event_asset, "gene": genes[0], "capsule": capsule,
                                   "action_id": (pred.get("strategy_metadata", {}).get("evolution") or {}).get("action_id"), "memory_eligible": False})
        if feedback_items:
            self.library.engine.call("feedback_batch", items=feedback_items)
        errors = balanced_sample(wrong)
        controls, used = [], set()
        for error in errors:
            pool = sorted((r for r in right if r["event_id"] not in used), key=lambda r: (subtype(r) != subtype(error), r["actual_direction"] != error["actual_direction"], r["event_id"]))
            if pool:
                controls.append(pool[0]); used.add(pool[0]["event_id"])
        return errors + controls

    async def propose(self, round_number, parent, examples, current_metrics, best_metrics):
        folder = self.root / "rounds" / f"{round_number:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        patterns = await self.agent(folder / "attribution.json", ATTRIBUTION_V2,
            {"rules": [r["rule"] for r in parent["records"]], "learning_examples": examples,
             "catalog": catalog(), "learning_population": {"n": len(self.events("learn")), "sampled_examples": len(examples)}})
        by_id = {e["event_id"]: e for e in examples}
        valid = []
        for pattern in patterns.get("patterns", []):
            failures = [by_id[e] for e in pattern.get("event_ids", []) if e in by_id and not by_id[e]["correct"]]
            # The attribution prompt asks for event IDs, but models often add
            # a parenthetical explanation (for example,
            # ``evo_cn_...（日期，原因）``). Resolve only IDs already present in
            # the sampled examples so explanatory text cannot introduce new
            # evidence or bypass the frozen learning sample.
            controls = []
            for raw_control in pattern.get("counterexamples", []):
                resolved = self._resolve_example_id(raw_control, by_id)
                if resolved is not None:
                    controls.append(resolved)
            if (len({e["date"] for e in failures}) >= 3 and len({e["group_id"] for e in failures}) >= 3
                    and controls and all(e in by_id and by_id[e]["correct"] for e in controls)):
                valid.append(pattern)
        atomic_json(folder / "patterns.validated.json", valid)
        if not valid:
            return None, {"reason": "insufficient_repeated_learning_evidence", "patterns": 0}
        previous = []
        for filename in sorted((self.root / "rounds").glob("*/candidate.json")):
            if int(filename.parent.name) < round_number:
                old = json.loads(filename.read_text())
                previous.append({"round": int(filename.parent.name), "rules": [r["rule"] for r in old["records"]],
                                 "validation_summary": json.loads((self.root / "runs" / f"g{int(filename.parent.name)}" / "validation/metrics.json").read_text())})
        payload = {"rules": [r["rule"] for r in parent["records"]], "patterns": valid, "catalog": catalog(),
                   "validation_summary": current_metrics, "best_validation_summary": best_metrics,
                   "previous_candidates": previous, "instruction": "Do not re-propose an unchanged or previously evaluated policy. Address measured failures with a distinct supported change.",
                   "used_rule_ids": [r["rule"]["id"] for r in parent["records"]]}
        forbidden = {e["event_id"] for e in examples}
        for attempt in range(3):
            proposal = await self.agent(folder / ("proposal.json" if attempt == 0 else f"proposal.repair{attempt}.json"), MUTATION_V2, payload)
            if not proposal.get("edits"):
                return None, {"reason": "no_supported_mutation", "patterns": len(valid)}
            try:
                candidate = self.library.evolve(parent, proposal, patterns=valid, forbidden_tokens=forbidden)
                rules_hash = digest([r["rule"] for r in candidate["records"]])
                if rules_hash == digest([r["rule"] for r in parent["records"]]) or any(rules_hash == digest(p["rules"]) for p in previous):
                    raise ValueError("unchanged or already evaluated policy; propose a distinct evidence-supported change")
                mutation = self.library.engine.seal({"type": "Mutation", "id": "mutation_" + candidate["hash"][:24], "category": "optimize",
                    "trigger_signals": [p["id"] for p in valid], "target": parent["hash"], "expected_effect": "Improve fixed-denominator validation accuracy without coverage or cost regression", "risk_level": "low"})
                atomic_json(folder / "mutation.json", mutation)
                atomic_json(folder / "candidate.json", candidate)
                return candidate, {"mutation": mutation, "proposal": proposal, "patterns": len(valid)}
            except (ValueError, TypeError, KeyError) as exc:
                payload = {**payload, "previous_proposal": proposal, "validation_error": str(exc)}
        return None, {"reason": "mutation_validation_failed", "patterns": len(valid)}

    @staticmethod
    def _resolve_example_id(value, examples):
        """Resolve a model-reported counterexample to a sampled event ID."""
        if isinstance(value, dict):
            value = value.get("event_id") or value.get("id")
        if not isinstance(value, str):
            return None
        if value in examples:
            return value
        # Prefer longer IDs first in case a fixture contains a shared prefix.
        return next((event_id for event_id in sorted(examples, key=len, reverse=True) if event_id in value), None)

    def decision(self, candidate, best, baseline):
        if candidate["n"] != best["n"] or candidate["n"] != baseline["n"]:
            raise ValueError("candidate evaluation universe changed")
        reasons = []
        if candidate["coverage"] < baseline["coverage"] - .02:
            reasons.append("coverage_regression")
        if candidate["missing_predictions"]:
            reasons.append("missing_predictions")
        if candidate["audit_violation_rate"] > baseline["audit_violation_rate"] + .02:
            reasons.append("audit_regression")
        if candidate["mean_input_tokens"] > baseline["mean_input_tokens"] * 1.5:
            reasons.append("context_cost_regression")
        if candidate["correct"] <= best["correct"]:
            reasons.append("no_measured_accuracy_gain")
        return {"accepted": not reasons, "reasons": reasons, "candidate_correct": candidate["correct"], "best_correct": best["correct"], "n": candidate["n"]}

    def prepare_round_cycle(self, candidate, parent, mutation, *, version):
        changed = [r["gene"] for r in candidate["records"] if r["gene"]["id"] not in {p["gene"]["id"] for p in parent["records"]}]
        selected = changed[0] if changed else candidate["records"][0]["gene"]
        folder = self.root / "rounds" / f"{int(version.removeprefix('g')):02d}"
        receipt_path = folder / "cycle.receipt.json"
        task_hash = digest({"version": version, "candidate": candidate["hash"], "mutation": mutation["id"],
                            "validation": self.dataset_manifest["hashes"]["validation"]})
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if receipt.get("task_hash") != task_hash or receipt.get("gene_asset_id") != selected["asset_id"]:
                raise ValueError("prepared evolution cycle changed")
            return receipt
        receipt = self.library.engine.prepare_cycle(
            cycle_id=f"{self.root.name}_{version}_validation",
            signals=["financial_research", "measured_validation_candidate", *mutation.get("trigger_signals", [])],
            forced_gene_id=selected["asset_id"], experiment_hash=self.experiment_hash,
            dataset_hash=self.manifest["dataset_hash"], model_hash=self.model_revision,
            task_hash=task_hash, library_hash=candidate["hash"])
        atomic_json(receipt_path, receipt)
        return receipt

    def round_feedback(self, candidate, parent, metric, decision, mutation, *, version):
        changed = [r["gene"] for r in candidate["records"] if r["gene"]["id"] not in {p["gene"]["id"] for p in parent["records"]}]
        memory_eligible = bool(changed)
        if hasattr(self, "model_revision"):
            receipt = self.prepare_round_cycle(candidate, parent, mutation, version=version)
            commit_path = self.root / "rounds" / f"{int(version.removeprefix('g')):02d}" / "cycle.commit.json"
            execution = {"experiment_hash": self.experiment_hash, "dataset_hash": self.manifest["dataset_hash"],
                "model_hash": self.model_revision, "task_hash": receipt["task_hash"], "library_hash": candidate["hash"],
                "status": "success" if decision["accepted"] else "failed", "score": metric["score"], "n": metric["n"],
                "tokens": int(metric.get("total_tokens") or metric.get("tokens_in", 0) + metric.get("tokens_out", 0)),
                "duration_ms": int(metric.get("total_step_ms") or 0), "strong_evidence": bool(decision["accepted"] and not metric["missing_predictions"]),
                "memory_eligible": memory_eligible, "task_id": f"{self.root.name}:{version}:validation"}
            committed = self.library.engine.commit_cycle(receipt, execution,
                problem_signature="pronoia_validation_policy_error", target=parent["hash"],
                expected_effect="improve fixed-denominator validation accuracy without coverage or cost regression",
                summary=f"{metric['correct']}/{metric['n']} validation predictions correct")
            if commit_path.exists() and json.loads(commit_path.read_text()) != committed:
                raise ValueError("committed evolution cycle changed")
            atomic_json(commit_path, committed)
            return committed
        # Compatibility path for isolated unit fixtures that predate v2 receipts.
        if not changed:
            changed = [candidate["records"][0]["gene"]]
        event_id = "event_round_" + digest([self.experiment_hash, version, candidate["hash"]])[:24]
        capsule = None
        if decision["accepted"]:
            capsule = {"type": "Capsule", "id": "capsule_round_" + event_id.removeprefix("event_round_"), "trigger": ["financial_research", "measured_validation_gain"],
                "gene": changed[0]["id"], "summary": "Measured validation improvement of financial Gene library; final test not yet evaluated", "confidence": float(metric["score"]),
                "blast_radius": {"files": 0, "lines": 0}, "outcome": {"status": "success", "score": metric["score"]},
                "execution_trace": [{"stage": "validate", "dataset_hash": self.manifest["dataset_hash"], "n": metric["n"], "correct": metric["correct"]}],
                "content": {"source_split": "validation_aggregate", "library_hash": candidate["hash"], "decision": decision}}
        event = {"type": "EvolutionEvent", "id": event_id, "intent": "optimize", "signals": ["financial_research"], "genes_used": [g["id"] for g in changed],
            "mutation_id": mutation["id"], "blast_radius": {"files": 0, "lines": 0}, "outcome": {"status": "success" if decision["accepted"] else "failed", "score": metric["score"]},
            "source_type": "generated", "capsule_id": capsule["id"] if capsule else None,
            "meta": {"evaluation_scope": "validation_aggregate", "dataset_hash": self.manifest["dataset_hash"], "n": metric["n"], "decision": decision, "library_hash": candidate["hash"], "version": version,
                     "memory_eligible": memory_eligible, "attribution": "changed Gene co-use" if memory_eligible else "policy removal; no single-Gene credit"}}
        self.library.engine.call("feedback", gene=changed[0], event=event, capsule=capsule, action_id="act_" + event_id, memory_eligible=memory_eligible)

    @staticmethod
    def aggregate_repetitions(rows):
        if not rows or len({row["n"] for row in rows}) != 1:
            raise ValueError("repeated evaluation universes changed")
        mean = lambda key: sum(float(row.get(key) or 0) for row in rows) / len(rows)
        total_high = sum(row.get("high_confidence_n", 0) for row in rows)
        wrong_high = sum(row.get("high_confidence_wrong", 0) for row in rows)
        n = rows[0]["n"]
        tokens_in = sum(row.get("tokens_in", 0) for row in rows)
        tokens_out = sum(row.get("tokens_out", 0) for row in rows)
        step_ms = sum(row.get("total_step_ms", 0) for row in rows)
        return {"n": n, "unique_n": n, "repetitions": len(rows), "prediction_runs": n * len(rows),
                "correct": mean("correct"), "score": mean("score"), "coverage": mean("coverage"),
                "available": mean("available"), "answered_accuracy": mean("answered_accuracy"),
                "balanced_accuracy": mean("balanced_accuracy"), "always_up_accuracy": mean("always_up_accuracy"),
                "always_down_accuracy": mean("always_down_accuracy"),
                "missing_predictions": sum(row.get("missing_predictions", 0) for row in rows),
                "high_confidence_n": total_high, "high_confidence_wrong": wrong_high,
                "high_confidence_error_rate": wrong_high / total_high if total_high else None,
                "audit_violation_rate": mean("audit_violation_rate"), "tokens_in": tokens_in, "tokens_out": tokens_out,
                "total_tokens": tokens_in + tokens_out, "mean_input_tokens": tokens_in / (n * len(rows)),
                "total_step_ms": step_ms, "mean_step_ms": step_ms / (n * len(rows)),
                "version": rows[0]["version"], "library_hash": rows[0]["library_hash"],
                "dataset_hash": rows[0]["dataset_hash"]}

    async def run(self):
        from .report import render_report
        lockfile = (self.root / ".experiment.lock").open("a")
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.emit(stage="initializing", dataset=self.dataset.name, mode=self.mode)
            initial = self.library.compile()
            from .baseline import archive_protocol, seed_baseline
            archive_protocol(self.root)
            if self.baseline_from:
                seed_baseline(self, initial)
            base_preds, base_path = await self.predict("baseline", initial, "learn", baseline=True)
            baseline_learning = await self.score(base_path, "learn")
            _, base_validation_path = await self.predict("baseline", initial, "validation", baseline=True)
            baseline = await self.score(base_validation_path, "validation")
            if baseline["coverage"] < .9:
                raise RuntimeError("baseline output coverage below 90%; repair model/data before evolution")
            baseline_examples = self.learning_feedback("baseline", initial, base_preds)
            current_preds, path = await self.predict("g0", initial, "learn")
            learning = await self.score(path, "learn")
            examples = self.learning_feedback("g0", initial, current_preds)
            _, path = await self.predict("g0", initial, "validation")
            metric = await self.score(path, "validation")
            best, best_version, best_metric = initial, "g0", metric
            evolved_best, evolved_version, evolved_metric = initial, "g0", metric
            # Baseline remains a valid incumbent; integration is not assumed to improve it.
            if self.decision(metric, baseline, baseline)["accepted"] is False:
                best, best_version, best_metric = initial, "baseline", baseline
            history = [{"round": 0, "version": "g0", "library_hash": initial["hash"], "learning": learning, "validation": metric,
                        "accepted": best_version == "g0", "best_version": best_version, "best_accuracy": best_metric["score"]}]
            current, current_metric = initial, metric
            refresh_ids = [event["event_id"] for event in sorted(self.events("learn"),
                key=lambda event: digest(["accepted-learning-refresh-v1", self.experiment_hash, event["event_id"]]))[:self.refresh_count]]
            for round_number in range(1, self.rounds + 1):
                self.emit(stage="generating_gene", round=round_number, best_version=best_version, best_accuracy=best_metric["score"])
                candidate, proposal = await self.propose(round_number, current, examples or baseline_examples, current_metric, best_metric)
                if candidate is None:
                    history.append({"round": round_number, "version": f"g{round_number}", "skipped": proposal["reason"], "accepted": False,
                                    "best_version": best_version, "best_accuracy": best_metric["score"]})
                else:
                    version = f"g{round_number}"
                    self.prepare_round_cycle(candidate, current, proposal["mutation"], version=version)
                    _, path = await self.predict(version, candidate, "validation")
                    candidate_metric = await self.score(path, "validation")
                    decision = self.decision(candidate_metric, best_metric, baseline)
                    evolved_decision = self.decision(candidate_metric, evolved_metric, baseline)
                    if evolved_decision["accepted"]:
                        evolved_best, evolved_version, evolved_metric = candidate, version, candidate_metric
                    self.round_feedback(candidate, current, candidate_metric, decision, proposal["mutation"], version=version)
                    candidate_learning, candidate_examples = None, examples
                    if decision["accepted"]:
                        preds, refresh_path = await self.predict(version, candidate, "learn", event_ids=refresh_ids, namespace="accepted_refresh")
                        candidate_learning = await self.score(refresh_path, "learn")
                        candidate_examples = self.learning_feedback(version, candidate, preds, namespace="accepted_refresh")
                        best, best_version, best_metric = candidate, version, candidate_metric
                        current, current_metric, examples = candidate, candidate_metric, candidate_examples
                    history.append({"round": round_number, "version": version, "library_hash": candidate["hash"], "learning": candidate_learning,
                                    "validation": candidate_metric, "accepted": decision["accepted"], "decision": decision,
                                    "best_version": best_version, "best_accuracy": best_metric["score"]})
                atomic_json(self.root / "history.json", history)
                render_report(self.root, baseline=baseline, history=history)
            final_lock = {"experiment_hash": self.experiment_hash,
                          "versions": {"baseline": initial["hash"], best_version: best["hash"], evolved_version: evolved_best["hash"]},
                          "chosen": best_version, "evolved_reference": evolved_version, "repetitions": self.repetitions,
                          "unique_test_n": len(self.events("test")),
                          "selection_metric": "validation fixed-denominator accuracy", "history_hash": digest(history),
                          "test_order_seed": digest([self.experiment_hash, "paired-team-full-test-order-v1"])}
            lock_path = self.root / "final_lock.json"
            if lock_path.exists() and json.loads(lock_path.read_text()) != final_lock:
                raise ValueError("final test lock cannot be changed")
            atomic_json(lock_path, final_lock)
            self.emit(stage="sealed_test", best_version=best_version)
            policies = {"baseline": initial, evolved_version: evolved_best}
            paths = {version: [] for version in policies}
            order_records = []
            test_events = self.events("test")
            for repetition in range(self.repetitions):
                ordered_events = sorted(test_events, key=lambda event: digest([self.experiment_hash, "sample", repetition, event["event_id"]]))
                flattened = []
                for event in ordered_events:
                    arms = sorted(policies, key=lambda version: digest([self.experiment_hash, "arm", repetition, event["event_id"], version]))
                    flattened.extend((version, event["event_id"]) for version in arms)
                schedules = {version: {} for version in policies}
                for rank, (version, event_id) in enumerate(flattened):
                    schedules[version][event_id] = rank * .002
                    order_records.append({"repetition": repetition + 1, "rank": rank, "version": version, "event_id": event_id})
                pair_runs = await asyncio.gather(*(self.predict(version, policy, "test", baseline=version == "baseline",
                    repetition=repetition, schedule=schedules[version]) for version, policy in policies.items()))
                for version, (_, prediction_path) in zip(policies, pair_runs):
                    paths[version].append(prediction_path)
            atomic_json(self.root / "test_order.json", {"seed": final_lock["test_order_seed"], "items": order_records})
            repeated = {version: [await self.score(path, "test") for path in version_paths] for version, version_paths in paths.items()}
            tests = {version: self.aggregate_repetitions(rows) for version, rows in repeated.items()}
            from ..event_backtest.evolution.controller_v2 import paired_comparison
            baseline_private = [json.loads(path.with_name("metrics.private.json").read_text()) for path in paths["baseline"]]
            evolved_private = [json.loads(path.with_name("metrics.private.json").read_text()) for path in paths[evolved_version]]
            paired = paired_comparison(baseline_private, evolved_private)
            official_pairs = []
            for before, after in zip(baseline_private, evolved_private):
                by_id = {row["event_id"]: row for row in before["outcomes"]}
                official_pairs.extend({"baseline_pass": by_id[row["event_id"]]["correct"], "evolved_pass": row["correct"]}
                                      for row in after["outcomes"])
            official_benchmark = self.library.engine.benchmark(official_pairs)
            base_test, evolved_test = tests["baseline"], tests[evolved_version]
            token_ok = evolved_test["total_tokens"] <= base_test["total_tokens"] * 1.5 if base_test["total_tokens"] else evolved_test["total_tokens"] == 0
            gates = {"accuracy_gain": evolved_test["score"] > base_test["score"],
                     "clustered_ci_above_zero": paired["independent_blocks"] >= 5 and paired["paired_14_day_block_bootstrap_95ci"][0] > 0,
                     "no_missing_predictions": not base_test["missing_predictions"] and not evolved_test["missing_predictions"],
                     "coverage_within_2pp": evolved_test["coverage"] >= base_test["coverage"] - .02,
                     "audit_rate_at_most_10pct": evolved_test["audit_violation_rate"] <= .10,
                     "test_tokens_within_1_5x": token_ok}
            improvement_proven = all(gates.values())
            constant_accuracy = max(base_test["always_up_accuracy"], base_test["always_down_accuracy"])
            result = {"baseline_validation": baseline, "history": history, "test": tests, "chosen": best_version, "evolved_reference": evolved_version,
                      "test_repetitions": repeated, "paired_test_comparison": paired, "official_evolver_benchmark": official_benchmark,
                      "quality_gates": gates, "tokens": self.usage(), "experiment_hash": self.experiment_hash,
                      "unique_test_n": len(test_events), "repetitions": self.repetitions,
                      "beats_constant_direction": evolved_test["score"] > constant_accuracy,
                      "improvement_proven": improvement_proven,
                      "activation_eligible": bool(improvement_proven and best_version == evolved_version)}
            atomic_json(self.root / "result.json", result)
            render_report(self.root, baseline=baseline, history=history, final=result)
            self.emit(stage="complete", chosen=best_version, test_accuracy=tests[best_version]["score"], improvement_proven=result["improvement_proven"])
            return result
        finally:
            lockfile.close()


async def main(args):
    experiment = Experiment(args.dataset, args.out, rounds=args.rounds, concurrency=args.concurrency, max_tokens=args.max_tokens,
                            mode=args.mode, repetitions=args.repetitions, refresh_count=args.refresh_count,
                            baseline_from=args.baseline_from)
    try:
        await asyncio.wait_for(experiment.run(), timeout=43200)
    except Exception as exc:
        try:
            with model_profile_context(experiment.profile):
                message = friendly_runtime_error(exc)
        except Exception:
            message = "任务未完成，冻结的模型连接暂不可用。"
        experiment.emit(stage="interrupted", error_type=type(exc).__name__, message=message)
        raise SystemExit(1) from None


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--max-tokens", type=int, default=50_000_000)
    p.add_argument("--mode", choices=["team_prompt", "team_full"], default="team_full")
    p.add_argument("--repetitions", type=int, default=2)
    p.add_argument("--refresh-count", type=int, default=50)
    p.add_argument("--baseline-from", type=Path)
    asyncio.run(main(p.parse_args()))
