/**
 * Versioned JSON/stdio adapter to the public EvoMap Evolver v2 API.
 *
 * The adapter is deliberately local-only. It never imports package internals,
 * starts a daemon, logs into the Hub, or publishes an asset.
 */
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import {
  algo,
  authoring,
  assetstore,
  benchmark,
  events,
  personality,
  signals as signalApi,
  wire,
} from '@evomap/evolver-core';

const require = createRequire(import.meta.url);
const EVOLVER_VERSION = require('@evomap/evolver/package.json').version;
// The distribution pins every public v2 package to the same release version.
const CORE_VERSION = EVOLVER_VERSION;
const PROTOCOL_VERSION = 2;

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function digest(value) {
  return `sha256:${crypto.createHash('sha256').update(canonical(value)).digest('hex')}`;
}

function atomicJson(filename, value) {
  fs.mkdirSync(path.dirname(filename), { recursive: true, mode: 0o700 });
  const temporary = `${filename}.tmp.${process.pid}.${crypto.randomBytes(4).toString('hex')}`;
  fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  fs.renameSync(temporary, filename);
}

function jsonLines(filename) {
  if (!fs.existsSync(filename)) return [];
  return fs.readFileSync(filename, 'utf8').split('\n').filter(Boolean).flatMap(line => {
    try { return [JSON.parse(line)]; } catch { return []; }
  });
}

