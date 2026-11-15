"""Calendar-week schedules and isolated historical outcome preparation.

Event facts never change. Deadline calendar dates are computed without prices;
price values are confined to the oracle, with explicit censoring instead of
shortening a horizon to the last available quotation.
"""
from __future__ import annotations
import argparse
import calendar
import concurrent.futures
import datetime as dt
import json
import math
from collections import Counter
from pathlib import Path

from .prepare import read_jsonl, write_json, write_jsonl
from .rubric import digest, validate_rules


def add_months(day: dt.date, months=3):
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def schedule(event_date: str, trading_days: list[str]):
    day = dt.date.fromisoformat(event_date[:10])
    # All frozen inputs have date-only announcements. A trading-day release
    # enters at the NEXT exchange close; weekend releases use the first close.
    dates = sorted(dt.date.fromisoformat(d) for d in trading_days)
    anchor = next(d for d in dates if d > day)
    cap = add_months(anchor)
    deadlines = [(f'W{k:02d}', anchor + dt.timedelta(days=7*k)) for k in range(1,14)
                 if anchor + dt.timedelta(days=7*k) < cap]
    deadlines.append(('M3', cap))
    result = []
    for horizon, deadline in deadlines:
        eligible = [d for d in dates if anchor <= d <= deadline]
        # Exact scheduled exchange close, never a later date or stock-specific
        # last quote. Full requested calendar must exist through the deadline.
        endpoint = max(eligible) if eligible and max(dates) >= deadline else None
        result.append({'horizon': horizon, 'anchor_start': str(anchor),
            'calendar_deadline': str(deadline), 'anchor_end': str(endpoint) if endpoint else None,
            'calendar_days': (deadline-anchor).days,
            'trading_days': sum(anchor < d <= endpoint for d in dates) if endpoint else None,
            'cap_date': str(cap)})
    return result


def versions(root: Path):
    specs = [('legacy','earnings_500_20260921','r0'),('v1_r1','earnings_500_20260921','r1'),
        ('v1_r2','earnings_500_20260921','r2'),('v1_r5','earnings_500_20260921','r5'),
        ('v2_r0','earnings_500_v2_20260921','r0'),('v2_r3','earnings_500_v2_20260921','r3'),
        ('v2_r4','earnings_500_v2_20260921','r4'),('v2_r5','earnings_500_v2_20260921','r5')]
    result = []
    for name, experiment, version in specs:
        path = root / experiment / 'rubrics' / (version+'.json')
        rules = json.loads(path.read_text())
        try:
            validate_rules(rules); valid, reason = True, None
        except ValueError as exc:
            valid, reason = False, str(exc)
        result.append({'version': name, 'source': str(path.resolve()), 'rules': rules,
            'hash': digest(rules), 'historically_contract_valid': valid, 'invalid_reason': reason})
    return result


def fetch_price(symbol: str, source: Path, out: Path, required_end: str):
    cached = out / 'prices' / (symbol+'.json')
    if cached.exists():
        return json.loads(cached.read_text()), 'weekly_cache'
    previous = source / (symbol+'.json')
    old = json.loads(previous.read_text()) if previous.exists() else {}
    if old and max(old) >= required_end:
        write_json(cached, old)
        return old, 'frozen_previous_cache'
    from ..labeller import _ak_cn_hist, _ak_cn_index_hist
    fn = _ak_cn_index_hist if symbol == 'sh000300' else _ak_cn_hist
    series = fn(symbol, '2023-10-01', '2026-09-18', retries=2, sleep_s=.15)
    if series is None or not len(series):
        # Preserve partial observations but never pretend missing endpoints mature.
        write_json(cached, old)
        return old, 'download_failed_partial_cache'
    values = {str(k): float(v) for k,v in series.items()}
    write_json(cached, values)
    return values, 'fresh_full_series_no_splicing'


