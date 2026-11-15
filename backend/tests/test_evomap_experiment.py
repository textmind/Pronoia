"""Protocol-boundary tests; synthetic model scores are never published as results."""
import asyncio
import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.evolution.corpus import atomic_json
from app.evolution.experiment import Experiment
from app.evolution.identity import implementation_hash
from app.evolution.policy import GeneLibrary
from app.evolution.runtime import evolution_context, current_context, prompt_context
from app.evolution.score import score
from app.event_backtest.evolution.rubric import digest
from app.event_backtest.evolution.structured import INITIAL_STRUCTURED_RULES
from app.event_backtest.models import TeamPrediction


def make_dataset(root, count=10):
    hashes = {}
    for split, year in [('learn', 2023), ('validation', 2024), ('test', 2025)]:
        events, labels = [], []
        for i in range(count):
            eid = f'{split}_{i}'
            events.append({'event_id': eid, 'group_id': eid, 'market': 'CN', 'symbol': f'{600000+i}',
                'event_time': f'{year}-{i+1:02d}-10T18:00:00+08:00', 'event_type_l2': '财报',
                'title': '业绩预告（预增）', 'event_text': '预计主营利润增长50%。', 'source_url': 'https://example.org/notice'})
            labels.append({'event_id': eid, 'label_t3': 'up' if i % 2 else 'down',
                'label_available_at': f'{year}-{i+1:02d}-20T15:00:00+08:00'})
        for filename, rows in [(f'{split}.events.jsonl', events), (f'oracle/{split}.labels.jsonl', labels)]:
            path = root / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in rows))
        hashes[split] = {'events': digest(events), 'labels': digest(labels)}
    atomic_json(root/'manifest.json', {'counts': {s:count for s in hashes}, 'hashes': hashes})
    return root


def fake_profile(root):
    atomic_json(root/'model.private.json', {'id':'synthetic', 'name':'Unit fixture', 'model_id':'synthetic',
        'provider':'openai-compatible', 'base_url':'https://example.invalid/v1', 'max_output_tokens':4096,
        'timeout_seconds':3, 'capabilities':{}, 'secret_env_ref':'fixture-never-resolved'})


def test_context_is_task_local_and_restored_after_failure():
    async def one(name):
        with evolution_context([{'id':name,'asset_id':name,'strategy':[name]}]):
            await asyncio.sleep(0)
            assert current_context()['genes'][0]['id'] == name
            assert name in prompt_context()
        assert current_context() is None
    async def run():
        await asyncio.gather(one('first'), one('second'))
    asyncio.run(run())
    with pytest.raises(RuntimeError), evolution_context([]):
        raise RuntimeError('fixture')
    assert prompt_context() == ''


def test_official_case_recall_excludes_future_heldout_and_current_event(tmp_path):
    library = GeneLibrary(tmp_path)
    compiled = library.compile()
    event = {'event_id':'now','market':'CN','event_time':'2024-06-01', 'event_text':'利润增长'}
    def case(eid, split, stamp):
        return library.engine.seal({'type':'Capsule','id':eid,'trigger':['financial_research'], 'gene':compiled['records'][0]['gene']['id'],
            'summary':'Synthetic recall fixture', 'blast_radius':{'files':0,'lines':0},
            'confidence':.7, 'outcome':{'status':'success','score':1}, 'content':{
                'source_split':split,'event_id':eid,'outcome_observed_at':stamp}})
    cases = [case('past','learn','2024-05-01T15:00:00+08:00'), case('future','learn','2024-07-01T15:00:00+08:00'),
        case('heldout','test','2024-05-01T15:00:00+08:00'), case('now','learn','2024-05-01T15:00:00+08:00')]
    routed = library.route(compiled,event,capsules=cases)
    assert routed['eligible_capsules'] == 1
    assert len(routed['genes']) == 4
    assert routed['capsule']['id'] == 'past'


def test_scorer_requires_lock_and_immutable_inputs(tmp_path):
    dataset = make_dataset(tmp_path/'data', 2)
    run = tmp_path/'run'
    manifest = {'implementation_hash':implementation_hash()}
    atomic_json(run/'manifest.json', manifest)
    config = {'version':'g0','library_hash':'fixture','dataset_hash':digest(json.loads((dataset/'manifest.json').read_text())), 'experiment_hash':digest(manifest)}
    path = run/'runs/g0/test/predictions.jsonl'
    atomic_json(path.with_name('config.json'), config)
    path.write_text(json.dumps({'event_id':'test_0','pred_direction':'down','abstain':False,'strategy_metadata':{'prediction_status':'available','rule_audit':{'valid':True}}})+'\n')
    with pytest.raises(FileNotFoundError):
        score(dataset,run,'test',path,path.with_name('metrics.private.json'))
    atomic_json(run/'final_lock.json', {'experiment_hash':digest(manifest),'versions':{'g0':'fixture'}})
    result = score(dataset,run,'test',path,path.with_name('metrics.private.json'))
    assert result['score'] == .5 and result['n'] == 2 and result['missing_predictions'] == 1
    assert 'outcomes' not in result
    path_events=dataset/'test.events.jsonl'
    path_events.write_text(path_events.read_text().replace('主营', '其他'))
    with pytest.raises(ValueError, match='packets changed'):
        score(dataset,run,'test',path,path.with_name('metrics.private.json'))


