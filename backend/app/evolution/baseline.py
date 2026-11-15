"""Explicit baseline-only reuse, with frozen provenance and carried request charges."""
from __future__ import annotations

import fcntl
import hashlib
import json
import zipfile
from contextlib import contextmanager
from pathlib import Path

from .corpus import Corpus, atomic_json
from .identity import implementation_sources, implementation_hash
from ..event_backtest.evolution.rubric import digest
from ..event_backtest.engine import _event_prompt
from ..event_backtest.models import EventRecord


def archive_protocol(root):
    archive=root/'protocol-source.zip'
    if archive.exists():
        return
    sources=implementation_sources()
    temporary=root/'protocol-source.zip.tmp'
    with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED) as stream:
        for name,value in sources.items():
            stream.writestr(name,value)
    temporary.chmod(0o600)
    temporary.replace(archive)
    atomic_json(root/'protocol-source.json',{'implementation_hash':implementation_hash(),
        'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
        'files':{name:hashlib.sha256(value.encode()).hexdigest() for name,value in sources.items()}})


@contextmanager
def stopped_source(source):
    with (source/'.experiment.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('baseline source is still running; stop it before importing') from None
        yield


def source_manifest(source):
    with stopped_source(source):
        if (source/'final_lock.json').exists():
            raise ValueError('baseline reuse from a test-exposed experiment is forbidden')
        manifest=json.loads((source/'manifest.json').read_text())
        if manifest['mode']!='team_prompt':
            raise ValueError('only structured baseline predictions can be reused')
        archive=source/'protocol-source.zip'
        index=json.loads((source/'protocol-source.json').read_text())
        if hashlib.sha256(archive.read_bytes()).hexdigest()!=index['sha256']:
            raise ValueError('baseline source archive changed')
        with zipfile.ZipFile(archive) as stream:
            sources={name:stream.read(name).decode() for name in index['files']}
        if digest(sources)!=manifest['implementation_hash']:
            raise ValueError('baseline source implementation identity changed')
        current=implementation_sources()
        # Controller/journaling may change; actual predictor, transport, packet,
        # rubric and accounting implementation must remain exactly unchanged.
        required=['backend/app/event_backtest/engine.py','backend/app/agents/team.py','backend/app/llm.py',
                  'backend/app/evolution/runtime.py','backend/app/evolution/meter.py',
                  'backend/app/event_backtest/evolution/rubric.py','backend/app/event_backtest/evolution/structured.py']
        if any(sources.get(name)!=current[name] for name in required):
            raise ValueError('baseline prediction behavior changed; reuse refused')
        return manifest


def validate_source(experiment):
    source=experiment.baseline_from
    if source==experiment.root:
        raise ValueError('baseline source must be another experiment')
    manifest=source_manifest(source)
    for key in ('dataset_hash','model','model_revision','endpoint_hash','base_prompt_hash','mode'):
        if manifest.get(key)!=experiment.manifest.get(key):
            raise ValueError('baseline source data/model/request identity differs: '+key)
    if json.loads((source/'model.private.json').read_text())!=experiment.profile:
        raise ValueError('baseline source frozen connection differs')
    return manifest


def seed_baseline(experiment, library):
    source=experiment.baseline_from
    manifest=validate_source(experiment)
    source_hash=digest(manifest)
    imported={}
    carried=0
    with stopped_source(source):
        # Keep failed/in-flight reservations too. Restart never resets the
        # previously incurred request budget or treats reused work as free.
        for receipt in (source/'request_usage').glob('*.json'):
            value=json.loads(receipt.read_text())
            target=experiment.meter.root/('imported_'+receipt.name)
            value={**value,'reused_source_experiment':source_hash,'reused_source_receipt':receipt.name}
            carried+=value['charged_tokens']
            if target.exists() and json.loads(target.read_text())!=value:
                raise ValueError('imported request receipt changed')
            atomic_json(target,value)
        for split in ('learn','validation'):
            events={e['event_id']:e for e in experiment.events(split)}
            # Keep this in lockstep with Experiment.predict's full cache
            # identity. Baseline reuse only imports the complete, unfiltered
            # learn/validation pass, so both new dimensions are frozen to
            # their explicit defaults.
            identity={'version':'baseline','library_hash':library['hash'],'dataset_hash':experiment.manifest['dataset_hash'],
                      'experiment_hash':experiment.experiment_hash,'repetition':None,'evaluation_event_ids':None}
            count=0
            for path in (source/'runs/baseline'/split/'tasks').glob('*.json'):
                saved=json.loads(path.read_text());event_id=saved['prediction']['event_id']
                event=events.get(event_id)
                old_identity={**identity,'experiment_hash':source_hash}
                if not event or saved['identity']!=old_identity or saved['packet_hash']!=digest(event) or saved['route']['genes']:
                    raise ValueError('baseline source task identity differs')
                original=Corpus(source/'corpus').load(saved['task_id'],split)
                if original['prediction']!=saved['prediction'] or digest(original['event'])!=digest(event):
                    raise ValueError('baseline source task differs from immutable corpus')
                task_id=f'{experiment.root.name}:baseline:{split}:{event_id}'
                provenance={'experiment_hash':source_hash,'task_hash':original['content_hash']}
                experiment.corpus.record(task_id=task_id,split=split,event=event,prediction=saved['prediction'],genes=[],
                    trace={'reused_baseline':provenance,'packet_hash':digest(json.loads(_event_prompt(EventRecord.from_dict(event))))},
                    model_revision=experiment.model_revision,generation=0)
                target=experiment.root/'runs/baseline'/split/'tasks'/path.name
                value={**saved,'identity':identity,'task_id':task_id,'reused_baseline':provenance}
                if target.exists() and json.loads(target.read_text())!=value:
                    raise ValueError('imported baseline task changed')
                atomic_json(target,value);count+=1
            imported[split]=count
    atomic_json(experiment.root/'baseline-reuse.json',{'source':str(source),'source_experiment_hash':source_hash,
                'imported':imported,'carried_request_budget':carried,
                'test_predictions_imported':0,'optimizer_requests_imported':0})
    experiment.emit(stage='baseline_reused',imported=imported,tokens=experiment.usage())
