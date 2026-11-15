"""Local experiment catalog and bounded, independent worker dispatch."""
from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import uuid
import datetime as dt
from contextlib import contextmanager
from pathlib import Path

from .. import config, settings
from .corpus import atomic_json
from .experiment import Experiment
from .identity import implementation_hash
from .gep import EvoMap

RUNS = config.PROJECT_ROOT / 'backtesting' / 'evomap'
DATA = Path(config.DATA_DIR)


def active_policy_path():
    return RUNS / 'active-policy.json'


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def run_path(identity):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}', identity):
        raise ValueError('无效的实验编号')
    path = (RUNS / identity).resolve()
    if not path.is_relative_to(RUNS.resolve()) or not (path/'manifest.json').is_file():
        raise FileNotFoundError('实验不存在')
    return path


def active(path):
    lockpath = path/'.experiment.lock'
    if lockpath.exists():
        with lockpath.open('a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    worker = read(path/'worker.json', {})
    if worker.get('pid'):
        args = subprocess.run(['ps','-p',str(int(worker['pid'])),'-o','args='],capture_output=True,text=True).stdout
        return 'app.evolution.experiment' in args and str(path) in args
    return False


def datasets():
    items=[]
    for path in sorted(DATA.glob('evomap_*/manifest.json')):
        data=read(path,{})
        if not data.get('hashes'):
            continue
        items.append({'id':path.parent.name,'counts':data.get('counts'), 'issuer_disjoint':data.get('issuer_disjoint'),
                      'stats':data.get('stats'), 'as_of':data.get('as_of'), 'label_horizon':'t3'})
    return items


def summary(path):
    manifest=read(path/'manifest.json',{})
    status=read(path/'status.json',{})
    running=active(path)
    successor=read(path/'superseded.json',{}).get('run_id')
    if not running and status.get('stage') not in {'complete','interrupted','cancelled'}:
        status={**status,'stage':'interrupted','message':'后台进程已停止，已保存的预测保留。'}
    if successor:
        status={**status,'stage':'interrupted','message':'修复后的实验已复用此处基线，继续运行：'+successor}
    return {'id':path.name,'model':manifest.get('model'), 'dataset':Path(manifest.get('dataset_path','')).name,
            'rounds':manifest.get('rounds'), 'mode':manifest.get('mode'), 'max_tokens':manifest.get('max_tokens'),
            'repetitions':manifest.get('final_repetitions',1), 'engine_version':(manifest.get('engine') or {}).get('engine_version'),
            'status':{**status,'running':running}, 'has_result':(path/'result.json').exists(), 'superseded_by':successor,
            'activation_eligible':bool((read(path/'result.json',{}) or {}).get('activation_eligible')),
            'can_resume':not running and status.get('stage')!='complete' and manifest.get('implementation_hash')==implementation_hash()}


def list_runs():
    return [summary(p.parent) for p in sorted(RUNS.glob('*/manifest.json'),key=lambda p:p.stat().st_mtime,reverse=True)]


@contextmanager
def catalog_lock():
    RUNS.mkdir(parents=True,exist_ok=True)
    with (RUNS/'.jobs.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield


def ensure_idle():
    if any(active(p.parent) for p in RUNS.glob('*/manifest.json')):
        raise ValueError('已有自进化实验运行中，请等待它完成')


def launch_locked(path):
    if active(path):
        return summary(path)
    manifest=read(path/'manifest.json')
    if manifest['implementation_hash']!=implementation_hash():
        raise ValueError('执行代码已变化，请新建实验以保持结果可审计')
    if (path/'result.json').exists():
        raise ValueError('实验已完成，无需重新运行')
    ensure_idle()
    command=[sys.executable,'-m','app.evolution.experiment','--dataset',manifest['dataset_path'],'--out',str(path),
             '--rounds',str(manifest['rounds']),'--concurrency',str(manifest['concurrency']),
             '--max-tokens',str(manifest['max_tokens']),'--mode',manifest['mode'],
             '--repetitions',str(manifest.get('final_repetitions',2)),
             '--refresh-count',str(manifest.get('accepted_learning_refresh_count',50))]
    with (path/'worker.log').open('a') as log:
        process=subprocess.Popen(command,cwd=config.PROJECT_ROOT/'backend',stdout=log,stderr=subprocess.STDOUT,
                                 env={**os.environ,'PYTHONPATH':str(config.PROJECT_ROOT/'backend')},start_new_session=True)
    atomic_json(path/'worker.json',{'pid':process.pid})
    return summary(path)


def launch(path):
    with catalog_lock():
        return launch_locked(path)


def create(dataset_id, *, rounds, concurrency, max_tokens, mode, repetitions=2, refresh_count=50):
    settings.require_model()
    if dataset_id not in {d['id'] for d in datasets()}:
        raise ValueError('请选择已准备并冻结的数据集')
    with catalog_lock():
        ensure_idle()
        path=RUNS/('evomap_'+uuid.uuid4().hex[:12])
        Experiment(DATA/dataset_id,path,rounds=rounds,concurrency=concurrency,max_tokens=max_tokens,mode=mode,
                   repetitions=repetitions,refresh_count=refresh_count)
        return launch_locked(path)


def cancel(identity):
    path=run_path(identity)
    with catalog_lock():
        if not active(path):
            raise ValueError('实验当前未运行')
        worker=read(path/'worker.json',{})
        pid=int(worker.get('pid') or 0)
        if not pid:
            raise ValueError('找不到可停止的后台进程')
        os.killpg(pid,signal.SIGTERM)
        atomic_json(path/'status.json',{'at':dt.datetime.now(dt.timezone.utc).isoformat(),'stage':'cancelled',
                                        'message':'已手动停止；冻结预测和检查点均已保留。'})
        return summary(path)


def active_policy():
    state=read(active_policy_path(),{'schema_version':1,'active':None,'history':[]})
    return {'active':state.get('active'),'history':state.get('history',[])[-20:]}


def activate(identity, version=None):
    path=run_path(identity)
    if active(path):
        raise ValueError('实验仍在运行，不能激活策略')
    result=read(path/'result.json')
    final_lock=read(path/'final_lock.json')
    if not result or not result.get('activation_eligible'):
        raise ValueError('该实验未通过独立测试改善门槛，不能激活')
    chosen=version or result.get('chosen')
    if chosen != result.get('chosen') or chosen not in final_lock.get('versions',{}):
        raise ValueError('只能激活验证集预注册并通过测试的获胜版本')
    with catalog_lock():
        state=read(active_policy_path(),{'schema_version':1,'active':None,'history':[]})
        entry={'run_id':identity,'version':chosen,'library_hash':final_lock['versions'][chosen],
               'experiment_hash':result['experiment_hash'],'activated_at':dt.datetime.now(dt.timezone.utc).isoformat(),
               'previous':state.get('active')}
        history=[*state.get('history',[]),{'action':'activate','entry':entry}]
        atomic_json(active_policy_path(),{'schema_version':1,'active':entry,'history':history})
        return active_policy()


def rollback_policy():
    with catalog_lock():
        state=read(active_policy_path(),{'schema_version':1,'active':None,'history':[]})
        current=state.get('active')
        if not current:
            raise ValueError('当前没有已激活策略')
        previous=current.get('previous')
        history=[*state.get('history',[]),{'action':'rollback','from':current,'to':previous,
                                           'at':dt.datetime.now(dt.timezone.utc).isoformat()}]
        atomic_json(active_policy_path(),{'schema_version':1,'active':previous,'history':history})
        return active_policy()


def detail(identity):
    path=run_path(identity)
    libraries=sorted([read(p) for p in (path/'assets/libraries').glob('*.json')],key=lambda x:(x['generation'],x['hash']))
    feedback=[read(p) for p in sorted((path/'corpus/feedback').glob('*.json'))]
    tasks=[]
    for split in ('learn','validation','test'):
        for p in (path/'corpus/tasks'/split).glob('*.json'):
            item=read(p)
            tasks.append({'task_id':item['task_id'],'split':split,'event_id':item['event']['event_id'],
                          'event':item['event'],'trace':item['trace'],
                          'title':item['event']['title'],'symbol':item['event']['symbol'],'generation':item['generation'],
                          'genes':item['genes'],'prediction':item['prediction'],'content_hash':item['content_hash']})
    rounds=[]
    for folder in sorted((path/'rounds').glob('*')):
        if folder.is_dir() and folder.name.isdigit():
            candidate=read(folder/'candidate.json')
            proposals=sorted(folder.glob('proposal*.json'))
            proposals=[p for p in proposals if not p.name.endswith(('.request.json','.usage.json'))]
            rounds.append({'round':int(folder.name),'patterns':read(folder/'patterns.validated.json',[]),
                           'mutation':read(folder/'mutation.json'),'candidate_hash':candidate['hash'] if candidate else None,
                           'proposals':[{'attempt':p.stem,'value':read(p)} for p in proposals]})
    return {**summary(path),'manifest':read(path/'manifest.json'), 'curve':read(path/'curve.json'),
            'result':read(path/'result.json'), 'history':read(path/'history.json',[]), 'libraries':libraries,
            'corpus':tasks,'feedback':feedback,'round_details':rounds,'final_lock':read(path/'final_lock.json'),
            'active_policy':active_policy().get('active')}


def assets(identity, *, limit=200):
    path=run_path(identity)
    snapshot=EvoMap(path/'assets/runtime').call('snapshot')
    collections={key:rows for key,rows in snapshot.items() if isinstance(rows,list)}
    return {'counts':{key:len(rows) for key,rows in collections.items()},
            'items':{key:rows[-limit:] for key,rows in collections.items()},
            'personality':snapshot.get('personality'), 'experience':snapshot.get('experience'),
            'protocol_version':snapshot.get('protocol_version'), 'limit':limit}


ARTIFACTS={'accuracy.png':'image/png','accuracy.svg':'image/svg+xml','accuracy.csv':'text/csv',
           'curve.json':'application/json','report.html':'text/html','result.json':'application/json',
           'test_order.json':'application/json'}
