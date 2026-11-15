"""Curate archived real earnings disclosures and regenerate aligned T+3 labels.

Selection depends only on source facts, dates, duplicate history, price coverage,
and nonzero label eligibility. No prediction or sign preference enters sampling.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

from .rubric import digest


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    tmp.replace(path)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in rows))


def candidate_events(source: Path) -> tuple[list[dict], dict]:
    used = set()
    for path in source.glob("events*.jsonl"):
        for event in read_jsonl(path):
            used.add((str(event.get("symbol")), str(event.get("event_time", ""))[:10]))
    rows, seen = [], set()
    rejected = Counter()
    for path in sorted((source / "raw").glob("yjyg_*.csv")):
        period = path.stem.split("_")[-1]
        end = dt.date.fromisoformat(f"{period[:4]}-{period[4:6]}-{period[6:]}")
        for raw in csv.DictReader(path.open(encoding="utf-8-sig")):
            code = str(raw.get("股票代码", "")).zfill(6)
            date = str(raw.get("公告日期", ""))[:10]
            if not re.fullmatch(r"(?:60[0135]|688|00[0123]|30[01])\d{3}", code):
                rejected["unsupported_symbol"] += 1; continue
            try:
                day = dt.date.fromisoformat(date)
            except ValueError:
                rejected["missing_date"] += 1; continue
            if not -90 <= (day - end).days <= 120:
                rejected["late_revision_or_period_mismatch"] += 1; continue
            if "归属于上市公司股东的净利润" not in raw.get("预测指标", ""):
                rejected["other_measure"] += 1; continue
            key = (code, date)
            if key in used or key in seen:
                rejected["previously_used_or_duplicate"] += 1; continue
            change = raw.get("业绩变动", "").strip()
            if len(change) < 20 or not re.search(r"\d", change):
                rejected["missing_financial_facts"] += 1; continue
            seen.add(key)
            raw_hash = digest(raw)
            name = raw.get("股票简称", "")
            title = f"{name} {period}报告期业绩预告（{raw.get('预告类型', '')}）"
            # Provider source values are preserved, never derived from labels.
            text = f"公告日期：{date}。报告期截至：{end}。{title}。指标：{raw['预测指标']}。业绩变动：{change}。"
            if raw.get("业绩变动原因"):
                text += "业绩变动原因：" + raw["业绩变动原因"][:1600]
            rows.append({"event_id": "evo_cn_" + code + "_" + raw_hash[:12],
                         "market": "CN", "symbol": code, "event_time": date,
                         "available_time": date, "event_type_l2": "财报超预期/不及预期",
                         "title": title, "event_text": text,
                         "source_url": f"https://data.eastmoney.com/bbsj/{code}/yjyg.html",
                         "benchmark": "sh000300", "group_id": code + ":" + period,
                         "source_provenance": {"archive_commit": "bf96445", "file": str(path),
                             "raw_row_hash": raw_hash, "report_period": period,
                             "provider": "akshare.stock_yjyg_em / Eastmoney",
                             "revision_history": "not independently timestamp-verified"}})
    return rows, dict(rejected)


def _ordered(rows: list[dict]) -> list[dict]:
    """Round-robin dates; hashed ordering within dates, independent of outcomes."""
    days = defaultdict(list)
    for row in rows:
        days[row["event_time"]].append(row)
    for values in days.values():
        values.sort(key=lambda r: digest(["evo-20260921", r["event_id"]]))
    result = []
    for i in range(max(map(len, days.values()), default=0)):
        for date in sorted(days):
            if i < len(days[date]):
                result.append(days[date][i])
    return result


def _price(symbol: str, root: Path, *, benchmark=False):
    import pandas as pd
    from ..labeller import _ak_cn_hist, _ak_cn_index_hist
    path = root / f"{symbol}.json"
    if path.exists():
        saved = json.loads(path.read_text())
        return pd.Series({dt.date.fromisoformat(k): v for k, v in saved.items()}).sort_index()
    fn = _ak_cn_index_hist if benchmark else _ak_cn_hist
    series = fn(symbol, "2023-10-01", "2026-05-31", retries=2, sleep_s=0.15)
    if series is None or len(series) < 30:
        return None
    write_json(path, {str(k): float(v) for k, v in series.items()})
    return series


def _label(event: dict, root: Path, benchmark, *, price_loader=None):
    from ..labeller import _event_anchor_dates, _return_between
    prices = (price_loader or _price)(event["symbol"], root)
    if prices is None:
        return None, "price_unavailable"
    day = dt.date.fromisoformat(event["event_time"])
    anchors = _event_anchor_dates(prices, day, 3, event["event_time"], "CN")
    if not anchors:
        return None, "missing_anchors"
    start, end, _, _ = anchors
    if start > day + dt.timedelta(days=12):
        return None, "suspended_at_event"
    a_days = list(prices.loc[start:end].index)
    b_days = list(benchmark.loc[start:end].index)
    if a_days != b_days or len(a_days) != 4:
        return None, "suspended_or_misaligned_window"
    ar, br = _return_between(prices, start, end), _return_between(benchmark, start, end)
    if ar is None or br is None or not math.isfinite(ar-br) or ar == br:
        return None, "ineligible_label"
    features = {}
    prior = prices[prices.index < day]
    for n in (5, 20):
        if len(prior) <= n:
            continue
        s, e = prior.index[-n-1], prior.index[-1]
        asset_ret, bm_ret = _return_between(prices, s, e), _return_between(benchmark, s, e)
        if asset_ret is not None and bm_ret is not None:
            features.update({f"asset_return_{n}d_pct": asset_ret*100,
                             f"benchmark_return_{n}d_pct": bm_ret*100,
                             f"excess_return_{n}d_pct": (asset_ret-bm_ret)*100})
    event = {**event, "pre_event_features": features}
    label = {"event_id": event["event_id"], "car_t3": ar-br,
             "ret_t3": ar, "bm_ret_t3": br, "label_t3": "up" if ar > br else "down",
             "anchor_start": str(start), "anchor_end": str(end),
             "label_available_at": str(end) + "T15:00:00+08:00",
             "method": "benchmark_relative_return", "epsilon": 0}
    return (event, label), None


def prepare(source: Path, out: Path, counts=(300, 100, 100), workers=6,
            *, windows=None, exclude_dataset: Path | None = None):
    if (out / "manifest.json").exists():
        raise ValueError("dataset already frozen; choose a new output directory")
    out.mkdir(parents=True, exist_ok=True)
    price_dir = source / "prices"
    price_dir.mkdir(exist_ok=True)
    events, excluded = candidate_events(source)
    if exclude_dataset is not None:
        prior = [e for p in exclude_dataset.glob('*.events.jsonl') for e in read_jsonl(p)]
        used = {(e['symbol'], e['event_time']) for e in prior}
        used_groups = {e['group_id'] for e in prior}
        events = [e for e in events if (e['symbol'], e['event_time']) not in used
                  and e['group_id'] not in used_groups]
    benchmark = _price("sh000300", price_dir, benchmark=True)
    if benchmark is None:
        raise RuntimeError("benchmark historical data unavailable")
    windows = windows or [("learn", "2024-01-01", "2024-11-30", counts[0]),
               ("validation", "2025-01-01", "2025-05-31", counts[1]),
               ("test", "2025-07-01", "2026-02-28", counts[2])]
    used_symbols, stats, hashes = set(), {}, {}
    reserved = {e['symbol'] for e in events if windows[2][1] <= e['event_time'] <= windows[2][2]} if exclude_dataset else set()
    for index, (name, start, end, count) in enumerate(windows):
        pool = _ordered([e for e in events if start <= e["event_time"] <= end
                         and (name == 'test' or e['symbol'] not in reserved)])
        selected, labels, rejected = [], [], Counter()
        cursor = 0
        while len(selected) < count and cursor < len(pool):
            batch = []
            while len(batch) < min(workers, count-len(selected)) and cursor < len(pool):
                row = pool[cursor]; cursor += 1
                if row["symbol"] in used_symbols or row["symbol"] in {e["symbol"] for e in batch}:
                    continue
                batch.append(row)
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(_label, row, price_dir, benchmark) for row in batch]
                for row, future in zip(batch, futures):
                    try:
                        result, error = future.result(timeout=180)
                    except Exception as exc:
                        result, error = None, type(exc).__name__
                    if error:
                        rejected[error] += 1
                        continue
                    event, label = result
                    # Conservative month-scale gaps and actual label maturity both checked.
                    next_start = windows[index+1][1] if index < 2 else "9999-01-01"
                    if label["anchor_end"] >= next_start:
                        rejected["cross_boundary_label"] += 1; continue
                    used_symbols.add(event["symbol"])
                    selected.append(event); labels.append(label)
            write_json(out / "preparation_progress.json", {"split": name, "selected": len(selected),
                                                          "required": count, "rejected": dict(rejected)})
            print(f"PREPARE {name} {len(selected)}/{count} candidates={cursor}", flush=True)
        if len(selected) != count:
            raise RuntimeError(f"insufficient qualified {name} events: {len(selected)}/{count}")
        selected.sort(key=lambda e: (e["event_time"], e["event_id"]))
        write_jsonl(out / f"{name}.events.jsonl", selected)
        # No sign distribution or held-out outcome is printed during preparation.
        label_path = out / "oracle" / f"{name}.labels.jsonl"
        write_jsonl(label_path, labels)
        label_path.chmod(0o600)
        hashes[name] = {"events": digest(selected), "labels": digest(labels)}
        stats[name] = {"count": count, "first_date": selected[0]["event_time"],
                       "last_date": selected[-1]["event_time"],
                       "distinct_days": len({e["event_time"] for e in selected}),
                       "excluded": dict(rejected)}
    write_json(out / "manifest.json", {"version": 2 if exclude_dataset else 1, "counts": dict(zip([w[0] for w in windows], counts)),
        "hashes": hashes, "stats": stats, "source_exclusions": excluded,
        "scope": "CN earnings forecasts, disjoint issuers, raw financial facts",
        "label_protocol": "t3/benchmark_relative_return/epsilon0/unknown_time_next_close",
        "source_archive": "bf96445", "windows": windows,
        "prior_use": "exclude known papv/1000 symbol-date pairs and " + str(exclude_dataset),
        "reserved_test_issuers": len(reserved),
        "limitations": ["Raw source revision timestamps not independently verified",
                        "Historical LLM pretraining exposure cannot be excluded",
                        "Adjusted price series downloaded now and frozen for this experiment"]})
    print("DATASET_FROZEN " + json.dumps(stats, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    prepare(args.source, args.out, workers=args.workers)
