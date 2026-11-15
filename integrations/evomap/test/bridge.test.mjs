import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawn } from 'node:child_process';

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pronoia-gep-'));
const bridge = new URL('../bridge.mjs', import.meta.url);
const call = body => JSON.parse(execFileSync(process.execPath, [bridge.pathname], {input: JSON.stringify({root, ...body}),encoding:'utf8',timeout:20000})).result;
const callWithSplitUtf8 = body => new Promise((resolve,reject) => {
  const child=spawn(process.execPath,[bridge.pathname],{stdio:['pipe','pipe','pipe']});
  const input=Buffer.from(JSON.stringify({root,...body}));
  const marker=Buffer.from('公');
  const markerAt=input.indexOf(marker);
  assert.ok(markerAt>=0);
  let stdout='';let stderr='';
  child.stdout.setEncoding('utf8');child.stderr.setEncoding('utf8');
  child.stdout.on('data',chunk=>{stdout+=chunk;});child.stderr.on('data',chunk=>{stderr+=chunk;});
  child.on('error',reject);
  child.on('close',code=>{
    try {
      const envelope=JSON.parse(stdout);
      if(code!==0||!envelope.ok) reject(new Error(envelope.error||stderr||`bridge exited ${code}`));
      else resolve(envelope.result);
    } catch(error) { reject(error); }
  });
  child.stdin.write(input.subarray(0,markerAt+1));
  setTimeout(()=>child.stdin.end(input.subarray(markerAt+1)),25);
});
const gene = () => call({op:'seal',asset:{type:'Gene',id:'gene_finance_quality_v1',category:'optimize',signals_match:['earnings','profit_declining'],
  strategy:['Compare operating profit and one-off gains using as-of evidence.'], constraints:{max_files:1,forbidden_paths:['oracle','test.labels.jsonl']},validation:['pronoia:fixed_denominator_evaluation']}});
test.after(() => fs.rmSync(root,{recursive:true,force:true}));

test('bridge exposes the pinned public v2 runtime without publication', () => {
  const value=call({op:'inspect'});
  assert.equal(value.protocol_version,2);
  assert.equal(value.engine_version,'2.0.51');
  assert.equal(value.public_api_only,true);
  assert.equal(value.network_publication,false);
});

test('assets use the actual official SDK schema and canonical identity', () => {
  const asset = gene();
  assert.match(asset.asset_id,/^sha256:[a-f0-9]{64}$/);
  assert.equal(call({op:'verify',asset}).valid,true);
  assert.equal(call({op:'verify',asset:{...asset,summary:'tampered'}}).valid,false);
  assert.throws(() => call({op:'seal',asset:{type:'Gene',id:'invalid'}}));
});
test('official asset store preserves immutable Gene versions', () => {
  const asset = gene();
  const result = call({op:'install',genes:[asset]});
  assert.equal(result.genes.length,1);
  assert.equal(result.genes[0].id,asset.id);
  assert.throws(() => call({op:'install',genes:[{...asset,strategy:['Changed without a new version ID']}]}));
});
test('official signals, selector, hypotheses and attempt are connected', () => {
  const asset=gene();
  const extracted = call({op:'extract',corpus:'错误：预测错误；收益下降，现有推理需要改进',recent_events:[]});
  assert.ok(extracted.signals.length);
  const result=call({op:'select',signals:['earnings','profit_declining'],genes:[asset],capsules:[]});
  assert.equal(result.selectedGene.id,asset.id);
  assert.ok(result.selector && result.memoryAdvice);
  const attempt=call({op:'attempt',signals:['earnings'],gene:asset});
  assert.ok(attempt.actionId && attempt.hypothesis.hypothesisId);
});
test('measured feedback creates a real Event, Capsule and memory outcome, idempotently', () => {
  const asset=gene();
  const capsule=call({op:'seal',asset:{type:'Capsule',id:'capsule_evaluation_1',trigger:['earnings'],gene:asset.id,summary:'Validated operating-quality strategy',confidence:.6,
    blast_radius:{files:0,lines:0},outcome:{status:'success',score:.6},execution_trace:[{stage:'validate',task_id:'task_1',metric:'accuracy',n:10,correct:6}]}});
  const event=call({op:'seal',asset:{type:'EvolutionEvent',id:'event_evaluation_1',intent:'optimize',signals:['earnings'],genes_used:[asset.id],mutation_id:'mutation_1',blast_radius:{files:0,lines:0},
    outcome:{status:'success',score:.6},source_type:'generated',capsule_id:capsule.id,meta:{task_id:'task_1',correct:6,n:10}}});
  const input={op:'feedback',event,capsule,gene:asset,action_id:'act_1'};
  assert.equal(call(input).duplicate,false);
  assert.equal(call(input).duplicate,true);
  const snapshot=call({op:'snapshot'});
  assert.equal(snapshot.events.length,1);
  assert.equal(snapshot.capsules.length,1);
  assert.equal(snapshot.memory.filter(e=>e.kind==='outcome').length,1);
});
test('failed work stays in history and cannot manufacture a success Capsule', () => {
  const asset=gene();
  const event=call({op:'seal',asset:{type:'EvolutionEvent',id:'event_failure',intent:'repair',signals:['earnings'],genes_used:[asset.id],mutation_id:'mutation_2',blast_radius:{files:0,lines:0},
    outcome:{status:'failed',score:.2},source_type:'generated'}});
  call({op:'feedback',event,gene:asset,action_id:'act_2'});
  const snapshot=call({op:'snapshot'});
  assert.equal(snapshot.events.length,2);
  assert.equal(snapshot.capsules.length,1);
});

