"""Deterministic weekly outcome judge, paired summaries and standalone HTML."""
from __future__ import annotations
import argparse
import collections
import datetime as dt
import html
import json
import math
import random
from pathlib import Path
from statistics import mean

from .prepare import read_jsonl,write_json,write_jsonl
from .rubric import digest


def metrics(rows):
    eligible=[r for r in rows if r['eligible']]
    n=len(eligible);available=sum(r['available'] for r in eligible)
    correct=sum(r['correct'] for r in eligible)
    by_class={d:[r for r in eligible if r['label']==d] for d in ('up','down')}
    recall={d:sum(r['correct'] for r in rs)/len(rs) if rs else None for d,rs in by_class.items()}
    # Per-event means prevent longer observable trajectories from overweighting events.
    per_event=collections.defaultdict(list)
    for r in eligible:per_event[r['event_id']].append(int(r['correct']))
    return {'n':n,'scheduled':len(rows),'eligible_coverage':n/len(rows) if rows else None,
        'correct':correct,'accuracy':correct/n if n else None,
        'available':available,'coverage':available/n if n else None,
        'answered_accuracy':correct/available if available else None,
        'balanced_accuracy':mean(recall.values()) if all(x is not None for x in recall.values()) else None,
        'recall_up':recall['up'],'recall_down':recall['down'],
        'always_up_accuracy':len(by_class['up'])/n if n else None,
        'always_down_accuracy':len(by_class['down'])/n if n else None,
        'flat_n':sum(r['label']=='flat' for r in eligible),
        'predicted_up':sum(r['available'] and r['pred_direction']=='up' for r in eligible),
        'predicted_down':sum(r['available'] and r['pred_direction']=='down' for r in eligible),
        'audit_violation_rate':sum(not r['audit_valid'] for r in eligible)/n if n else None,
        'event_macro_accuracy':mean(mean(rs) for rs in per_event.values()) if per_event else None,
        'event_n':len(per_event),'note':'invalid/missing/insufficient are incorrect; missing price has explicit independent exclusion'}