def test_complete_feedback_round_and_cached_restart_without_extra_model_calls(tmp_path):
    dataset = make_dataset(tmp_path/'data')
    root = tmp_path/'run'
    fake_profile(root)
    predictions, optimizer = [], []
    async def fake_prediction(events, *, run_id, **kwargs):
        event = events[0]
        i = int(event.event_id.rsplit('_',1)[1])
        version = run_id.split(':')[1]
        actual = 'up' if i % 2 else 'down'
        threshold = 8 if version == 'g1' else 6
        direction = actual if i < threshold else ('down' if actual == 'up' else 'up')
        predictions.append(run_id)
        return [TeamPrediction(event_id=event.event_id, pred_direction=direction, run_id=run_id,
            confidence=.6,rationale='Synthetic fixture only',tokens_in=100,tokens_out=20,
            strategy_metadata={'prediction_status':'available','rule_audit':{'valid':True}})]
    async def fake_agent(system, user, **kwargs):
        payload = json.loads(user)
        optimizer.append(payload)
        assert 'test_0' not in user and 'validation_0' not in user and 'outcomes' not in user
        if 'learning_examples' in payload:
            errors = [e['event_id'] for e in payload['learning_examples'] if not e['correct']]
            controls = [e['event_id'] for e in payload['learning_examples'] if e['correct']]
            value = {'patterns':[{'id':'P1','event_ids':errors[:3],'counterexamples':controls[:1]}]}
        else:
            rule = copy.deepcopy(INITIAL_STRUCTURED_RULES[0])
            rule['action']['analysis'].append('operating_quality')
            value = {'edits':[{'op':'update','rule_id':'R01','pattern_ids':['P1'],'rule':rule}]}
        return SimpleNamespace(value=value,usage={'total_tokens':50})
    with patch('app.evolution.experiment.model_profile_context',return_value=nullcontext()), \
         patch('app.evolution.experiment.run_team_prompt',side_effect=fake_prediction), \
         patch('app.evolution.experiment.complete_json_diagnostic',side_effect=fake_agent), \
         patch('app.evolution.experiment.config.LLM_RPS_INTERVAL_S',0,create=True):
        result = asyncio.run(Experiment(dataset,root,rounds=1,concurrency=2,max_tokens=1_000_000).run())
        assert result['chosen'] == 'g1'
        assert result['history'][-1]['accepted']
        assert result['test']['g1']['score'] == .8
        assert (root/'accuracy.png').is_file() and (root/'accuracy.svg').is_file()
        assert len(list((root/'corpus/feedback').glob('*.json'))) == 30
        snapshot=GeneLibrary(root/'assets').engine.call('snapshot')
        assert any(e.get('meta',{}).get('selection_stage')=='applied' for e in snapshot['events'])
        assert any(e['type']=='cycle.solidified' for e in snapshot['root_events'])
        assert any(c['id'].startswith('capsule_case_') for c in snapshot['capsules'])
        n_calls=(len(predictions),len(optimizer))
        repeated=asyncio.run(Experiment(dataset,root,rounds=1,concurrency=2,max_tokens=1_000_000).run())
        assert repeated == result and n_calls == (len(predictions),len(optimizer))


def test_budget_is_reserved_before_network_and_unknown_usage_remains_charged(tmp_path):
    from app.evolution.meter import UsageMeter, metered_client, meter_context
    called=[]
    class Client:
        async def create(self, **kwargs):
            called.append(kwargs)
            return SimpleNamespace(usage={'prompt_tokens':20,'completion_tokens':30,'total_tokens':50})
    meter=UsageMeter(tmp_path/'usage',6000)
    async def run():
        client=Client()
        assert metered_client(client) is client
        with meter_context(meter):
            await metered_client(client).create(messages=[{'role':'user','content':'hello'}],max_tokens=100)
            assert meter.total()==50
            with pytest.raises(RuntimeError,match='budget reached'):
                await metered_client(client).create(messages=[],max_tokens=10000)
        receipt=meter.begin({'messages':[],'max_tokens':100})
        assert meter.total()>50
        assert json.loads(receipt.read_text())['source']=='conservative_reservation'
    asyncio.run(run())
    assert len(called)==1