test('large feedback batches reuse one runtime and keep the installed Gene immutable', () => {
  const asset=gene();
  const items=Array.from({length:150},(_,index)=>({
    gene:asset,
    memory_eligible:false,
    event:{type:'EvolutionEvent',id:`event_batch_${index}`,intent:'optimize',signals:['earnings'],genes_used:[asset.id],
      mutation_id:`mutation_batch_${index}`,blast_radius:{files:0,lines:0},outcome:{status:'failed',score:0},source_type:'generated'},
  }));
  const result=call({op:'feedback_batch',items});
  assert.equal(result.count,items.length);
  assert.equal(result.event_ids.length,items.length);
  const snapshot=call({op:'snapshot'});
  assert.equal(snapshot.events.filter(event=>event.id.startsWith('event_batch_')).length,items.length);
  assert.equal(snapshot.genes.filter(item=>item.id===asset.id).length,1);
});

test('stdio preserves a multibyte Gene when JSON is split inside its UTF-8 sequence', async () => {
  const asset=call({op:'seal',asset:{type:'Gene',id:'gene_utf8_boundary_v1',category:'optimize',signals_match:['earnings'],
    strategy:['公告包含可核查的利润变化。'],constraints:{max_files:1,forbidden_paths:['oracle']},validation:['pronoia:fixed_denominator_evaluation']}});
  call({op:'install',genes:[asset]});
  const event={type:'EvolutionEvent',id:'event_utf8_boundary',intent:'optimize',signals:['earnings'],genes_used:[asset.id],mutation_id:'mutation_utf8_boundary',
    blast_radius:{files:0,lines:0},outcome:{status:'failed',score:0},source_type:'generated'};
  const result=await callWithSplitUtf8({op:'feedback_batch',items:[{gene:asset,event,memory_eligible:false}]});
  assert.equal(result.count,1);
  assert.deepEqual(result.event_ids,[event.id]);
});

test('frozen prepare/commit runs the official complete v2 cycle and is idempotent', () => {
  const asset=gene();
  call({op:'install',genes:[asset]});
  const identity={experiment_hash:'exp',dataset_hash:'data',model_hash:'model',task_hash:'task',library_hash:'library'};
  const receipt=call({op:'prepare_cycle',cycle_id:'cycle_contract_1',signals:['financial_research','earnings'],forced_gene_id:asset.asset_id,...identity});
  assert.match(receipt.receipt_id,/^sha256:[a-f0-9]{64}$/);
  assert.equal(receipt.gene_asset_id,asset.asset_id);
  const execution={...identity,status:'success',score:.7,n:20,tokens:1200,duration_ms:42,strong_evidence:true,task_id:'validation_round_1'};
  const committed=call({op:'commit_cycle',receipt,execution,summary:'14/20 validation predictions correct'});
  assert.equal(committed.cycle.finalStage,'solidified');
  assert.equal(committed.cycle.resolutionStatus,'resolved_by_evidence');
  assert.equal(committed.cycle.event.genes_used[0],asset.id);
  assert.equal(call({op:'commit_cycle',receipt,execution}).cycle.event.asset_id,committed.cycle.event.asset_id);
  const snapshot=call({op:'snapshot'});
  assert.ok(snapshot.root_events.some(event=>event.type==='cycle.solidified'));
  assert.ok(snapshot.personality.history.length);
  assert.ok(snapshot.experience.items.some(item=>item.assetId===asset.asset_id));
  assert.ok(snapshot.memory.some(item=>item.sourceFingerprint===receipt.receipt_id));
});

test('commit rejects a changed frozen receipt or execution identity', () => {
  const asset=gene();
  const identity={experiment_hash:'exp2',dataset_hash:'data2',model_hash:'model2',task_hash:'task2',library_hash:'library2'};
  const receipt=call({op:'prepare_cycle',cycle_id:'cycle_contract_2',signals:['earnings'],genes:[asset],forced_gene_id:asset.id,...identity});
  const execution={...identity,status:'failed',score:.2};
  assert.throws(()=>call({op:'commit_cycle',receipt:{...receipt,task_hash:'changed'},execution}));
  assert.throws(()=>call({op:'commit_cycle',receipt,execution:{...execution,model_hash:'changed'}}));
});
