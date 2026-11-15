"""Financial Gene libraries, official routing, and temporally eligible case recall."""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from ..event_backtest.evolution.structured import validate_structured, textual_rules, INITIAL_STRUCTURED_RULES
from ..event_backtest.evolution.rubric import digest, mutate
from .corpus import atomic_json
from .gep import EvoMap

DOMAIN_SIGNALS = {
    "profit_change": ["利润", "earnings", "profit_change"],
    "no_expectations": ["预期", "expectation", "no_expectations"],
    "one_off_gain": ["非经常", "一次性", "one_off_gain"],
    "one_off_loss": ["减值", "非经常", "one_off_loss"],
    "operating_change": ["主营", "经营", "operating_change"],
    "incremental_disclosure": ["修正", "更正", "incremental_disclosure"],
    "same_direction_price": ["price_available", "价格", "same_direction_price"],
    "opposite_direction_price": ["price_available", "价格", "opposite_direction_price"],
    "conflicting_evidence": ["conflicting_evidence", "矛盾"],
    "long_term_only": ["horizon", "长期", "long_term_only"],
    "common_market_factor": ["benchmark", "行业", "common_market_factor"],
    "single_evidence": ["single_evidence", "证据"],
    "loss_improving": ["减亏", "扭亏", "loss_improving"],
    "profit_declining": ["预减", "下降", "profit_declining"],
    "extreme_yoy": ["基数", "extreme_yoy"],
}


class GeneLibrary:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.engine = EvoMap(self.root / "runtime")

    def compile(self, rules=None, *, generation=0, parent=None, evidence=None):
        rules = copy.deepcopy(INITIAL_STRUCTURED_RULES if rules is None else rules)
        validate_structured(rules)
        records = []
        for structured, text in zip(rules, textual_rules(rules)):
            condition = structured["condition"]["code"]
            identifier = f"gene_pronoia_{structured['id'].lower()}_{digest(structured)[:16]}"
            asset = self.engine.seal({"type": "Gene", "id": identifier, "category": "optimize",
                "signals_match": DOMAIN_SIGNALS.get(condition, [condition]) + ["financial_research"],
                "summary": text["condition"], "strategy": [text["condition"] + "：" + text["action"]],
                "constraints": {"max_files": 1, "forbidden_paths": ["oracle", "test.labels.jsonl", "validation.labels.jsonl", ".env"]},
                "validation": ["pronoia:asof_packet", "pronoia:fixed_denominator_accuracy", "pronoia:heldout_quality_gate"]})
            records.append({"gene": asset, "rule": structured})
        library = {"generation": generation, "parent": parent, "evidence": evidence or [], "records": records}
        library["hash"] = digest(library)
        path = self.root / "libraries" / (library["hash"] + ".json")
        if path.exists() and json.loads(path.read_text()) != library:
            raise ValueError("Gene library identity changed")
        atomic_json(path, library)
        self.engine.install([r["gene"] for r in records])
        return library

    def evolve(self, parent, proposal, *, patterns, forbidden_tokens=()):
        allowed = {p["id"] for p in patterns}
        for edit in proposal.get("edits", []):
            if not edit.get("pattern_ids") or set(edit["pattern_ids"]) - allowed:
                raise ValueError("mutation must cite measured learning patterns")
        retired = {r["rule"]["id"] for path in (self.root / "libraries").glob("*.json")
                   for r in json.loads(path.read_text())["records"]}
        rules = mutate([r["rule"] for r in parent["records"]], proposal,
                       retired_ids=retired, forbidden_tokens=set(forbidden_tokens))
        return self.compile(rules, generation=parent["generation"] + 1, parent=parent["hash"], evidence=sorted(allowed))

    def route(self, library, event, *, capsules=(), max_genes=4):
        text = str(event.get("title", "")) + "\n" + str(event.get("event_text", ""))
        # Upstream extraction is supplemented with deterministic domain signals,
        # derived solely from the actual input packet, never from its outcome.
        signals = self.engine.call("extract", corpus=text)["signals"] + ["financial_research", "earnings", "benchmark", "horizon"]
        if "利润" in text:
            signals.append("profit_change")
        if any(term in text for term in ("下降", "预减", "首亏", "略减")):
            signals.append("profit_declining")
        if any(term in text for term in ("减亏", "扭亏")):
            signals.append("loss_improving")
        if any(term in text for term in ("非经常", "减值", "处置")):
            signals += ["one_off_gain", "one_off_loss"]
        if not (event.get("pre_event_features") or {}).get("consensus_expectation"):
            signals.append("no_expectations")
        if event.get("pre_event_features"):
            signals.append("price_available")
        signals = sorted(set(signals))
        as_of = str(event.get("available_time") or event["event_time"])
        cutoff = dt.datetime.fromisoformat(as_of)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=ZoneInfo("America/New_York" if event.get("market") == "US" else "Asia/Shanghai"))
        eligible = []
        for capsule in capsules:
            content = capsule.get("content") or {}
            try:
                observed = dt.datetime.fromisoformat(content["outcome_observed_at"])
            except (KeyError, ValueError, TypeError):
                continue
            if observed.tzinfo is not None and observed <= cutoff and content.get("source_split") == "learn" and content.get("event_id") != event["event_id"]:
                eligible.append(capsule)
        remaining = {r["gene"]["id"]: r for r in library["records"]}
        selected, decisions, recalled = [], [], None
        for _ in range(min(max_genes, len(remaining))):
            choice = self.engine.select(signals, genes=[r["gene"] for r in remaining.values()], capsules=eligible)
            gene = choice.get("selectedGene")
            if not gene:
                break
            selected.append(remaining.pop(gene["id"]))
            decisions.append(choice["selector"])
            # The exact upstream key is stored as well for audit/compatibility.
            possible = choice.get("selectedCapsule") or choice.get("capsule")
            if isinstance(possible, dict):
                recalled = possible
        if not selected:
            raise ValueError("No eligible Gene selected; inspect memory suppression and signals")
        return {"genes": [r["gene"] for r in selected], "rules": [r["rule"] for r in selected], "capsule": recalled,
                "signals": signals, "selector_decisions": decisions, "eligible_capsules": len(eligible), "library_hash": library["hash"]}
