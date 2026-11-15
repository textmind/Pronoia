"""Second preregistered experiment: typed edits, bounded repair, balanced diagnosis."""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import fcntl
import json
import random
import re
from dataclasses import replace
from collections import Counter, defaultdict
from pathlib import Path

from .controller import Experiment, ATTRIBUTION_PROMPT
from .evaluation import summary
from .prepare import read_jsonl, write_json
from .rubric import digest
from .structured import INITIAL_STRUCTURED_RULES, catalog, validate_structured
from ...llm import runtime_target_context

ATTRIBUTION_V2 = ATTRIBUTION_PROMPT + '''
输出协议不可变：资料充分必须给up/down；低置信度不能改变输出状态。
主目标是提高方向正确率；降低confidence不算方向改善，也不等于已实现概率校准。
样本按错误方向和预告子类型平衡抽取，不代表总体比例；总体判断必须核对learning_population。
每个模式至少引用3个不同日期的真实错误，并引用至少1个同类成功反例；说明为何规则改动不会损害该反例。
只可建议catalog允许的条件、analysis枚举、数值阈值、乘数、置信度上限或规则增删。
规则触发为模型自报，不能当作确定性的执行事实；不得臆造缺失的公告阶段、预期或价格。
'''
MUTATION_V2 = '''你是结构化规则编辑器。仅输出JSON，不得输出解释段落。
只能使用catalog列出的条件与analysis枚举、数值范围和严格schema；不能添加任何自由文本条件或动作。
最多3处编辑，每条规则只编辑一次，每个编辑引用至少1个给定pattern_id。
资料充分必须up/down，不能修改输出状态；规则编号不能包含事件、公司、日期。
方向正确率是主指标。只降置信度不构成方向提升，请优先检验可改变证据权衡的有依据改动。
验证反馈只有汇总；不得推测验证事件答案。若收到validation_error，只修复语法/约束错误，不增加编辑预算。
返回{"edits":[{"op":"update|add|delete","rule_id":"R01","pattern_ids":["P1"],"reason":"预期改善与可能退化","rule":完整结构化规则}]}。
delete不带rule。无需改动时edits为空。新增ID不能复用used_rule_ids。
'''


def subtype(record):
    match = re.search(r'（([^（）]+)）', record['packet']['as_of_packet'].get('title', ''))
    return match.group(1) if match else 'unknown'


def balanced_sample(records, limit=12):
    strata = defaultdict(list)
    for r in records:
        strata[(r['actual_direction'], subtype(r))].append(r)
    for rows in strata.values():
        rows.sort(key=lambda r: digest(['v2-balanced', r['event_id']]))
    chosen, per_day = [], Counter()
    while any(strata.values()) and len(chosen) < limit:
        for key in sorted(strata):
            if len(chosen) >= limit:
                break
            while strata[key]:
                row = strata[key].pop(0)
                if per_day[row['date']] < 2:
                    chosen.append(row)
                    per_day[row['date']] += 1
                    break
    return chosen


