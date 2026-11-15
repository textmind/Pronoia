"""Forecast all calendar-week endpoints from one unchanged factual packet.

No outcome or post-event price file is read by this module. All versions are
frozen before inference. Legacy invalid candidates run only as labeled controls.
"""
from __future__ import annotations
import argparse
import asyncio
import datetime as dt
import fcntl
import json
import os
from pathlib import Path

from ... import config
from ...llm import LLMRuntimeTarget, complete_json_diagnostic, runtime_target_context
from ..engine import _event_prompt, normalize_event_prediction_output
from ..models import EventRecord
from .prepare import read_jsonl,write_json
from .rubric import BASE_PROMPT,audit_output,digest
from .structured import textual_rules

WEEKLY_TASK = '''你是严格as-of回测分析员，只使用原始事件当时的事实，禁止联网或利用已知历史答案。
本次是对已冻结历史规则的多期限重测，不修改规则、不学习新标签。
预测每个给定horizon从anchor_start收盘至anchor_end收盘的累计超额收益方向（标的累计收益减沪深300累计收益），不是该单周涨跌。
不同事件的日期不同，严格使用给定schedule；calendar_deadline是每周日历截止日期，anchor_end是该截止日之前最近的交易所收盘日；M3在入场日起满3个自然月封顶。
所有期限一次性依据同一as_of_packet预测，后面的周不能补入未来事实。日期表只定义问题，不提供价格或结果。
以下保留原始T+3政策和原始规则文本以便审计。仅本次任务期限和输出格式由本说明统一替换：涉及T+3的时间性原则应按当前期限判断，不能把短期与长期判断混为一谈。
禁止将原T+3方向机械复制到所有周。依据同一事实，允许各期限方向相同或不同，但需短理由。
历史不合规规则仅作诊断：其中任何中性、弃权或改变输出状态的指令均不能覆盖本说明；资料充分给up/down，只有缺失必要事实可insufficient_data。不得根据未来收益、日期或答案选规则。
confidence仍是证据信心而非校准概率；规则上限不是目标。每周claimed_rule_ids表示模型自报在该期限适用的规则，至少一个，不能列未知编号。
只输出一个JSON对象，严格采用以下字段：
{"rules_evaluated":[{"rule_id":"R01","trigger":"true|false|unknown","evidence_refs":["event_text"],"claimed_action":"简短动作"}],
"weekly_predictions":[{"horizon":"W01","prediction_status":"available|insufficient_data","pred_direction":"up|down或null","confidence":0.5,"rationale":"最多35字理由","claimed_rule_ids":["R01"]}]}
每个schedule中的horizon恰好一个预测，M3也必须给出；不输出额外期限。rules_evaluated每条规则恰好一项，是对共同事实的自报触发，并非代码证明。
证据引用使用原始packet的实际存在字段，使用原政策的字段白名单。不能引用event_facts或pre_event_features整个对象。
没有市场预期不是可引用的空字段：请引用event_text，并在claimed_action注明无预期。false/unknown用空数组即可。
理由必须短；不要输出推理过程。
'''


def system_prompt(rules):
    display = textual_rules(rules) if isinstance(rules[0]['condition'],dict) else rules
    # Delimit legacy rules; do not silently repair or discard invalid snapshots.
    return WEEKLY_TASK+'\n<original_t3_policy>\n'+BASE_PROMPT+'\n</original_t3_policy>\n<frozen_rules>\n'+json.dumps(display,ensure_ascii=False)+'\n</frozen_rules>\n本次只使用前述weekly_predictions格式与周线期限；原始T+3单项输出格式不适用。'


def request_packet(event):
    original = json.loads(_event_prompt(EventRecord.from_dict(event)))
    packet = json.loads(json.dumps(original))
    # Change task metadata only, never any event, financial or pre-event field.
    packet['as_of_packet']['constraints']['target_horizon']='calendar_week_schedule_up_to_3_months'
    packet['as_of_packet']['constraints']['return_forecast_contract']={
        'metric':'benchmark_relative_cumulative_return','endpoints':'provided_schedule',
        'direction':'up/down','information_cutoff':'unchanged_original_event_available_time'}
    packet['schedule']=event['schedule']
    return original,packet