def label_horizon(event, horizon, prices, benchmark, cutoff='2026-09-21'):
    item = {'event_id': event['event_id'], 'split': event['split'], **horizon}
    start, end = horizon['anchor_start'], horizon['anchor_end']
    if horizon['calendar_deadline'] > cutoff:
        return {**item, 'eligible': False, 'reason': 'not_mature'}
    if end is None:
        return {**item, 'eligible': False, 'reason': 'incomplete_exchange_calendar'}
    if end == start:
        return {**item, 'eligible': False, 'reason': 'no_trading_session_after_entry'}
    if any(k not in prices or k not in benchmark for k in (start,end)):
        return {**item, 'eligible': False, 'reason': 'missing_exact_endpoint'}
    values = [prices[start],prices[end],benchmark[start],benchmark[end]]
    if any(not math.isfinite(v) or v <= 0 for v in values):
        return {**item, 'eligible': False, 'reason': 'invalid_price'}
    asset, bm = prices[end]/prices[start]-1, benchmark[end]/benchmark[start]-1
    excess = asset-bm
    return {**item, 'eligible': True, 'reason': None,
            'asset_return': asset, 'benchmark_return': bm, 'excess_return': excess,
            'label': 'up' if excess > 0 else 'down' if excess < 0 else 'flat'}


def prepare_weekly(dataset: Path, source: Path, experiment_root: Path, out: Path):
    if (out/'data_manifest.json').exists():
        print('WEEKLY_DATA_ALREADY_FROZEN', flush=True); return
    out.mkdir(parents=True, exist_ok=True)
    benchmark, bm_source = fetch_price('sh000300', source, out, '2026-08-01')
    if not benchmark or max(benchmark) < '2026-08-01':
        raise RuntimeError('extended benchmark calendar unavailable through 3-month deadlines')
    originals = [dict(e,split=split) for split in ('learn','validation','test')
                 for e in read_jsonl(dataset/(split+'.events.jsonl'))]
    events = [{**e, 'schedule': schedule(e['event_time'], list(benchmark))} for e in originals]
    if len(events)!=500:
        raise ValueError('requires original 300/100/100 universe')
    registry = versions(experiment_root)
    write_json(out/'versions.json', registry)
    # Predictor-safe manifest and event file. No label or future price values.
    write_jsonl(out/'events.jsonl', events)
    write_json(out/'schedule_manifest.json', {'events_hash':digest(events), 'versions_hash':digest(registry),
        'counts':dict(Counter(e['split'] for e in events)),
        'dataset_manifest_hash':digest(json.loads((dataset/'manifest.json').read_text())),
        'cutoff':'2026-09-21', 'horizon_semantics':'cumulative_excess_return_to_calendar_week_deadline',
        'cap':'three_calendar_months_from_entry_close', 'deadline_trading_mapping':'last_exchange_close_on_or_before_deadline',
        'scope':'all500_same_original_facts_all8_unique_snapshots',
        'prediction_repetitions':1, 'evaluation_only_no_new_evolution':True})
    print('WEEKLY_SCHEDULE_FROZEN events=500 versions=8',flush=True)
    labels, sources, failures = [], {}, Counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        pending = [pool.submit(fetch_price,e['symbol'],source,out,e['schedule'][-1]['calendar_deadline']) for e in events]
        for i,(event,future) in enumerate(zip(events,pending),1):
            try:
                prices,origin = future.result()
            except Exception as exc:
                prices,origin = {},type(exc).__name__
            sources[event['symbol']] = {'source':origin,'hash':digest(prices),'last_date':max(prices,default=None)}
            for horizon in event['schedule']:
                label = label_horizon(event,horizon,prices,benchmark)
                labels.append(label)
                if not label['eligible']: failures[label['reason']]+=1
            if i%25==0:
                print(f'WEEKLY_LABEL_PREP {i}/500',flush=True)
                write_json(out/'preparation_status.json',{'complete_events':i,'total':500})
    write_jsonl(out/'oracle'/'weekly.labels.jsonl', labels)
    (out/'oracle'/'weekly.labels.jsonl').chmod(0o600)
    write_json(out/'data_manifest.json', {'labels_hash':digest(labels),'events_hash':digest(events),
        'sources':sources,'benchmark_hash':digest(benchmark),'benchmark_source':bm_source,
        'eligible_pairs':sum(x['eligible'] for x in labels),'total_pairs':len(labels),
        'missing_reasons':dict(failures), 'never_forward_fill_missing_stock_endpoint':True,
        'note':'Old as-of feature values retained; refreshed adjusted series not spliced to old labels.'})
    print('WEEKLY_LABELS_FROZEN '+json.dumps({'pairs':len(labels),'missing':dict(failures)}),flush=True)


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--dataset',type=Path,required=True);p.add_argument('--source',type=Path,required=True)
    p.add_argument('--experiments',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();prepare_weekly(a.dataset,a.source,a.experiments,a.out)
