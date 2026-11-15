"""Opt-in durable request budget, shared by all nested model calls in a task."""
import json
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .corpus import atomic_json

_current = ContextVar('pronoia_experiment_meter', default=None)
_task = ContextVar('pronoia_experiment_meter_task', default='')


class UsageMeter:
    def __init__(self, root: Path, limit: int):
        self.root, self.limit = root, limit

    def total(self):
        return sum(json.loads(p.read_text())['charged_tokens'] for p in self.root.glob('*.json'))

    def task_total(self, task_id):
        return sum(row.get('charged_tokens', 0) for p in self.root.glob('*.json')
                   if (row := json.loads(p.read_text())).get('task_id') == task_id)

    def begin(self, kwargs):
        # UTF-8 bytes bound ordinary tokenized text conservatively. Hidden SDK
        # retries (at most two) are reserved too. The maximum is held until a
        # response finishes; failures and abandoned streams keep that hold.
        text = {k:v for k,v in kwargs.items() if k in {'messages','input','instructions','tools'}}
        inputs = len(json.dumps(text,ensure_ascii=False).encode()) + 1024
        outputs = int(kwargs.get('max_tokens') or kwargs.get('max_output_tokens') or 65536)
        charge = (inputs + outputs) * 3
        if self.total() + charge > self.limit:
            raise RuntimeError('experiment request budget reached before sending a model request')
        path = self.root/(uuid.uuid4().hex+'.json')
        atomic_json(path, {'charged_tokens':charge,'reserved_tokens':charge,'input_bytes':inputs,
                           'source':'conservative_reservation','completed':False,'task_id':_task.get()})
        return path

    def finish(self, path, response):
        from ..llm import _response_usage
        usage = _response_usage(response)
        if usage.get('total_tokens'):
            previous=json.loads(path.read_text())
            atomic_json(path,{**previous,'charged_tokens':usage['total_tokens'],'usage':usage,'source':'provider_usage','completed':True})

    def complete(self, path, output_bytes):
        """Settle a successful usage-free response without forgiving failures.

        UTF-8 byte length is an upper bound for ordinary token counts. The
        factor of three retains room for the SDK's two hidden retries. This is
        only used after normal response/stream completion; interrupted work
        deliberately keeps the larger maximum-output reservation.
        """
        previous=json.loads(path.read_text())
        if previous.get('completed'):
            return
        charged=max(1,(int(previous.get('input_bytes') or 1024)+max(0,int(output_bytes)))*3)
        atomic_json(path,{**previous,'charged_tokens':min(int(previous['reserved_tokens']),charged),
                          'observed_output_bytes':max(0,int(output_bytes)),
                          'source':'conservative_completed_bytes','completed':True})


@contextmanager
def meter_context(meter, *, task_id=''):
    token=_current.set(meter)
    task_token=_task.set(task_id)
    try:
        yield
    finally:
        _task.reset(task_token)
        _current.reset(token)


class Stream:
    def __init__(self, stream, meter, receipt):
        self.stream,self.meter,self.receipt=stream,meter,receipt
        self.iterator=stream.__aiter__()
        self.output_bytes=0
    def __aiter__(self):
        return self
    async def __anext__(self):
        try:
            chunk=await self.iterator.__anext__()
        except StopAsyncIteration:
            self.meter.complete(self.receipt,self.output_bytes)
            raise
        self.output_bytes+=response_bytes(chunk)
        self.meter.finish(self.receipt,chunk)
        return chunk
    async def close(self):
        await self.stream.close()


class Resource:
    def __init__(self, underlying, meter):
        self.underlying,self.meter=underlying,meter
    def __getattr__(self, name):
        return Resource(getattr(self.underlying,name),self.meter)
    async def create(self, **kwargs):
        if kwargs.get('stream'):
            options=kwargs.get('stream_options')
            kwargs['stream_options']={**(options if isinstance(options,dict) else {}),'include_usage':True}
        receipt=self.meter.begin(kwargs)
        response=await self.underlying.create(**kwargs)
        if kwargs.get('stream'):
            return Stream(response,self.meter,receipt)
        self.meter.finish(receipt,response)
        self.meter.complete(receipt,response_bytes(response))
        return response


def metered_client(client):
    meter=_current.get()
    return Resource(client,meter) if meter else client


def response_bytes(response):
    """Conservative serialized size for a completed provider payload/chunk."""
    try:
        value=response.model_dump(exclude_none=True)
    except Exception:
        value=vars(response) if hasattr(response,'__dict__') else str(response)
    return len(json.dumps(value,ensure_ascii=False,default=str,separators=(',',':')).encode())