def test_successful_usage_free_stream_settles_reservation_by_observed_bytes(tmp_path):
    from app.evolution.meter import UsageMeter, metered_client, meter_context
    class Chunks:
        def __init__(self):
            self.values=iter([SimpleNamespace(usage=None,choices=[{'delta':{'content':'first'}}]),
                              SimpleNamespace(usage=None,choices=[{'delta':{'content':'second'}}])])
        def __aiter__(self):
            return self
        async def __anext__(self):
            try:return next(self.values)
            except StopIteration:raise StopAsyncIteration from None
    class Client:
        async def create(self,**kwargs):
            assert kwargs['stream_options']['include_usage'] is True
            return Chunks()
    meter=UsageMeter(tmp_path/'usage',100_000)
    async def run():
        with meter_context(meter,task_id='stream-task'):
            stream=await metered_client(Client()).create(messages=[{'role':'user','content':'hello'}],
                                                         max_tokens=20_000,stream=True)
            async for _ in stream:pass
    asyncio.run(run())
    receipt=json.loads(next((tmp_path/'usage').glob('*.json')).read_text())
    assert receipt['completed'] is True and receipt['source']=='conservative_completed_bytes'
    assert receipt['charged_tokens']<receipt['reserved_tokens']
    assert receipt['observed_output_bytes']>0 and receipt['task_id']=='stream-task'


def test_interrupted_usage_free_stream_keeps_maximum_reservation(tmp_path):
    from app.evolution.meter import UsageMeter, metered_client, meter_context
    class Broken:
        def __aiter__(self):return self
        async def __anext__(self):raise RuntimeError('transport interrupted')
    class Client:
        async def create(self,**kwargs):return Broken()
    meter=UsageMeter(tmp_path/'usage',100_000)
    async def run():
        with meter_context(meter):
            stream=await metered_client(Client()).create(messages=[],max_tokens=10_000,stream=True)
            with pytest.raises(RuntimeError,match='transport interrupted'):
                await stream.__anext__()
    asyncio.run(run())
    receipt=json.loads(next((tmp_path/'usage').glob('*.json')).read_text())
    assert receipt['completed'] is False and receipt['charged_tokens']==receipt['reserved_tokens']


def test_repeated_candidate_round_feedback_has_distinct_immutable_attempt_ids(tmp_path):
    library=GeneLibrary(tmp_path/'assets');parent=library.compile()
    rules=copy.deepcopy([r['rule'] for r in parent['records']]);rules[0]['action']['signal_multiplier']=.5
    candidate=library.compile(rules,generation=1,parent=parent['hash'],evidence=['P1'])
    experiment=object.__new__(Experiment);experiment.library=library
    experiment.manifest={'dataset_hash':'fixture'};experiment.experiment_hash='fixture-experiment'
    for version,score_value in [('g1',.4),('g2',.425)]:
        experiment.round_feedback(candidate,parent,{'score':score_value,'n':40},{'accepted':False},{'id':'mutation_fixture'},version=version)
    events=library.engine.call('snapshot')['events']
    assert len(events)==2 and events[0]['id']!=events[1]['id']
    experiment.round_feedback(candidate,parent,{'score':.425,'n':40},{'accepted':False},{'id':'mutation_fixture'},version='g2')
    assert len(library.engine.call('snapshot')['events'])==2