def paired(rows0,rows1):
    before={(r['event_id'],r['horizon']):r for r in rows0 if r['eligible']}
    grouped=collections.defaultdict(list);dates={};fixes=breaks=0
    for r in rows1:
        key=(r['event_id'],r['horizon'])
        if not r['eligible'] or key not in before:continue
        a=before[key];delta=int(r['correct'])-int(a['correct'])
        fixes+=delta==1;breaks+=delta==-1
        grouped[r['event_id']].append(delta);dates[r['event_id']]=r['event_date']
    if not grouped:return None
    event_values={eid:mean(vals) for eid,vals in grouped.items()}
    blocks=collections.defaultdict(list)
    for eid,v in event_values.items():blocks[dt.date.fromisoformat(dates[eid]).toordinal()//14].append(v)
    rng=random.Random(20260921);population=list(blocks.values());samples=[]
    for _ in range(2000):
        draw=[v for block in rng.choices(population,k=len(population)) for v in block]
        samples.append(mean(draw))
    samples.sort()
    return {'event_macro_delta':mean(event_values.values()),'ci95':[samples[50],samples[1949]],
        'independent_events':len(grouped),'calendar_blocks':len(blocks),
        'fixed_event_horizon_pairs':fixes,'regressed_event_horizon_pairs':breaks,
        'stable_positive_evidence':len(blocks)>=10 and samples[50]>0,
        'interpretation':'descriptive posthoc multiple-version/horizon comparison; no multiple-testing correction'}


def fmt(v,delta=False):
    return '—' if v is None else f'{v*100:+.2f}pp' if delta else f'{v:.2%}'


def generate(root:Path,output:Path):
    lock=json.loads((root/'predictions_lock.json').read_text())
    manifest=json.loads((root/'experiment_manifest.json').read_text())
    if lock['manifest_hash']!=digest(manifest):raise ValueError('manifest changed after forecast lock')
    events=read_jsonl(root/'events.jsonl');registry=json.loads((root/'versions.json').read_text())
    if digest(events)!=manifest['events_hash'] or digest(registry)!=manifest['versions_hash']:
        raise ValueError('events or policies changed')
    by_event={e['event_id']:e for e in events};all_predictions={}
    for v in registry:
        version=v['version'];pred=read_jsonl(root/'predictions'/version/'predictions.jsonl')
        if digest(pred)!=lock['journal_hashes'][version] or len(pred)!=len(events) or {p['event_id'] for p in pred}!=set(by_event):
            raise ValueError('all complete frozen prediction journals required before oracle read')
        all_predictions[version]=pred
    # First Oracle access is below this point, after ALL snapshots are complete.
    data_manifest=json.loads((root/'data_manifest.json').read_text())
    labels=read_jsonl(root/'oracle'/'weekly.labels.jsonl')
    if digest(labels)!=data_manifest['labels_hash']:raise ValueError('oracle hash mismatch')
    oracle={(r['event_id'],r['horizon']):r for r in labels}
    if len(oracle)!=len(labels):raise ValueError('duplicate oracle')
    judged=[];summary={};costs={}
    for version,predictions in all_predictions.items():
        costs[version]={k:sum(int(p['usage'].get(k) or 0) for p in predictions) for k in ('input_tokens','output_tokens')}
        for p in predictions:
            event=by_event[p['event_id']];by_h={r['horizon']:r for r in p['weekly_predictions']}
            for spec in event['schedule']:
                truth=oracle[(p['event_id'],spec['horizon'])];r=by_h.get(spec['horizon'],{})
                available=r.get('prediction_status')=='available' and r.get('pred_direction') in ('up','down') and not r.get('abstain',True)
                judged.append({'version':version,'event_id':p['event_id'],'event_date':event['event_time'][:10],
                    'split':event['split'],'horizon':spec['horizon'],'deadline':spec['calendar_deadline'],
                    'anchor_end':spec['anchor_end'],'eligible':truth['eligible'],'exclusion_reason':truth['reason'],
                    'label':truth.get('label'),'excess_return':truth.get('excess_return'),
                    'available':available,'pred_direction':r.get('pred_direction'),'confidence':r.get('confidence'),
                    'correct':bool(truth['eligible'] and available and r['pred_direction']==truth['label']),
                    'audit_valid':r.get('audit_valid',False)})
    horizons=sorted({r['horizon'] for r in judged if r['horizon']!='M3'})+['M3']
    for version in all_predictions:
        summary[version]={}
        for split in ('learn','validation','test','all'):
            rows=[r for r in judged if r['version']==version and (split=='all' or r['split']==split)]
            summary[version][split]={'overall':metrics(rows),
                'by_horizon':{h:metrics([r for r in rows if r['horizon']==h]) for h in horizons}}
    pairs=[('legacy','v1_r1'),('legacy','v1_r2'),('legacy','v1_r5'),('legacy','v2_r0'),
        ('v2_r0','v2_r3'),('v2_r3','v2_r4'),('v2_r3','v2_r5'),('v2_r0','v2_r5'),('legacy','v2_r5')]
    comparisons={}
    for a,b in pairs:
        comparisons[a+' → '+b]={}
        for split in ('learn','validation','test','all'):
            ra=[r for r in judged if r['version']==a and (split=='all' or r['split']==split)]
            rb=[r for r in judged if r['version']==b and (split=='all' or r['split']==split)]
            comparisons[a+' → '+b][split]={'overall':paired(ra,rb),
                'M3':paired([r for r in ra if r['horizon']=='M3'],[r for r in rb if r['horizon']=='M3'])}
    results={'summary':summary,'comparisons':comparisons,'costs':costs,'horizons':horizons,
        'data_eligibility':{k:data_manifest[k] for k in ['eligible_pairs','total_pairs','missing_reasons']},
        'versions':[{k:v[k] for k in ['version','historically_contract_valid','invalid_reason']} for v in registry],
        'forecast_repetitions':1,'fixed_information':True,'score_time':dt.datetime.now(dt.timezone.utc).isoformat(),
        'limitations':['No new evolution or promotion from these retrospective weekly scores',
          'Learn and validation already used for T+3 search; report test separately',
          'Test now used for all-version diagnostic comparison; cannot be reused as sealed selection data',
          'Historical disclosure revisions and LLM pretraining exposure not excluded',
          'One joint weekly forecast per event/version; no repeated stochastic estimation',
          'Cumulative horizons overlap; they are not independent samples; clustered intervals are descriptive',
          'Invalid v1 candidates use immutable weekly binary contract override; diagnostic only',
          'confidence is not a calibrated probability; no trading PnL claim']}
    write_json(root/'weekly_report.json',results);write_jsonl(root/'judged.jsonl',judged)
    lines=['# 各策略版本按周至三个月的准确率比较','',
        '**评估口径：每个事件入场收盘日起，截至各周截止日的累计超额收益方向；M3为三个月终点。**',
        '500条事件（学习300、验证100、测试100）、8个不同历史规则快照，全部补跑同一事实上下文的周线预测。每个事件/版本一次生成整条时间线；不能把多周视作独立样本。','',
        '## 测试集各版本比较','',
        '| 版本 | 全期限准确率 | 事件等权准确率 | 平衡准确率 | 回答覆盖率 | 审计违规率 | M3准确率 |',
        '| --- | --- | --- | --- | --- | --- | --- |']
    for version in summary:
        o=summary[version]['test']['overall'];m3=summary[version]['test']['by_horizon']['M3']
        lines.append('| '+version+' | '+' | '.join(fmt(x) for x in [o['accuracy'],o['event_macro_accuracy'],o['balanced_accuracy'],o['coverage'],o['audit_violation_rate'],m3['accuracy']])+' |')
    lines+=['','全期限准确率分母是各版本相同的可评分事件×期限，未回答/无效记错；行情不可评分的节点单独列出。事件等权指标先对同一事件各期限平均，避免期限较多的事件权重偏大。平衡准确率是上涨与下跌召回率的均值。','',
        '## 测试集逐周准确率','',
        '| 期限 | 可评分事件数 | '+' | '.join(summary)+' |','| --- | --- | '+' | '.join(['---']*len(summary))+' |']
    for h in horizons:
        n=summary['legacy']['test']['by_horizon'][h]['n']
        lines.append(f'| {h} | {n} | '+' | '.join(fmt(summary[v]['test']['by_horizon'][h]['accuracy']) for v in summary)+' |')
    lines+=['','W13只在三个月上限之前确有第13个完整自然周的事件中存在；其分母可能更小，不能与其他周直接混作同一总体。M3始终按每个事件自己的三个月截止日计算。','',
        '## 配对变化（测试集）','',
        '| 比较 | 事件等权准确率差 | 95%分块区间 | 修复/退化节点 | 独立事件/时间块 |',
        '| --- | --- | --- | --- | --- |']
    for pair,record in comparisons.items():
        d=record['test']['overall']
        if d:
            lines.append(f'| {pair} | {fmt(d["event_macro_delta"],True)} | [{fmt(d["ci95"][0],True)}, {fmt(d["ci95"][1],True)}] | {d["fixed_event_horizon_pairs"]}/{d["regressed_event_horizon_pairs"]} | {d["independent_events"]}/{d["calendar_blocks"]} |')
    lines+=['','区间按事件保留完整时间线，再按公告日期14日块重采样；没有把各周当独立样本。多版本多期限比较没有多重检验校正，作为诊断结果，不宣称已验证新的冠军策略。','',
        '## 学习、验证与测试分开查看','',
        '| 版本 | 学习事件等权准确率 | 验证事件等权准确率 | 测试事件等权准确率 | 输入/输出token |',
        '| --- | --- | --- | --- | --- |']
    for version in summary:
        lines.append('| '+version+' | '+' | '.join(fmt(summary[version][s]['overall']['event_macro_accuracy']) for s in ('learn','validation','test'))+f' | {costs[version]["input_tokens"]:,}/{costs[version]["output_tokens"]:,} |')
    lines+=['','## 时间线与版本说明','',
        '- 只改变期限任务与输出格式。事件正文、事前行情、模型及对应版本原始规则快照保持不变；不向模型提供周线实际行情。',
        '- 日期不统一：交易日公告从下一交易日收盘入场，周末公告从下一交易所收盘入场；每7个自然日一个截止日期，落在非交易日取之前最近交易所收盘。最后在入场日起满3个自然月封顶。',
        '- label用同一锚点的个股累计收益减沪深300累计收益，正为up、负为down；精确为零记flat且二分类不获命中。不得拿T+3预测直接当周预测。',
        '- 原始legacy与第二轮legacy是同一快照，去重。第二轮第1/2轮没有产生改动，不能伪造R1/R2。第一轮R1/R2原本违规，本次仅以固定二分类周线契约覆盖其输出状态指令作诊断，不是恢复其合法性。',
        '- 旧T+3封存执行因用户转向本次周线评估而停止，保留部分预测；没有声称旧T+3最终测试已经完成。',
        '- 本次所有预测落盘并锁定后才打开周线标签；没有用周线测试结果继续修改规则。',
        '- 置信度为未校准信心，审计是模型自报引用与约束检查，不代表因果证明。',
        '',f'全500条计划节点{data_manifest["total_pairs"]}个，其中可评分{data_manifest["eligible_pairs"]}个；不可评分原因：`{json.dumps(data_manifest["missing_reasons"],ensure_ascii=False)}`。',
        f'总token：**{sum(sum(c.values()) for c in costs.values()):,}**（不含开发smoke）。','',
        '完整逐事件逐周结果：`backtesting/self_evolving/weekly_3m_20260921/judged.jsonl`；摘要：`weekly_report.json`。']
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text('\n'.join(lines)+'\n')
    dashboard(root,results)
    write_json(root/'status.json',{'stage':'completed','completed':len(events)*len(registry),
        'updated_at':dt.datetime.now(dt.timezone.utc).isoformat(),'tokens':sum(sum(c.values()) for c in costs.values())})
    print(json.dumps({v:{k:summary[v]['test']['overall'][k] for k in ['accuracy','event_macro_accuracy','coverage','balanced_accuracy']} for v in summary},indent=2))


def dashboard(root,results):
    data=json.dumps(results,ensure_ascii=False).replace('</','<\\/')
    page='''<!doctype html><html lang="zh"><meta charset="utf-8"><title>周线规则版本比较</title>
<style>body{font:15px system-ui;max-width:1300px;margin:35px auto;background:#f5f7fa;color:#172b4d;padding:20px}h1{font-size:27px}select{padding:8px}table{border-collapse:collapse;width:100%;background:white}td,th{padding:9px;text-align:right;border-bottom:1px solid #dde3ec}td:first-child,th:first-child{text-align:left}svg{background:white;width:100%;height:400px}.note{color:#526278}#legend span{display:inline-block;margin:8px}</style>
<h1>8个规则版本 · 每周至3个月</h1><p class="note">同一事件上下文；截至各周累计超额收益方向。事件×期限重叠，不能当独立样本。</p>
<label>数据分区 <select id="split"><option value="test">测试100条</option><option value="validation">验证100条</option><option value="learn">学习300条</option><option value="all">全部500条（描述性）</option></select></label>
<div id="legend"></div><svg id="chart" viewBox="0 0 1200 400" aria-label="各版本逐周准确率曲线"></svg><div id="table"></div>
<p class="note">W13可能不是所有事件都存在；每列显示可评分N。M3为各自日历三个月截止点。缺失行情不缩短期限，缺失预测记错。第一轮R1/R2仅诊断对照。单次整条时间线预测，未估计模型重复调用波动。</p>
<script>const D=__DATA__;const colors=['#475569','#ad1457','#8e24aa','#ef6c00','#1565c0','#00897b','#689f38','#d32f2f'];
const versions=Object.keys(D.summary),hs=D.horizons;function pct(x){return x==null?'—':(x*100).toFixed(2)+'%'}
function render(){const split=document.querySelector('#split').value;let svg='';for(let y=0;y<=100;y+=20){let py=350-y*3;svg+=`<line x1="65" y1="${py}" x2="1160" y2="${py}" stroke="#e5eaf0"/><text x="12" y="${py+5}">${y}%</text>`}
hs.forEach((h,i)=>{let x=75+i*1080/(hs.length-1);svg+=`<text x="${x-12}" y="380">${h}</text>`});
versions.forEach((v,j)=>{let points=[];hs.forEach((h,i)=>{let m=D.summary[v][split].by_horizon[h];if(m.accuracy!=null){let x=75+i*1080/(hs.length-1),y=350-m.accuracy*300;points.push(`${x},${y}`);svg+=`<circle cx="${x}" cy="${y}" r="3" fill="${colors[j]}"><title>${v} ${h}: ${pct(m.accuracy)} N=${m.n}</title></circle>`}});svg+=`<polyline points="${points.join(' ')}" fill="none" stroke="${colors[j]}" stroke-width="2"/>`});
document.querySelector('#chart').innerHTML=svg;document.querySelector('#legend').innerHTML=versions.map((v,j)=>`<span style="color:${colors[j]}">● ${v}</span>`).join('');let t='<table><tr><th>版本</th>'+hs.map(h=>`<th>${h}<br>N=${D.summary.legacy[split].by_horizon[h].n}</th>`).join('')+'<th>事件等权</th><th>覆盖率</th></tr>';for(let v of versions){let s=D.summary[v][split];t+='<tr><td>'+v+'</td>'+hs.map(h=>'<td>'+pct(s.by_horizon[h].accuracy)+'</td>').join('')+'<td>'+pct(s.overall.event_macro_accuracy)+'</td><td>'+pct(s.overall.coverage)+'</td></tr>'}document.querySelector('#table').innerHTML=t+'</table>'}document.querySelector('#split').onchange=render;render();</script></html>'''
    (root/'weekly_comparison.html').write_text(page.replace('__DATA__',data))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();generate(a.root,a.output)
