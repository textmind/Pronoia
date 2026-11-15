"""Typed policy search space; only this immutable catalog supplies model instructions.

Predicates remain interpreted by the LLM. Structural validation does not prove
that a predicate is true or that a multiplier was causally obeyed.
"""
from __future__ import annotations

import math

CONDITIONS = {
    'profit_change': ('fundamental', '公告包含可核查的利润亏损、扭亏或盈利变化'),
    'no_expectations': ('expectation', '只有同比变化而没有事前市场一致预期'),
    'one_off_gain': ('quality', '正文指出收益受非经常性收益、资产处置收益或会计调整收益影响'),
    'one_off_loss': ('quality', '正文指出亏损受非经常性损失、减值或会计调整损失影响'),
    'operating_change': ('quality', '正文指出主营销量、收入、成本或毛利发生变化'),
    'incremental_disclosure': ('event_stage', '公告是更正、补充或进展，且能够识别原有事实和新增事实'),
    'same_direction_price': ('price', 'packet提供事前5日涨跌且与消息方向同向、绝对变动至少{threshold}%'),
    'opposite_direction_price': ('price', 'packet提供事前5日涨跌且与消息方向相反、绝对变动至少{threshold}%'),
    'conflicting_evidence': ('conflict', '存在至少两条相互独立且方向相反的实质证据'),
    'long_term_only': ('horizon', '有长期经营判断但缺少短期催化证据'),
    'common_market_factor': ('benchmark', '事件涉及行业或市场共同因素'),
    'single_evidence': ('confidence', '方向依据来自一项孤立事实且缺少独立佐证'),
    'loss_improving': ('fundamental', '利润仍然亏损但同比减亏，且有可核查的变动原因'),
    'profit_declining': ('fundamental', '利润仍为正但同比下降，且有可核查的变动原因'),
    'large_yoy_no_expectations': ('expectation', '同比变动绝对幅度至少{threshold}%，但缺少事前市场一致预期'),
}
THRESHOLDS = {'same_direction_price': (1, 20), 'opposite_direction_price': (1, 20),
              'large_yoy_no_expectations': (20, 500)}
ANALYSIS = {
    'profit_level_vs_change': '区分利润绝对水平和同比变化；亏损即使同比改善也不得自动当作强利好，结合幅度及原因判断方向。',
    'yoy_not_surprise': '不得把同比增长等同于超市场预期，或把同比下降等同于低于市场预期。',
    'one_off_quality': '区分一次性损益与持续经营；对该项一次性损益按指定乘数调整，不重复计算利润数字与其同源原因。',
    'operating_quality': '优先考虑主营量价及毛利对经营的影响；同一事实只算一次，正负信号同时存在时相互抵消。',
    'incremental_only': '只将新增事实计入事件冲击；重复披露不额外放大方向信号。',
    'price_absorption': '考虑价格已反映消息；没有事前行情时本规则为unknown，不能自行编造涨跌。',
    'price_disagreement': '分别分析消息与事前价格背离的可能原因，不能把背离自动解释为未来反转。',
    'weigh_conflict': '分别引用相反证据再判断净影响。',
    'short_term_catalyst': '区分长期经营趋势与T+3反应，不能直接把长期增长结论当成短期强方向信号。',
    'relative_benchmark': '判断公司相对沪深300的影响，不把市场普涨自动视为公司正超额收益；基准信息缺失时说明限制。',
    'independent_evidence': '不能把相同事实的标题、正文和指标当作三个独立证据。',
    'loss_recovery': '区分持续经营驱动的减亏与一次性减亏，并分别衡量亏损水平和经营修复的方向贡献。',
    'profit_deceleration': '区分正常基数效应和主营业务恶化，分别衡量仍然盈利与盈利下滑的方向贡献。',
    'base_effect': '核查同比基数是否接近零、为负或包含一次性项目，避免仅按同比百分数大小放大冲击。',
}


def rule(rid, code, analysis, *, threshold=None, multiplier=None, cap=None):
    return {'id': rid, 'category': CONDITIONS[code][0],
            'condition': {'code': code, 'threshold': threshold},
            'action': {'analysis': analysis, 'signal_multiplier': multiplier, 'confidence_cap': cap}}