def test_baseline_reuse_preserves_frozen_predictions_and_prior_charges(tmp_path):
    from app.evolution.baseline import archive_protocol,seed_baseline
    dataset=make_dataset(tmp_path/'data',2);source=tmp_path/'source';fake_profile(source)
    calls=[]
    async def fake_prediction(events,**kwargs):
        calls.append(events[0].event_id)
        return [TeamPrediction(event_id=events[0].event_id,pred_direction='up',run_id=kwargs['run_id'],
            tokens_in=100,tokens_out=20,strategy_metadata={'prediction_status':'available','rule_audit':{'valid':True}})]
    with patch('app.evolution.experiment.model_profile_context',return_value=nullcontext()), \
         patch('app.evolution.experiment.run_team_prompt',side_effect=fake_prediction), \
         patch('app.evolution.experiment.config.LLM_RPS_INTERVAL_S',0,create=True):
        original=Experiment(dataset,source,rounds=1,concurrency=1,max_tokens=1_000_000)
        initial=original.library.compile();archive_protocol(source)
        first,_=asyncio.run(original.predict('baseline',initial,'learn',baseline=True))
        atomic_json(source/'request_usage/provider.json',{'charged_tokens':150,'source':'provider_usage'})
        atomic_json(source/'request_usage/inflight.json',{'charged_tokens':500,'source':'conservative_reservation'})
        target=tmp_path/'target'
        successor=Experiment(dataset,target,rounds=1,concurrency=1,max_tokens=1_000_000,baseline_from=source)
        compiled=successor.library.compile();seed_baseline(successor,compiled)
        reused,_=asyncio.run(successor.predict('baseline',compiled,'learn',baseline=True))
        assert first==reused and calls==['learn_0','learn_1']
        assert successor.usage()==650
        atomic_json(target/'request_usage/new.json',{'charged_tokens':77})
        seed_baseline(successor,compiled)
        assert successor.usage()==727
        assert json.loads((target/'baseline-reuse.json').read_text())['carried_request_budget']==650
        atomic_json(source/'final_lock.json',{'versions':{}})
        with pytest.raises(ValueError,match='test-exposed'):
            Experiment(dataset,tmp_path/'forbidden',rounds=1,concurrency=1,max_tokens=1_000_000,baseline_from=source)


def test_no_op_mutation_is_not_retested_for_a_noise_gain(tmp_path):
    library=GeneLibrary(tmp_path/'assets');parent=library.compile()
    experiment=object.__new__(Experiment);experiment.library=library;experiment.root=tmp_path
    experiment.events=lambda split:[{}]*10
    calls=[]
    async def agent(filename,system,payload):
        calls.append(filename)
        if 'learning_examples' in payload:
            return {'patterns':[{'id':'P1','event_ids':['e1','e2','e3'],'counterexamples':['success']}]}
        return {'edits':[{'op':'update','rule_id':'R01','pattern_ids':['P1'],'rule':copy.deepcopy(INITIAL_STRUCTURED_RULES[0])}]}
    experiment.agent=agent
    examples=[{'event_id':f'e{i}','date':f'2023-01-0{i}','group_id':f'g{i}','correct':False} for i in [1,2,3]]
    examples.append({'event_id':'success','correct':True})
    candidate,reason=asyncio.run(experiment.propose(1,parent,examples,{},{}))
    assert candidate is None and reason['reason']=='mutation_validation_failed'
    assert len(calls)==4  # attribution plus three bounded proposal/repair attempts


def test_attribution_counterexample_annotations_resolve_to_sampled_event(tmp_path):
    library = GeneLibrary(tmp_path/'assets')
    parent = library.compile()
    experiment = object.__new__(Experiment)
    experiment.library = library
    experiment.root = tmp_path
    experiment.events = lambda split: [{}] * 10
    examples = [
        {'event_id': f'e{i}', 'date': f'2023-01-0{i}', 'group_id': f'g{i}', 'correct': False}
        for i in [1, 2, 3]
    ] + [{'event_id': 'success', 'date': '2023-01-04', 'group_id': 'g4', 'correct': True}]

    async def agent(filename, system, payload):
        if 'learning_examples' in payload:
            return {'patterns': [{
                'id': 'P1',
                'event_ids': ['e1', 'e2', 'e3'],
                'counterexamples': ['success（2023-01-04，带解释）'],
            }]}
        rule = copy.deepcopy(INITIAL_STRUCTURED_RULES[0])
        rule['action']['analysis'].append('operating_quality')
        return {'edits': [{'op': 'update', 'rule_id': 'R01', 'pattern_ids': ['P1'], 'rule': rule}]}

    experiment.agent = agent
    candidate, reason = asyncio.run(experiment.propose(1, parent, examples, {}, {}))
    assert candidate is not None
    assert reason['patterns'] == 1


def test_rule_deletion_keeps_measured_round_event_without_single_gene_credit(tmp_path):
    library=GeneLibrary(tmp_path/'assets');parent=library.compile()
    candidate=library.compile([r['rule'] for r in parent['records']][1:],generation=1,parent=parent['hash'],evidence=['P1'])
    experiment=object.__new__(Experiment);experiment.library=library
    experiment.manifest={'dataset_hash':'fixture'};experiment.experiment_hash='fixture-experiment'
    experiment.round_feedback(candidate,parent,{'score':.7,'n':40,'correct':28},{'accepted':True},{'id':'mutation_deletion'},version='g1')
    snapshot=library.engine.call('snapshot')
    assert len(snapshot['events'])==1 and snapshot['events'][0]['meta']['memory_eligible'] is False
    assert len(snapshot['capsules'])==1
    assert not [r for r in snapshot['memory'] if r['kind']=='outcome']
