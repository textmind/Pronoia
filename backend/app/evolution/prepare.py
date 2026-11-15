"""Fresh temporal holdout, issuer separation, and immutable as-of packets."""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd

from ..event_backtest.evolution.prepare import candidate_events, _ordered, _label, read_jsonl, write_jsonl
from ..event_backtest.evolution.rubric import digest
from ..event_backtest.collector import _call_with_timeout
from ..market_runtime import initialize_native_runtime
from ..skills.price_data import fetch_price_frame
from .corpus import atomic_json
from .gep import ROOT


def progress(out, **value):
    record = {"at": dt.datetime.now(dt.timezone.utc).isoformat(), **value}
    with (out / "progress.jsonl").open("a") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    atomic_json(out / "status.json", record)
    print(json.dumps(record, ensure_ascii=False), flush=True)


def prepare(out: Path, *, counts=(150, 60, 100), as_of="2026-11-12", workers=3):
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    final = out / "manifest.json"
    if final.exists():
        saved = json.loads(final.read_text())
        for split in ("learn", "validation", "test"):
            if digest(read_jsonl(out / f"{split}.events.jsonl")) != saved["hashes"][split]["events"]:
                raise ValueError("frozen dataset changed")
        progress(out, stage="complete", reused=True, counts=saved["counts"])
        return saved
    if any(type(n) is not int or not 1 <= n <= 2000 for n in counts):
        raise ValueError("invalid dataset size")
    cutoff = dt.date.fromisoformat(as_of)
    if cutoff > dt.datetime.now(ZoneInfo("Asia/Shanghai")).date():
        raise ValueError("future observations cannot be collected")
    initialize_native_runtime()
    raw_root = out / "sources" / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    progress(out, stage="collecting", as_of=as_of)
    for original in (ROOT / "data/self_evolving_sources/raw").glob("yjyg_*.csv"):
        target = raw_root / original.name
        if not target.exists():
            shutil.copy2(original, target)
    receipts = {}
    for period in ("20260630", "20260930"):
        target = raw_root / f"yjyg_{period}.csv"
        if not target.exists():
            frame = _call_with_timeout(lambda: ak.stock_yjyg_em(date=period), timeout_s=120, label="evomap-disclosures-" + period)
            if frame is None or frame.empty:
                raise RuntimeError("Could not fetch earnings disclosures for " + period)
            temporary = target.with_suffix(".tmp")
            frame.to_csv(temporary, index=False, encoding="utf-8-sig")
            temporary.replace(target)
        receipts[target.name] = {"sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "provider": "Eastmoney/akshare.stock_yjyg_em", "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        progress(out, stage="source_ready", period=period)
    atomic_json(out / "sources/receipts.json", receipts)
    events, rejections = candidate_events(out / "sources")
    seen_events, seen_groups, seen_pairs, seen_symbols = set(), set(), set(), set()
    prior_files = set((ROOT / "data").rglob("*.events.jsonl"))
    for name in ("self_evolving_500_v1", "self_evolving_500_v2", "samples", "ab_prompt_compression_10", "ab_prompt_compression_20"):
        prior_files.update((ROOT / "data" / name).glob("*.jsonl"))
    for filename in sorted(prior_files):
        if filename.is_relative_to(out) or "label" in filename.name:
            continue
        for row in read_jsonl(filename):
            if row.get("event_id"):
                seen_events.add(row["event_id"])
            if row.get("group_id"):
                seen_groups.add(row["group_id"])
            if row.get("symbol"):
                seen_symbols.add(row["symbol"])
            if row.get("symbol") and row.get("event_time"):
                seen_pairs.add((row["symbol"], row["event_time"][:10]))
    events = [e for e in events if e["event_id"] not in seen_events and e["group_id"] not in seen_groups
              and e["symbol"] not in seen_symbols and (e["symbol"], e["event_time"][:10]) not in seen_pairs]
    raw_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in raw_root.glob("yjyg_*.csv")}
    for event in events:
        provenance = event["source_provenance"]
        filename = Path(provenance["file"]).name
        provenance.pop("archive_commit", None)
        provenance["raw_file_sha256"] = raw_hashes[filename]
        provenance["capture_kind"] = "downloaded_source" if filename in receipts else "prior_raw_archive"
    windows = [("learn", "2024-01-01", "2024-11-30", counts[0]), ("validation", "2025-01-01", "2025-11-30", counts[1]),
               ("test", "2026-07-01", "2026-10-31", counts[2])]
    reserved = {e["symbol"] for e in events if windows[2][1] <= e["event_time"] <= windows[2][2]}
    price_root = out / "sources/prices"
    price_root.mkdir(exist_ok=True)

    def prices(symbol, _root=None, *, benchmark=False):
        filename = price_root / (symbol + ".json")
        if filename.exists():
            values = json.loads(filename.read_text())
            return pd.Series({dt.date.fromisoformat(k): v for k, v in values.items()}).sort_index()
        frame = fetch_price_frame(symbol, "2023-10-01", as_of, market="CN", adjust="qfq").frame
        if frame is None or frame.empty:
            return None
        series = pd.Series(pd.to_numeric(frame["close"], errors="coerce").values, index=pd.to_datetime(frame["date"]).dt.date).dropna()
        series = series[~series.index.duplicated()].sort_index()
        atomic_json(filename, {str(k): float(v) for k, v in series.items()})
        return series

    benchmark = prices("sh000300")
    used_symbols, hashes, stats = set(), {}, {}
    for index, (split, first, last, count) in enumerate(windows):
        candidates = _ordered([e for e in events if first <= e["event_time"] <= last and (split == "test" or e["symbol"] not in reserved)])
        selected, labels, rejected = [], [], Counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for event in candidates:
                if len(selected) >= count:
                    break
                if event["symbol"] in used_symbols:
                    continue
                try:
                    result, error = pool.submit(_label, event, price_root, benchmark, price_loader=prices).result(timeout=180)
                except Exception as exc:
                    result, error = None, type(exc).__name__
                if error:
                    rejected[error] += 1
                    continue
                packet, label = result
                next_start = windows[index + 1][1] if index < 2 else as_of
                if label["anchor_end"] >= next_start:
                    rejected["outcome_not_mature_before_boundary"] += 1
                    continue
                used_symbols.add(event["symbol"])
                selected.append(packet)
                labels.append(label)
                progress(out, stage="preparing", split=split, selected=len(selected), required=count)
        if len(selected) != count:
            raise RuntimeError(f"Insufficient eligible {split} events: {len(selected)}/{count}")
        selected.sort(key=lambda e: (e["event_time"], e["event_id"]))
        write_jsonl(out / f"{split}.events.jsonl", selected)
        label_file = out / "oracle" / f"{split}.labels.jsonl"
        label_file.parent.mkdir(exist_ok=True)
        write_jsonl(label_file, labels)
        label_file.chmod(0o600)
        hashes[split] = {"events": digest(selected), "labels": digest(labels)}
        stats[split] = {"n": count, "first": selected[0]["event_time"], "last": selected[-1]["event_time"], "distinct_days": len({e["event_time"] for e in selected}), "exclusions": dict(rejected)}
    manifest = {"version": "evomap_asof_v2", "as_of": as_of, "counts": dict(zip([w[0] for w in windows], counts)), "hashes": hashes, "stats": stats,
                "windows": windows, "source_rejections": rejections, "excluded_prior_events": len(seen_events),
                "excluded_prior_groups": len(seen_groups), "excluded_prior_issuers": len(seen_symbols),
                "issuer_disjoint": True, "prior_issuer_overlap": 0,
                "label_protocol": "t3/benchmark_relative_return/epsilon0/unknown_time_next_close",
                "limitations": ["Historical model pretraining exposure unknown", "Source revision histories are not independently timestamp-verified", "Adjusted historical price series downloaded now"]}
    atomic_json(final, manifest)
    progress(out, stage="complete", counts=manifest["counts"], manifest_hash=digest(manifest))
    return manifest


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--as-of", default="2026-11-12")
    p.add_argument("--learn", type=int, default=150)
    p.add_argument("--validation", type=int, default=60)
    p.add_argument("--test", type=int, default=100)
    args = p.parse_args()
    prepare(args.out, counts=(args.learn, args.validation, args.test), as_of=args.as_of)