def paired_comparison(base, candidate):
    dates, deltas = {}, defaultdict(float)
    for m0, m1 in zip(base, candidate):
        before = {r['event_id']: r for r in m0['outcomes']}
        for row in m1['outcomes']:
            eid = row['event_id']
            dates[eid] = row['date']
            deltas[eid] += (int(row['correct']) - int(before[eid]['correct'])) / len(base)
    blocks = defaultdict(list)
    for eid, value in deltas.items():
        blocks[dt.date.fromisoformat(dates[eid]).toordinal() // 14].append(value)
    rng, values, draws = random.Random(20260921), list(blocks.values()), []
    for _ in range(2000):
        sample = [v for block in rng.choices(values, k=len(values)) for v in block]
        draws.append(sum(sample) / len(sample))
    draws.sort()
    return {'delta': sum(deltas.values()) / len(deltas),
            'paired_14_day_block_bootstrap_95ci': [draws[50], draws[1949]],
            'independent_blocks': len(blocks), 'test_n': len(deltas)}


class StructuredExperiment(Experiment):
    initial_rules = INITIAL_STRUCTURED_RULES
    attribution_prompt = ATTRIBUTION_V2
    mutation_prompt = MUTATION_V2
    optimizer_max_tokens = 16384
    include_legacy_reference = True
    extra_files = [Path(__file__), Path(__file__).with_name('structured.py'), Path(__file__).with_name('prepare.py')]
    policy_extras = {'experiment_version': 2, 'max_validation_repairs': 2,
        'optimizer_output_tokens': 16384, 'optimizer_timeout_seconds': 300,
        'balanced_errors': 12, 'matched_successes': 12,
        'legacy_test_reference': True, 'calibration_objective': 'diagnostic_only_not_promotion',
        'test_window': '2026-03-01..2026-04-30', 'immutable_output_contract': 'up/down; missing_facts_only_abstention'}

    async def call_agent(self, path, system, payload):
        with runtime_target_context(replace(self.target, timeout_seconds=300, max_output_tokens=16384)):
            return await super().call_agent(path, system, payload)

    def learning_examples(self, predictions):
        # Construct all records with the existing packet and Oracle boundary.
        from ..engine import _event_prompt
        from ..models import EventRecord
        events = read_jsonl(self.dataset / 'learn.events.jsonl')
        labels = {r['event_id']: r for r in read_jsonl(self.dataset / 'oracle' / 'learn.labels.jsonl')}
        pred = {r['event_id']: r for r in predictions}
        wrong, right, population = [], [], defaultdict(lambda: Counter(n=0, correct=0))
        for event in events:
            p = pred[event['event_id']]
            record = {'event_id': event['event_id'], 'group_id': event['group_id'], 'date': event['event_time'],
                'packet': json.loads(_event_prompt(EventRecord.from_dict(event))),
                'prediction': {k: p.get(k) for k in ['pred_direction', 'confidence', 'rationale', 'abstain']},
                'rules_evaluated': [c for c in p['strategy_metadata'].get('rule_audit', {}).get('claims', []) if c.get('trigger') == 'true'],
                'audit_errors': p['strategy_metadata'].get('rule_audit', {}).get('errors', []),
                'actual_direction': labels[event['event_id']]['label_t3']}
            hit = not p.get('abstain') and p.get('pred_direction') == record['actual_direction']
            record['correct'] = hit
            for key in ['all', 'actual:' + record['actual_direction'],
                        'predicted:' + str(p.get('pred_direction')), 'subtype:' + subtype(record)]:
                population[key]['n'] += 1
                population[key]['correct'] += int(hit)
            (right if hit else wrong).append(record)
        errors = balanced_sample(wrong)
        successes, used, per_day = [], set(), Counter()
        for error in errors:
            pool = sorted([r for r in right if r['event_id'] not in used and per_day[r['date']] < 2],
                key=lambda r: (subtype(r) != subtype(error), r['actual_direction'] != error['actual_direction'], digest(r['event_id'])))
            if pool:
                r = pool[0]
                successes.append(r); used.add(r['event_id']); per_day[r['date']] += 1
        self.learning_population = {k: dict(v) for k, v in population.items()}
        return errors + successes

    def attribution_payload(self, rules, examples, predictions):
        return {'rules': rules, 'learning_examples': examples,
                'learning_population': self.learning_population, 'catalog': catalog(),
                'sampling': '12 errors balanced by actual direction and forecast subtype, max2/date; 12 closest subtype/direction success controls. Selected proportions are not population rates.'}

    def filter_patterns(self, patterns, examples):
        by_id = {e['event_id']: e for e in examples}
        valid = []
        for p in patterns:
            errors = [by_id[i] for i in p['event_ids'] if not by_id[i]['correct']]
            counter = p.get('counterexamples')
            if (len({e['date'] for e in errors}) >= 3 and len({e['group_id'] for e in errors}) >= 3
                    and isinstance(counter, list) and counter
                    and all(isinstance(i, str) and i in by_id and by_id[i]['correct'] for i in counter)):
                valid.append(p)
        return valid

    def validate_proposal(self, rules, proposal, patterns, examples, seen_ids):
        candidate = super().validate_proposal(rules, proposal, patterns, examples, seen_ids)
        validate_structured(candidate)
        return candidate

    async def propose_candidate(self, folder, rules, patterns, examples, current, best, seen_ids):
        if not patterns:
            return {'edits': []}, rules, None
        payload = {'rules': rules, 'patterns': patterns, 'validation_summary': summary(current),
            'best_validation_summary': summary(best), 'used_rule_ids': sorted(seen_ids), 'catalog': catalog()}
        for attempt in range(3):
            name = 'proposal' if attempt == 0 else f'proposal_repair_{attempt}'
            proposal = await self.call_agent(folder / f'{name}.json', self.mutation_prompt, payload)
            try:
                candidate = self.validate_proposal(rules, proposal, patterns, examples, seen_ids)
                write_json(folder / f'{name}.validation.json', {'valid': True, 'attempt': attempt})
                return proposal, candidate, None
            except (ValueError, TypeError, KeyError) as exc:
                error = str(exc)
                write_json(folder / f'{name}.validation.json', {'valid': False, 'attempt': attempt, 'error': error})
                payload = {**payload, 'previous_proposal': proposal, 'validation_error': error}
        return proposal, rules, error

    def report(self, best, all_metrics):
        super().report(best, all_metrics)
        path = self.root / 'final_report.json'
        report = json.loads(path.read_text())
        report['test_versions'] = {v: [{k: m[k] for k in ['score', 'coverage', 'audit_violation_rate',
            'mean_input_tokens', 'tokens_in', 'tokens_out', 'high_confidence_n', 'high_confidence_error_rate']}
            for m in ms] for v, ms in all_metrics.items()}
        report['representation_vs_legacy'] = paired_comparison(all_metrics['legacy'], all_metrics['r0'])
        report['total_vs_legacy'] = paired_comparison(all_metrics['legacy'], all_metrics[best])
        report['evolution_vs_structured_initial'] = paired_comparison(all_metrics['r0'], all_metrics[best]) if best != 'r0' else None
        report['selection_note'] = 'Legacy never participates in search; comparisons use the same 100 held-out events, 3 repeats. Confidence is not a calibrated probability.'
        write_json(path, report)


async def main(args):
    experiment = StructuredExperiment(args.dataset, args.out, concurrency=args.concurrency, max_tokens=args.max_tokens)
    with (experiment.root / 'process.lock').open('w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            await experiment.run()
        except Exception as exc:
            experiment.status(stage='interrupted', error_type=type(exc).__name__)
            raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--concurrency', type=int, default=24)
    parser.add_argument('--max-tokens', type=int, default=25_000_000)
    asyncio.run(main(parser.parse_args()))