INITIAL_STRUCTURED_RULES = [
    rule('R01', 'profit_change', ['profit_level_vs_change']),
    rule('R02', 'no_expectations', ['yoy_not_surprise'], cap=.65),
    rule('R03', 'one_off_gain', ['one_off_quality'], multiplier=.5),
    rule('R04', 'operating_change', ['operating_quality']),
    rule('R05', 'incremental_disclosure', ['incremental_only']),
    rule('R06', 'same_direction_price', ['price_absorption'], threshold=5, multiplier=.5),
    rule('R07', 'conflicting_evidence', ['weigh_conflict'], cap=.60),
    rule('R08', 'long_term_only', ['short_term_catalyst']),
    rule('R09', 'common_market_factor', ['relative_benchmark']),
    rule('R10', 'single_evidence', ['independent_evidence'], cap=.60),
]


def number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def validate_structured(rules):
    import re
    if not isinstance(rules, list) or not 1 <= len(rules) <= 15:
        raise ValueError('structured rubric must have 1..15 rules')
    seen = set()
    for r in rules:
        if not isinstance(r, dict) or set(r) != {'id', 'category', 'condition', 'action'}:
            raise ValueError('exact fields required: id/category/condition/action')
        rid = r['id']
        if not isinstance(rid, str) or not re.fullmatch(r'R[A-Z0-9_]{1,20}', rid) or rid in seen:
            raise ValueError('invalid/duplicate rule ID')
        seen.add(rid)
        c, a = r['condition'], r['action']
        if not isinstance(c, dict) or set(c) != {'code', 'threshold'}:
            raise ValueError('condition requires exactly code/threshold; no free text')
        code = c['code']
        if not isinstance(code, str) or code not in CONDITIONS or r['category'] != CONDITIONS[code][0]:
            raise ValueError('unknown condition code or mismatched category')
        if code in THRESHOLDS:
            if not number(c['threshold'], *THRESHOLDS[code]):
                raise ValueError(f'{code} threshold must be in {THRESHOLDS[code]}')
        elif c['threshold'] is not None:
            raise ValueError('this condition has no numeric threshold; use null')
        if not isinstance(a, dict) or set(a) != {'analysis', 'signal_multiplier', 'confidence_cap'}:
            raise ValueError('action requires exactly analysis/signal_multiplier/confidence_cap; no output-state edits')
        items = a['analysis']
        if not isinstance(items, list) or not 1 <= len(items) <= 2 or any(not isinstance(i, str) or i not in ANALYSIS for i in items) or len(set(items)) != len(items):
            raise ValueError('analysis requires 1..2 distinct catalog IDs')
        for field, lo, hi in [('signal_multiplier', .1, 2.5), ('confidence_cap', .3, .9)]:
            if a[field] is not None and not number(a[field], lo, hi):
                raise ValueError(f'{field} must be null or finite numeric {lo}..{hi}')


def textual_rules(rules):
    validate_structured(rules)
    result = []
    for r in rules:
        c, a = r['condition'], r['action']
        action = ''.join(ANALYSIS[i] for i in a['analysis'])
        if a['signal_multiplier'] is not None:
            action += f"该条件所对应消息信号的贡献乘以{a['signal_multiplier']:g}（保留其正负方向；不放大无关信号）。"
        if a['confidence_cap'] is not None:
            action += f"相应证据信心上限{a['confidence_cap']:.2f}。"
        result.append({'id': r['id'], 'category': r['category'],
                       'condition': CONDITIONS[c['code']][1].format(threshold=c['threshold']),
                       'action': action})
    return result


def catalog():
    return {'conditions': {k: {'category': v[0], 'meaning': v[1], 'threshold_range': THRESHOLDS.get(k)} for k, v in CONDITIONS.items()},
            'analysis': ANALYSIS, 'action_bounds': {'signal_multiplier': [.1, 2.5], 'confidence_cap': [.3, .9]},
            'schema_example': INITIAL_STRUCTURED_RULES[5],
            'immutable_output_contract': '资料充分必须up/down；仅缺失事实可insufficient_data。不得修改输出状态。confidence是信心，不是校准概率。方向正确率是唯一主指标。'}