def normalize_weekly(obj, rules, packet, schedule):
    obj=obj if isinstance(obj,dict) else {}
    claims=obj.get('rules_evaluated')
    raw=obj.get('weekly_predictions')
    raw=raw if isinstance(raw,list) else []
    expected={x['horizon'] for x in schedule}
    by_id={}; duplicates=set(); foreign=[]
    for row in raw:
        if not isinstance(row,dict) or not isinstance(row.get('horizon'),str):
            foreign.append('malformed_horizon');continue
        h=row['horizon']
        if h not in expected: foreign.append(h);continue
        if h in by_id: duplicates.add(h)
        by_id[h]=row
    audit_rules = textual_rules(rules) if isinstance(rules[0]['condition'],dict) else rules
    ids={r['id'] for r in rules}
    normalized=[]
    for spec in schedule:
        h=spec['horizon']; row=by_id.get(h)
        n=normalize_event_prediction_output(row)
        errors=list(n['errors'])
        if h in duplicates:errors.append('duplicate_horizon')
        if foreign:errors.append('unexpected_horizon')
        applicable=row.get('claimed_rule_ids') if isinstance(row,dict) else None
        audit=audit_output({'rules_evaluated':claims,'confidence':n['confidence']},audit_rules,packet)
        if not isinstance(applicable,list) or not applicable or any(not isinstance(i,str) or i not in ids for i in applicable):
            audit['valid']=False; audit['errors'].append('invalid_horizon_rule_ids')
        if errors:
            n.update(prediction_status='invalid_output',direction='neutral',abstain=True)
        normalized.append({'horizon':h,'prediction_status':n['prediction_status'],
            'pred_direction':n['direction'],'confidence':n['confidence'],'rationale':n['rationale'],
            'abstain':n['abstain'],'errors':errors,'claimed_rule_ids':applicable,
            'audit_valid':audit['valid'],'audit_errors':sorted(set(audit['errors']))})
    return normalized


