"""API boundaries for costly dispatch, heldout isolation and artifact access."""
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.evolution import service
from app.evolution.corpus import atomic_json
from app.evolution.identity import implementation_hash
from app.routes.evolution import router


def setup(tmp_path,monkeypatch):
    root=tmp_path/'runs'; data=tmp_path/'data'
    monkeypatch.setattr(service,'RUNS',root)
    monkeypatch.setattr(service,'DATA',data)
    app=FastAPI();app.include_router(router)
    return TestClient(app),root,data


def test_artifacts_are_allowlisted_and_do_not_serve_private_model_or_labels(tmp_path,monkeypatch):
    client,root,data=setup(tmp_path,monkeypatch)
    run=root/'fixture'
    atomic_json(run/'manifest.json',{'model':{'model_id':'synthetic'},'implementation_hash':implementation_hash()})
    atomic_json(run/'model.private.json',{'secret_env_ref':'never-expose-this'})
    atomic_json(run/'oracle/test.labels.jsonl',{'label_t3':'up'})
    (run/'accuracy.csv').write_text('round,accuracy\n0,0.5\n')
    for filename in ['model.private.json','test.labels.jsonl','worker.log','manifest.json']:
        assert client.get(f'/api/evolution/runs/fixture/artifacts/{filename}').status_code==404
    assert client.get('/api/evolution/runs/fixture/artifacts/accuracy.csv').status_code==200
    detail=client.get('/api/evolution/runs/fixture')
    assert detail.status_code==200 and 'never-expose-this' not in detail.text
    assert 'secret_env_ref' not in client.get('/api/evolution/runs').text


def test_create_rejects_fractional_limits_and_unready_model_before_dispatch(tmp_path,monkeypatch):
    client,root,data=setup(tmp_path,monkeypatch)
    for extra in [{'rounds':1.5},{'concurrency':0},{'max_tokens':True}]:
        response=client.post('/api/evolution/runs',json={'dataset_id':'evomap_fixture',**extra})
        assert response.status_code==422
    from fastapi import HTTPException
    def require():
        raise HTTPException(409,detail={'code':'MODEL_NOT_READY','settings_url':'/settings/models'})
    monkeypatch.setattr(service.settings,'require_model',require)
    response=client.post('/api/evolution/runs',json={'dataset_id':'evomap_fixture'})
    assert response.status_code==409 and response.json()['detail']['code']=='MODEL_NOT_READY'
    assert not root.exists()


def test_stale_protocol_cannot_resume_and_no_new_worker_is_spawned(tmp_path,monkeypatch):
    client,root,data=setup(tmp_path,monkeypatch)
    atomic_json(root/'stale/manifest.json',{'implementation_hash':'previous-implementation'})
    response=client.post('/api/evolution/runs/stale/resume')
    assert response.status_code==409
    assert not (root/'stale/worker.json').exists()


def test_active_experiment_rejects_create_before_snapshot_or_orphan_directory(tmp_path,monkeypatch):
    client,root,data=setup(tmp_path,monkeypatch)
    atomic_json(data/'evomap_fixture/manifest.json',{'hashes':{'learn':{}},'counts':{'learn':1}})
    atomic_json(root/'existing/manifest.json',{'implementation_hash':implementation_hash()})
    monkeypatch.setattr(service.settings,'require_model',lambda:{'id':'fixture'})
    monkeypatch.setattr(service,'active',lambda path:True)
    def forbidden(*a,**k):
        raise AssertionError('must not freeze another model or create an experiment')
    monkeypatch.setattr(service,'Experiment',forbidden)
    response=client.post('/api/evolution/runs',json={'dataset_id':'evomap_fixture'})
    assert response.status_code==409
    assert sorted(p.name for p in root.iterdir())==['.jobs.lock','existing']


def test_official_assets_are_visible_without_private_run_files(tmp_path,monkeypatch):
    client,root,data=setup(tmp_path,monkeypatch)
    atomic_json(root/'fixture/manifest.json',{'implementation_hash':implementation_hash()})
    from app.evolution.policy import GeneLibrary
    library=GeneLibrary(root/'fixture/assets');compiled=library.compile()
    response=client.get('/api/evolution/runs/fixture/assets?limit=2')
    assert response.status_code==200,response.text
    value=response.json()
    assert value['counts']['genes']==10
    assert len(value['items']['genes'])==2
    assert value['items']['genes'][0]['type']=='Gene'
    assert 'secret_env_ref' not in response.text and 'api_key' not in response.text
    assert client.get('/api/evolution/runs/fixture/assets?limit=1001').status_code==422


def test_runtime_missing_is_not_misreported_as_an_unknown_experiment(tmp_path,monkeypatch):
    client,root,data=setup(tmp_path,monkeypatch)
    from app.evolution.gep import GEPError
    def unavailable(*a,**k):
        raise GEPError('EvoMap 需要 Node.js 22.12 或更高版本')
    monkeypatch.setattr(service,'assets',unavailable)
    response=client.get('/api/evolution/runs/fixture/assets')
    assert response.status_code==503 and 'Node.js' in response.text


def test_round_diagnostics_do_not_overwrite_numeric_execution_limit(tmp_path,monkeypatch):
    client,root,data=setup(tmp_path,monkeypatch)
    atomic_json(root/'fixture/manifest.json',{'implementation_hash':implementation_hash(),'rounds':5,'model':{'model_id':'fixture'}})
    atomic_json(root/'fixture/rounds/01/patterns.validated.json',[{'id':'P1'}])
    response=client.get('/api/evolution/runs/fixture')
    assert response.status_code==200
    assert response.json()['rounds']==5
    assert response.json()['round_details'][0]['patterns']==[{'id':'P1'}]


def test_formal_defaults_are_team_full_and_bounded(tmp_path,monkeypatch):
    client,root,data=setup(tmp_path,monkeypatch)
    captured={}
    def create(dataset_id,**kwargs):
        captured.update(dataset_id=dataset_id,**kwargs)
        return {'id':'fixture'}
    monkeypatch.setattr(service,'create',create)
    response=client.post('/api/evolution/runs',json={'dataset_id':'evomap_fixture'})
    assert response.status_code==202
    assert captured=={'dataset_id':'evomap_fixture','rounds':3,'concurrency':2,'max_tokens':50_000_000,
                      'mode':'team_full','repetitions':2,'refresh_count':50}


def test_policy_requires_eligible_result_and_supports_rollback(tmp_path,monkeypatch):
    client,root,data=setup(tmp_path,monkeypatch)
    run=root/'fixture'
    atomic_json(run/'manifest.json',{'implementation_hash':implementation_hash()})
    atomic_json(run/'final_lock.json',{'versions':{'g2':'library-g2'}})
    atomic_json(run/'result.json',{'chosen':'g2','activation_eligible':False,'experiment_hash':'experiment'})
    rejected=client.post('/api/evolution/runs/fixture/activate',json={'version':'g2'})
    assert rejected.status_code==409
    result=json.loads((run/'result.json').read_text())
    result['activation_eligible']=True
    atomic_json(run/'result.json',result)
    activated=client.post('/api/evolution/runs/fixture/activate',json={'version':'g2'})
    assert activated.status_code==200
    assert activated.json()['active']['version']=='g2'
    assert client.get('/api/evolution/policy/active').json()['active']['run_id']=='fixture'
    rolled_back=client.post('/api/evolution/policy/rollback')
    assert rolled_back.status_code==200 and rolled_back.json()['active'] is None
