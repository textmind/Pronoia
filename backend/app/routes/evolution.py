"""No arbitrary file access, secrets or heldout Oracle labels in this API."""
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Literal

from ..evolution import service
from ..evolution.gep import GEPError
from ..evolution import online

router=APIRouter(prefix='/api/evolution',tags=['evolution'])


@router.get('/online/status')
def online_status():
    return online.status()


@router.post('/online/resume')
def online_resume():
    online.start()
    return online.status()


@router.post('/online/pause')
def online_pause():
    # Pausing is process-local and leaves the durable queue untouched.
    online.stop()
    return online.status()


@router.post('/online/rollback/{candidate_id}')
def online_rollback(candidate_id: str):
    try:
        return online.rollback(candidate_id)
    except FileNotFoundError:
        raise HTTPException(404, detail='候选 Gene 不存在') from None


@router.get('/datasets')
def datasets():
    return {'items':service.datasets()}


@router.get('/runs')
def runs():
    return {'items':service.list_runs()}


class CreateExperiment(BaseModel):
    dataset_id:str=Field(min_length=1,max_length=100,pattern=r'^evomap_[a-zA-Z0-9_-]+$')
    rounds:int=Field(default=3,ge=1,le=10,strict=True)
    concurrency:int=Field(default=2,ge=1,le=4,strict=True)
    max_tokens:int=Field(default=50000000,ge=100000,le=50000000,strict=True)
    mode:Literal['team_prompt','team_full']='team_full'
    repetitions:int=Field(default=2,ge=1,le=5,strict=True)
    refresh_count:int=Field(default=50,ge=1,le=500,strict=True)


@router.post('/runs',status_code=202)
def create(payload:CreateExperiment):
    try:
        return service.create(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(409,detail=str(exc)) from None
    except GEPError as exc:
        raise HTTPException(503,detail=str(exc)) from None


@router.get('/runs/{identity}')
def detail(identity:str):
    try:
        return service.detail(identity)
    except (ValueError,FileNotFoundError):
        raise HTTPException(404,detail='实验不存在') from None
    except GEPError as exc:
        raise HTTPException(503,detail=str(exc)) from None


@router.post('/runs/{identity}/resume',status_code=202)
def resume(identity:str):
    try:
        return service.launch(service.run_path(identity))
    except FileNotFoundError:
        raise HTTPException(404,detail='实验不存在') from None
    except ValueError as exc:
        raise HTTPException(409,detail=str(exc)) from None


@router.post('/runs/{identity}/cancel',status_code=202)
def cancel(identity:str):
    try:
        return service.cancel(identity)
    except FileNotFoundError:
        raise HTTPException(404,detail='实验不存在') from None
    except (ValueError,ProcessLookupError) as exc:
        raise HTTPException(409,detail=str(exc)) from None


class ActivatePolicy(BaseModel):
    version:str|None=Field(default=None,max_length=40,pattern=r'^(?:baseline|g[0-9]+)$')


@router.get('/policy/active')
def active_policy():
    return service.active_policy()


@router.post('/runs/{identity}/activate')
def activate(identity:str,payload:ActivatePolicy=ActivatePolicy()):
    try:
        return service.activate(identity,payload.version)
    except FileNotFoundError:
        raise HTTPException(404,detail='实验不存在') from None
    except ValueError as exc:
        raise HTTPException(409,detail=str(exc)) from None


@router.post('/policy/rollback')
def rollback_policy():
    try:
        return service.rollback_policy()
    except ValueError as exc:
        raise HTTPException(409,detail=str(exc)) from None


@router.get('/runs/{identity}/assets')
def assets(identity:str,limit:int=Query(default=200,ge=1,le=1000)):
    try:
        return service.assets(identity,limit=limit)
    except (ValueError,FileNotFoundError):
        raise HTTPException(404,detail='实验不存在') from None
    except GEPError as exc:
        raise HTTPException(503,detail=str(exc)) from None


@router.get('/runs/{identity}/artifacts/{name}')
def artifact(identity:str,name:str):
    if name not in service.ARTIFACTS:
        raise HTTPException(404,detail='产物不存在')
    try:
        path=service.run_path(identity)/name
        if not path.is_file():
            raise FileNotFoundError()
        return FileResponse(path,media_type=service.ARTIFACTS[name],filename=name)
    except (ValueError,FileNotFoundError):
        raise HTTPException(404,detail='产物尚未生成') from None