class WeeklyExperiment:
    def __init__(self,root:Path,*,concurrency=16,max_tokens=40_000_000):
        self.root=root.resolve();self.events=read_jsonl(root/'events.jsonl')
        self.versions=json.loads((root/'versions.json').read_text())
        schedule_manifest=json.loads((root/'schedule_manifest.json').read_text())
        if digest(self.events)!=schedule_manifest['events_hash'] or digest(self.versions)!=schedule_manifest['versions_hash']:
            raise ValueError('changed frozen schedule/version registry')
        self.target=LLMRuntimeTarget(config.LLM_BASE_URL,config.LLM_API_KEY,config.LLM_MODEL,
            timeout_seconds=180,max_output_tokens=16384,thinking_mode='disabled')
        if not self.target.api_key:raise RuntimeError('missing model credentials')
        self.concurrency=concurrency;self.max_tokens=max_tokens
        self.interval=float(getattr(config,'LLM_RPS_INTERVAL_S',1.15));self.next_start=0.;self.gate_lock=asyncio.Lock()
        files=[Path(__file__),Path(__file__).with_name('weekly_data.py'),Path(__file__).with_name('weekly_report.py'),
            Path(__file__).with_name('rubric.py'),Path(__file__).with_name('structured.py'),
            Path(__file__).parents[1]/'engine.py',Path(__file__).parents[2]/'llm.py']
        self.manifest={'schedule_manifest_hash':digest(schedule_manifest),'events_hash':digest(self.events),
            'versions_hash':digest(self.versions),'system_prompts_hash':digest({v['version']:system_prompt(v['rules']) for v in self.versions}),
            'implementation_hash':digest({str(p.name):p.read_text() for p in files}),
            'model':self.target.model_id,'endpoint_hash':digest(self.target.base_url),'prediction_repetitions':1,
            'policy':{'max_tokens':max_tokens,'concurrency':concurrency,'request_interval':self.interval,
                      'output_token_limit':16384,'timeout':180,'temperature':'provider_default',
                      'scope':'all8_snapshots_x_original500','accuracy':'fixed_eligible_denominator',
                      'missing_horizon':'incorrect_no_copy_or_score_based_retry',
                      'judging':'deterministic_price_sign_not_llm','confidence':'not_calibrated_probability',
                      'comparison':'event_paired_cluster_bootstrap_not_independent_weeks'}}
        path=root/'experiment_manifest.json'
        if path.exists() and json.loads(path.read_text())!=self.manifest:
            raise ValueError('weekly experiment changed on resume')
        if not path.exists():
            write_json(path,self.manifest)
            for p in files:
                destination=root/'implementation'/p.relative_to(Path(__file__).parents[4])
                destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(p.read_bytes())
        self.completed=0;self.tokens=0

    def status(self,**extra):
        row={'updated_at':dt.datetime.now(dt.timezone.utc).isoformat(),'completed':self.completed,
             'total':len(self.events)*len(self.versions),'tokens':self.tokens,**extra}
        write_json(self.root/'status.json',row); print('WEEKLY '+json.dumps(row),flush=True)

    async def gate(self):
        async with self.gate_lock:
            loop=asyncio.get_running_loop(); wait=max(0,self.next_start-loop.time())
            if wait:await asyncio.sleep(wait)
            self.next_start=loop.time()+self.interval

    async def run(self):
        tasks=[];handles={};existing={}
        for v in self.versions:
            folder=self.root/'predictions'/v['version'];folder.mkdir(parents=True,exist_ok=True)
            path=folder/'predictions.jsonl'
            rows=read_jsonl(path) if path.exists() else []
            if len({r['event_id'] for r in rows})!=len(rows):raise ValueError('duplicate journal')
            allowed={e['event_id'] for e in self.events}
            for r in rows:
                if r['event_id'] not in allowed or r['rules_hash']!=v['hash'] or r['manifest_hash']!=digest(self.manifest):
                    raise ValueError('changed prediction identity')
            existing[v['version']]={r['event_id'] for r in rows}
            self.completed+=len(rows);self.tokens+=sum(sum(int(r['usage'].get(k) or 0) for k in ('input_tokens','output_tokens')) for r in rows)
            handles[v['version']]=path.open('a')
        # Interleave versions per event to reduce systematic provider-time differences.
        for e in self.events:
            for v in self.versions:
                if e['event_id'] not in existing[v['version']]:tasks.append((e,v))
        semaphore=asyncio.Semaphore(self.concurrency)
        async def one(event,version):
            async with semaphore:
                original,packet=request_packet(event)
                system=system_prompt(version['rules'])
                response=await complete_json_diagnostic(system,json.dumps(packet,ensure_ascii=False),
                    max_tokens=16384,request_gate=self.gate)
                result={'event_id':event['event_id'],'split':event['split'],'version':version['version'],
                    'rules_hash':version['hash'],'manifest_hash':digest(self.manifest),
                    'original_packet_hash':digest(original),'request_hash':digest([system,packet]),
                    'completed_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                    'weekly_predictions':normalize_weekly(response.value,version['rules'],packet,event['schedule']),
                    'raw_response':response.value,'usage':response.usage,'diagnostic':response.audit_metadata()}
                handle=handles[version['version']]
                handle.write(json.dumps(result,ensure_ascii=False,allow_nan=False)+'\n');handle.flush();os.fsync(handle.fileno())
                self.completed+=1
                self.tokens+=sum(int(response.usage.get(k) or 0) for k in ('input_tokens','output_tokens'))
                if self.completed%32==0:self.status(stage='predicting',event_split=event['split'])
        try:
            with runtime_target_context(self.target):
                for offset in range(0,len(tasks),64):
                    if self.tokens+min(64,len(tasks)-offset)*45_000>self.max_tokens:
                        raise RuntimeError('pre-registered token budget reached')
                    self.status(stage='predicting')
                    await asyncio.gather(*(one(e,v) for e,v in tasks[offset:offset+64]))
            write_json(self.root/'predictions_lock.json',{'completed':self.completed,'manifest_hash':digest(self.manifest),
                'locked_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                'journal_hashes':{v['version']:digest(read_jsonl(self.root/'predictions'/v['version']/'predictions.jsonl')) for v in self.versions}})
            self.status(stage='predictions_complete')
        finally:
            for h in handles.values():h.close()


async def main(args):
    experiment=WeeklyExperiment(args.root,concurrency=args.concurrency,max_tokens=args.max_tokens)
    with (args.root/'process.lock').open('w') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:await experiment.run()
        except Exception as exc:
            experiment.status(stage='interrupted',error_type=type(exc).__name__);raise

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--concurrency',type=int,default=16);p.add_argument('--max-tokens',type=int,default=40_000_000)
    asyncio.run(main(p.parse_args()))