function appendJsonLine(filename, value) {
  fs.mkdirSync(path.dirname(filename), { recursive: true, mode: 0o700 });
  const fd = fs.openSync(filename, fs.constants.O_CREAT | fs.constants.O_APPEND | fs.constants.O_WRONLY, 0o600);
  try {
    fs.writeSync(fd, `${JSON.stringify(value)}\n`);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
}

/**
 * Evolver v2 requires an administrator-provisioned, separate-UID protected
 * audit service on POSIX. Pronoia local mode supplies the public Ingestor with
 * this append-only store. The Python bridge lock gives it one writer per
 * experiment and every append is fsynced before acknowledgement.
 */
class LocalAuditStore {
  constructor(filename) {
    this.path = filename;
    fs.mkdirSync(path.dirname(filename), { recursive: true, mode: 0o700 });
  }

  readAll() { return jsonLines(this.path); }
  readAllStrict() { return this.readAll(); }

  async append(raw) {
    const previous = this.readAll().at(-1);
    const event = {
      seq: (previous?.seq || 0) + 1,
      eventId: crypto.randomUUID(),
      ts: new Date().toISOString(),
      schemaVersion: raw.schemaVersion || '1.0.0',
      replayability: raw.replayability || 'deterministic',
      actor: raw.actor || { kind: 'machine' },
      ...raw,
    };
    appendJsonLine(this.path, event);
    return event;
  }

  *iterate(fromSeq = 0) {
    for (const event of this.readAll()) if (event.seq >= fromSeq) yield event;
  }

  tail(n = 1) { return this.readAll().slice(-n); }
  recover() { return { truncated: false }; }
}

function seal(input) {
  const asset = structuredClone(input);
  if (asset.type === 'Mutation') {
    for (const field of ['id', 'category', 'trigger_signals', 'target', 'expected_effect', 'risk_level']) {
      if (asset[field] == null) throw new Error(`Mutation is missing ${field}`);
    }
    asset.schema_version ??= wire.SCHEMA_VERSION;
    delete asset.asset_id;
    asset.asset_id = wire.computeAssetId(asset);
    return asset;
  }
  if (!['Gene', 'Capsule', 'EvolutionEvent', 'AntiGene'].includes(asset.type)) throw new Error('Unsupported GEP asset type');
  asset.schema_version ??= wire.SCHEMA_VERSION;
  delete asset.asset_id;
  asset.asset_id = wire.computeAssetId(asset);
  const validation = wire.validateWireDeep(asset);
  if (!validation.ok) throw new Error(`GEP schema rejected: ${validation.errors.join('; ')}`);
  if (!wire.verifyAssetId(asset)) throw new Error('GEP content identity mismatch');
  return asset;
}

function localRuntime(root) {
  if (!path.isAbsolute(root)) throw new Error('An absolute experiment runtime directory is required');
  const base = path.join(root, 'v2');
  const assetsDir = path.join(base, 'asset_store');
  fs.mkdirSync(base, { recursive: true, mode: 0o700 });
  const store = new assetstore.LocalJsonlProvider(assetsDir);
  const ingestor = new events.Ingestor({ store: new LocalAuditStore(path.join(base, 'root_events.jsonl')) });
  const personalityStore = new personality.PersonalityStore({ path: path.join(base, 'personality.json') });
  const experience = new assetstore.ExperienceLibrary(assetsDir);
  const review = assetstore.reviewLedgerForStore(store);
  return {
    base,
    store,
    ingestor,
    personalityStore,
    experience,
    review,
    receiptsDir: path.join(base, 'receipts'),
    commitsDir: path.join(base, 'commits'),
    memoryPath: path.join(base, 'memory_graph.jsonl'),
  };
}

function memoryRecords(runtime) {
  return jsonLines(runtime.memoryPath).filter(row => row?.version === 2 && row?.kind === 'outcome');
}

function memoryAdvice(runtime, requestedSignals) {
  const records = memoryRecords(runtime);
  return algo.deriveMemoryGraphAdvice(records, requestedSignals, Date.now(), {
    bytesRead: fs.existsSync(runtime.memoryPath) ? fs.statSync(runtime.memoryPath).size : 0,
    recordsRead: records.length,
    corruptLines: 0,
    oversizedLines: 0,
    scopeRejected: 0,
    provenanceRejected: 0,
    truncated: false,
    recovery: records.length ? 'healthy' : 'empty',
  });
}

function recordMemory(runtime, { signals, geneId, status, score, at, provenance = 'v2_local', sourceFingerprint }) {
  const normalized = algo.normalizeMemorySignals(signals);
  const row = {
    version: 2,
    kind: 'outcome',
    provenance,
    workspaceScope: digest(runtime.base),
    userScope: 'pronoia-local',
    signalFingerprint: algo.memorySignalFingerprint(normalized),
    signals: normalized,
    geneId: algo.safeMemoryGeneId(geneId),
    status,
    score,
    at,
    ...(sourceFingerprint ? { sourceFingerprint } : {}),
  };
  if (!memoryRecords(runtime).some(existing => sourceFingerprint && existing.sourceFingerprint === sourceFingerprint)) {
    appendJsonLine(runtime.memoryPath, row);
  }
  return row;
}

function learningView(gene, capsules) {
  const related = capsules.filter(c => c.gene === gene.id || c.gene === gene.asset_id);
  const success = related.filter(c => c.outcome?.status === 'success').length;
  const failed = related.filter(c => c.outcome?.status === 'failed').length;
  return {
    geneId: gene.id,
    total: related.length,
    success,
    failed,
    inert: 0,
    observed: 0,
    successRate: related.length ? success / related.length : 0,
    avgScore: related.length ? related.reduce((sum, c) => sum + Number(c.outcome?.score || 0), 0) / related.length : 0,
    recentCapsuleIds: related.slice(-20).map(c => c.asset_id),
  };
}

async function select(runtime, requestedSignals, requestedGenes, requestedCapsules, forcedGeneId) {
  const genes = (requestedGenes || await runtime.store.list('Gene', 1000)).map(seal);
  const capsules = (requestedCapsules || await runtime.store.list('Capsule', 1000)).map(seal);
  const advice = memoryAdvice(runtime, requestedSignals);
  const byId = new Map(advice.genes.map(item => [item.geneId, item]));
  const candidates = genes.map(gene => ({
    geneId: gene.id,
    assetId: gene.asset_id,
    signalsMatch: gene.signals_match,
    category: gene.category,
    summary: gene.summary,
    view: learningView(gene, capsules),
    memoryBoost: byId.get(gene.id)?.boost || 0,
  }));
  const selector = algo.makeGeneSelectionPoint();
  const decision = await selector.run({
    signals: [...new Set(requestedSignals)],
    candidates,
    semanticCorpus: candidates,
    selectionGuard: 'enforce',
    ...(forcedGeneId ? { forcedGeneId } : {}),
  }, { now: Date.now(), cycleId: `selection_${crypto.randomUUID()}`, rng: () => 0.5 });
  const selectedGene = genes.find(gene => gene.id === decision.selectedGeneId || gene.asset_id === decision.selectedAssetId) || null;
  const selectedCapsule = selectedGene ? capsules
    .filter(c => c.gene === selectedGene.id || c.gene === selectedGene.asset_id)
    .sort((a, b) => Number(b.outcome?.score || 0) - Number(a.outcome?.score || 0))[0] || null : null;
  return { selectedGene, selectedCapsule, selector: decision, memoryAdvice: advice };
}

async function install(runtime, rawGenes) {
  const genes = rawGenes.map(seal);
  for (const gene of genes) {
    if (gene.type !== 'Gene') throw new Error('Only Gene assets may be installed');
    const previous = await runtime.store.findByLogicalId(gene.id, 2, 'Gene');
    if (previous.length && !previous.some(item => item.asset_id === gene.asset_id)) {
      const installed = previous.map(item => item.asset_id).sort().join(',');
      const reference = previous[0];
      const changed = [...new Set([...Object.keys(reference), ...Object.keys(gene)])]
        .filter(key => canonical(reference[key]) !== canonical(gene[key])).sort().join(',');
      throw new Error(`Gene IDs are immutable; create a child version (${gene.id}: incoming ${gene.asset_id}; installed ${installed}; changed ${changed || 'unknown'})`);
    }
    await runtime.store.put(gene);
  }
  return { genes: await runtime.store.list('Gene', 1000) };
}

async function feedback(runtime, request) {
  const gene = seal(request.gene);
  await install(runtime, [gene]);
  const event = seal(request.event);
  if (!event.genes_used.includes(gene.id)) throw new Error('Feedback is not linked to the executed Gene');
  const capsule = request.capsule ? seal(request.capsule) : null;
  const previous = await runtime.store.get(event.asset_id);
  if (!previous) await runtime.store.putBundle(capsule ? [capsule, event] : [event]);
  if (request.memory_eligible !== false) recordMemory(runtime, { signals: event.signals, geneId: gene.id,
    status: event.outcome.status, score: event.outcome.score, at: request.at || new Date().toISOString(), sourceFingerprint: event.asset_id });
  return { event, capsule, duplicate: Boolean(previous) };
}

async function prepareCycle(runtime, request) {
  const cycleId = String(request.cycle_id || `cycle_${crypto.randomUUID()}`);
  const requestedSignals = [...new Set(request.signals || [])];
  if (!requestedSignals.length) throw new Error('prepare_cycle requires signals');
  const choice = await select(runtime, requestedSignals, request.genes, request.capsules, request.forced_gene_id);
  if (!choice.selectedGene) throw new Error('No eligible Gene selected');
  const personalityState = runtime.personalityStore.currentState();
  const binding = {
    schema: 'pronoia.evolver-cycle-receipt.v2',
    protocol_version: PROTOCOL_VERSION,
    cycle_id: cycleId,
    experiment_hash: request.experiment_hash,
    dataset_hash: request.dataset_hash,
    model_hash: request.model_hash,
    task_hash: request.task_hash,
    library_hash: request.library_hash,
    signals: requestedSignals,
    gene_id: choice.selectedGene.id,
    gene_asset_id: choice.selectedGene.asset_id,
    capsule_asset_id: choice.selectedCapsule?.asset_id || null,
    personality: personalityState,
    personality_hash: digest(personalityState),
    selector: choice.selector,
    created_at: new Date().toISOString(),
  };
  const receipt = { ...binding, receipt_id: digest(binding) };
  atomicJson(path.join(runtime.receiptsDir, `${receipt.receipt_id.slice(7)}.json`), receipt);
  return receipt;
}

function verifiedReceipt(runtime, receipt, { checkPersonality = true } = {}) {
  if (!receipt || receipt.schema !== 'pronoia.evolver-cycle-receipt.v2') throw new Error('Invalid cycle receipt');
  const { receipt_id: claimed, ...binding } = receipt;
  if (digest(binding) !== claimed) throw new Error('Cycle receipt identity mismatch');
  const filename = path.join(runtime.receiptsDir, `${claimed.slice(7)}.json`);
  if (!fs.existsSync(filename) || canonical(JSON.parse(fs.readFileSync(filename))) !== canonical(receipt)) {
    throw new Error('Cycle receipt is missing or changed');
  }
  if (checkPersonality && digest(runtime.personalityStore.currentState()) !== receipt.personality_hash) {
    throw new Error('Personality changed after cycle preparation');
  }
  return receipt;
}

async function commitCycle(runtime, request) {
  const receipt = verifiedReceipt(runtime, request.receipt, { checkPersonality: false });
  const completed = path.join(runtime.commitsDir, `${receipt.receipt_id.slice(7)}.json`);
  if (fs.existsSync(completed)) return JSON.parse(fs.readFileSync(completed));
  verifiedReceipt(runtime, receipt);
  const execution = structuredClone(request.execution || {});
  for (const field of ['experiment_hash', 'dataset_hash', 'model_hash', 'task_hash', 'library_hash']) {
    if (execution[field] !== receipt[field]) throw new Error(`Execution ${field} does not match prepared cycle`);
  }
  const gene = await runtime.store.get(receipt.gene_asset_id);
  if (!gene || gene.id !== receipt.gene_id) throw new Error('Prepared Gene is missing or changed');
  const now = new Date().toISOString();
  const status = execution.status === 'success' ? 'success' : 'failed';
  const score = Number(execution.score);
  if (!Number.isFinite(score) || score < 0 || score > 1) throw new Error('Execution score must be between 0 and 1');
  const engine = new algo.CycleEngine({
    ingestor: runtime.ingestor,
    selection: algo.makeGeneSelectionPoint(),
    store: runtime.store,
    now: () => Date.now(),
    personality: runtime.personalityStore,
    rng: () => 0.5,
  });
  const result = await algo.runEvolutionCycle(engine, runtime.store, {
    cycleId: receipt.cycle_id,
    problem: {
      id: `problem_${receipt.receipt_id.slice(7, 31)}`,
      signature: request.problem_signature || 'pronoia_validation_feedback',
      signatureV: 1,
      firstSeenAt: now,
      lastSeenAt: now,
      occurrences: Math.max(1, Number(execution.n || 1)),
      linkedSignals: receipt.signals,
      resolvedBy: null,
      status: 'open',
      value: { severity: 0.7, reach: 0.6, strategicFit: 0.9, novelty: 0.4, costEst: 0.3 },
      consecutiveFailures: status === 'failed' ? 1 : 0,
      cooldownUntil: null,
      extensions: { receipt_id: receipt.receipt_id },
    },
    signals: receipt.signals,
    category: request.category || 'optimize',
    forcedGeneId: receipt.gene_asset_id,
    target: request.target || 'Pronoia financial event prediction policy',
    expectedEffect: request.expected_effect || 'improve fixed-denominator validation accuracy',
    summary: request.summary || `Pronoia measured Team evaluation: ${score.toFixed(4)}`,
    confidence: Math.max(0, Math.min(1, Number(request.confidence ?? 0.7))),
    selectionGuard: 'enforce',
    execute: async (_mutation, decision) => ({
      outcome: { status, score, ...(execution.reason ? { reason: String(execution.reason) } : {}) },
      agentInvoked: true,
      selectedGeneInvocation: { geneId: decision.selectedGeneId, assetId: decision.selectedAssetId || receipt.gene_asset_id },
      plannedStrategy: gene.strategy,
      strongEvidence: Boolean(execution.strong_evidence),
      proofOfWork: {
        kind: 'external_receipt',
        external_receipt: { provider: 'pronoia-team-full', receipt_id: receipt.receipt_id, ts: now },
      },
      executionTerminal: { status, disposition: status === 'success' ? 'completed' : 'rejected' },
    }),
    solidifyPermit: context => context.geneId === receipt.gene_id
      ? { ok: true, reason: 'frozen Pronoia cycle receipt matched' }
      : { ok: false, reason: 'prepared Gene mismatch' },
  });
  if (execution.memory_eligible !== false) {
    recordMemory(runtime, {
      signals: receipt.signals,
      geneId: receipt.gene_id,
      status,
      score,
      at: now,
      sourceFingerprint: receipt.receipt_id,
    });
  }
  const evidence = await runtime.experience.recordEvidence({
    assetId: receipt.gene_asset_id,
    kind: 'experiment',
    outcome: status,
    summary: request.summary || `Pronoia Team evaluation score ${score.toFixed(4)}`,
    metrics: {
      durationMs: Math.max(0, Number(execution.duration_ms || 0)),
      tokens: Math.max(0, Number(execution.tokens || 0)),
      checkPassed: Boolean(execution.strong_evidence),
    },
    taskId: String(execution.task_id || receipt.cycle_id),
  });
  const committed = { receipt, cycle: result, evidence };
  atomicJson(completed, committed);
  return committed;
}

function legacySnapshot(root) {
  const read = filename => jsonLines(path.join(root, filename));
  let genes = [];
  const genesJson = path.join(root, 'genes.json');
  if (fs.existsSync(genesJson)) {
    try { genes = JSON.parse(fs.readFileSync(genesJson)).genes || []; } catch { genes = []; }
  }
  return { genes, capsules: read('capsules.jsonl'), events: read('events.jsonl'), memory: read('memory_graph.jsonl') };
}

export async function dispatch(request) {
  if (request.protocol_version != null && request.protocol_version !== PROTOCOL_VERSION) throw new Error('Unsupported bridge protocol version');
  if (request.op === 'inspect') return {
    protocol_version: PROTOCOL_VERSION,
    engine: '@evomap/evolver',
    engine_version: EVOLVER_VERSION,
    core_version: CORE_VERSION,
    sdk_version: wire.SCHEMA_VERSION,
    mode: 'local-complete-cycle',
    public_api_only: true,
    network_publication: false,
  };
  if (request.op === 'distill_draft') {
    return authoring.distillDraft(request.request || request);
  }
  if (request.op === 'seal') return seal(request.asset);
  if (request.op === 'verify') {
    const valid = Boolean(request.asset && wire.verifyAssetId(request.asset) && wire.validateWireDeep(request.asset).ok);
    return { valid, asset: valid ? structuredClone(request.asset) : null };
  }
  const runtime = localRuntime(request.root);
  if (request.op === 'install') return install(runtime, request.genes || []);
  if (request.op === 'capture_draft') {
    const gene = seal(request.gene);
    const capsule = request.capsule ? seal(request.capsule) : null;
    await runtime.store.putBundle(capsule ? [gene, capsule] : [gene]);
    runtime.review.quarantineIfAbsent(gene.asset_id, request.reason || 'auto-distilled probation');
    return { gene, capsule, review: runtime.review.get(gene.asset_id) };
  }
  if (request.op === 'promote_probation') {
    const promoted = await algo.autoPromoteProbationGenes(runtime.store, runtime.review, {
      minSuccess: Math.max(1, Number(request.min_success || 3)),
      by: request.by || 'pronoia-online-controller',
    });
    return { promoted, probation: await algo.scanProbationGenes(runtime.store, runtime.review, { minSuccess: Math.max(1, Number(request.min_success || 3)) }) };
  }
  if (request.op === 'extract') {
    const extracted = signalApi.extractSignals([{ text: request.corpus || '' }, ...(request.recent_events || []).map(text => ({ text: String(text) }))]);
    const sourceSignals = request.corpus ? ['financial_research'] : [];
    if (/(?:错误|失败|下降|error|failed|declin)/i.test(request.corpus || '')) sourceSignals.push('observed_problem');
    return { signals: [...new Set([...extracted.flatMap(item => [item.id, item.kind].filter(Boolean)), ...sourceSignals])], extracted };
  }
  if (request.op === 'select') return select(runtime, [...new Set(request.signals || [])], request.genes, request.capsules, request.forced_gene_id);
  if (request.op === 'recall') {
    const records = await runtime.store.list(undefined, Math.min(1000, Number(request.pool_limit || 1000)));
    return assetstore.recallLocalAssets(records, { text: String(request.text || ''), limit: Math.min(20, Number(request.limit || 5)) });
  }
  if (request.op === 'prepare_cycle') return prepareCycle(runtime, request);
  if (request.op === 'commit_cycle') return commitCycle(runtime, request);
  if (request.op === 'attempt') {
    const choice = await select(runtime, request.signals || [], [request.gene], [], request.gene?.id);
    const mutation = algo.buildMutation({ decision: choice.selector, category: request.gene.category, signals: request.signals || [], target: 'Pronoia prediction policy', expectedEffect: 'improve measured accuracy' });
    return { actionId: `act_${mutation.id}`, hypothesis: { hypothesisId: `hyp_${mutation.id}`, signals: request.signals || [] }, mutation };
  }
  if (request.op === 'feedback') return feedback(runtime, request);
  if (request.op === 'feedback_batch') {
    const results = [];
    // Reuse the provider/index instead of rebuilding it for every item.
    for (const item of request.items || []) results.push(await feedback(runtime, item));
    return { count: results.length, event_ids: results.map(item => item.event.id) };
  }
  if (request.op === 'evidence') {
    if (request.record) return runtime.experience.recordEvidence(request.record);
    return runtime.experience.evidence(request.asset_id);
  }
  if (request.op === 'benchmark') {
    const pairs = (request.pairs || []).map(pair => ({ currentPassed: Boolean(pair.baseline_pass), globalFlatAbstainPassed: Boolean(pair.evolved_pass) }));
    const evolvedOnly = pairs.filter(pair => pair.globalFlatAbstainPassed && !pair.currentPassed).length;
    const baselineOnly = pairs.filter(pair => !pair.globalFlatAbstainPassed && pair.currentPassed).length;
    return { ...benchmark.pairedRiskDifference95(pairs), mcnemarPValue: benchmark.exactMcNemarTwoSided(evolvedOnly, baselineOnly), evolvedOnly, baselineOnly };
  }
  if (request.op === 'import_v1') {
    const assets = request.assets || [];
    const record = { imported_at: new Date().toISOString(), source: request.source || 'v1', state: 'quarantined', assets };
    const identity = digest(record);
    atomicJson(path.join(runtime.base, 'v1_import', `${identity.slice(7)}.json`), record);
    return { import_id: identity, count: assets.length, state: 'quarantined', selectable: false };
  }
  if (request.op === 'snapshot') {
    const [genes, capsules, evolutionEvents, antiGenes, experienceList] = await Promise.all([
      runtime.store.list('Gene', 1000), runtime.store.list('Capsule', 1000), runtime.store.list('EvolutionEvent', 1000),
      runtime.store.list('AntiGene', 1000), runtime.experience.list(undefined, 100, 0),
    ]);
    const legacy = legacySnapshot(request.root);
    return {
      protocol_version: PROTOCOL_VERSION,
      genes,
      capsules,
      events: evolutionEvents,
      anti_genes: antiGenes,
      memory: memoryRecords(runtime),
      root_events: runtime.ingestor.readAll(),
      personality: runtime.personalityStore.load(),
      experience: experienceList,
      legacy_read_only: legacy,
    };
  }
  throw new Error('Unsupported EvoMap bridge operation');
}

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(new URL(import.meta.url).pathname)) {
  // StringDecoder preserves UTF-8 code points when a large JSON request is
  // split across pipe chunks. Coercing each Buffer independently corrupts a
  // multibyte character that straddles a chunk boundary and changes asset IDs.
  process.stdin.setEncoding('utf8');
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  console.log = (...args) => console.error(...args);
  try {
    const result = await dispatch(JSON.parse(input));
    process.stdout.write(JSON.stringify({ ok: true, result }, (_key, value) => value instanceof Set ? [...value] : value));
  } catch (error) {
    process.stdout.write(JSON.stringify({ ok: false, error: String(error?.message || error).slice(0, 2000) }));
    process.exitCode = 1;
  }
}
