import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  BarChart3,
  ChevronDown,
  FlaskConical,
  Loader2,
  Play,
  Plus,
  RefreshCw,
  Square,
} from "lucide-react";
import { api } from "../api";
import BacktestNav from "../components/backtest/BacktestNav";

import type {
  ModelLabBatch,
  ModelLabProfile,
  ModelLabResults,
} from "../types";
import { cls, relTime } from "../utils";
import ModelLabBatchComposer from "./model-lab/ModelLabBatchComposer";
import ModelLabResultsPanel, { LabStatus, batchNeedsRefresh, batchIsDemo, Empty } from "./model-lab/ModelLabResultsPanel";

type LabTab = "compose" | "results";


export default function ModelLabPage() {
  const [tab, setTab] = useState<LabTab>(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.has("batch")) return "results";
    const requested = params.get("tab");
    return requested === "results" ? requested : "compose";
  });
  const [profiles, setProfiles] = useState<ModelLabProfile[]>([]);
  const [batches, setBatches] = useState<ModelLabBatch[]>([]);
  const [selectedBatchId, setSelectedBatchId] = useState<string | null>(() => new URLSearchParams(window.location.search).get("batch"));
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshRevision, setRefreshRevision] = useState(0);
  const pageMountedRef = useRef(true);
  const loadInFlightRef = useRef(false);

  const load = useCallback(async (quiet = false) => {
    if (loadInFlightRef.current) return;
    loadInFlightRef.current = true;
    if (!quiet) setLoading(true);
    setError(null);
    try {
      const [profileResponse, batchResponse] = await Promise.all([api.modelLabProfiles(), api.modelLabBatches(100)]);
      if (!pageMountedRef.current) return;
      setProfiles(profileResponse.items);
      setBatches(batchResponse.items.filter((batch) => batch.scoring?.source !== "event_experiment"));
    } catch (reason) {
      if (pageMountedRef.current) setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      loadInFlightRef.current = false;
      if (!quiet && pageMountedRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    pageMountedRef.current = true;
    void load();
    return () => { pageMountedRef.current = false; };
  }, [load]);
  useEffect(() => {
    if (!batches.some((batch) => batchNeedsRefresh(batch.status))) return;
    const timer = window.setInterval(() => void load(true), 3000);
    return () => window.clearInterval(timer);
  }, [batches, load]);

  const refreshAll = useCallback(() => {
    setRefreshRevision((revision) => revision + 1);
    void load();
  }, [load]);

  const openCreatedBatch = (batch: ModelLabBatch) => {
    setSelectedBatchId(batch.id);
    setTab("results");
    void load(true);
  };

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col overflow-hidden bg-paper">
      <BacktestNav actions={<button type="button" onClick={refreshAll} disabled={loading} className="inline-flex items-center gap-1.5 rounded-lg border border-edge bg-card px-3 py-2 text-[10.5px] font-medium text-mute transition hover:text-ink disabled:opacity-50"><RefreshCw size={11} className={cls(loading && "animate-spin")} />刷新</button>} />
      <main className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[1500px] px-5 py-6 sm:px-7 lg:px-9">
          <section className="flex flex-col justify-between gap-4 lg:flex-row lg:items-end">
            <div>
              <p className="text-[9px] font-semibold uppercase tracking-[0.2em] text-violet">Pronoia model evaluation</p>
              <h1 className="mt-2 font-serif text-[27px] font-semibold text-ink">Pronoia 基模评测</h1>
              <a href="/settings/models" className="mt-2 inline-block text-sm text-violet underline">前往设置管理共享模型连接</a>
              <p className="mt-1.5 max-w-3xl text-[11px] leading-relaxed text-mute">让不同基模运行同一套 Pronoia，使用相同事件集和预测周期比较看涨 / 看跌判断、收益率预测与问答质量。基模选择只作用于本批次，其他页面沿用平台基模。</p>
            </div>
            <div className="grid grid-cols-3 gap-px overflow-hidden rounded-xl border border-edge bg-edge shadow-card">
              <SummaryCell label="模型 API" value={String(profiles.filter((profile) => profile.is_active).length)} />
              <SummaryCell label="评测批次" value={String(batches.length)} />
              <SummaryCell label="运行中" value={String(batches.filter((batch) => batchNeedsRefresh(batch.status)).length)} />
            </div>
          </section>

          <div className="mt-6 flex flex-wrap gap-1 rounded-xl border border-edge bg-card p-1.5 shadow-card">
            <TabButton active={tab === "compose"} onClick={() => setTab("compose")} icon={<Plus size={11} />} label="新建评测" />
            <TabButton active={tab === "results"} onClick={() => setTab("results")} icon={<BarChart3 size={11} />} label="批次与结果" badge={batches.length} />
          </div>

          {error && <div className="mt-4 flex items-start gap-2 rounded-lg border border-rise/20 bg-rise/5 px-3.5 py-3 text-[10px] text-rise"><AlertTriangle size={12} className="mt-0.5 shrink-0" />模型评测服务连接失败：{error}</div>}

          <div className="mt-4">
            {tab === "compose" && <ModelLabBatchComposer onCreated={openCreatedBatch} onManageProfiles={() => { window.location.href = "/settings/models"; }} />}
            {tab === "results" && <BatchRegistry batches={batches} profiles={profiles} selectedBatchId={selectedBatchId} refreshRevision={refreshRevision} onSelect={setSelectedBatchId} onChanged={refreshAll} />}
          </div>
        </div>
      </main>
    </div>
  );
}

function SummaryCell({ label, value }: { label: string; value: string }) {
  return <div className="min-w-[88px] bg-card px-3.5 py-2.5 text-center"><p className="text-[8px] text-faint">{label}</p><p className="mt-1 font-mono text-[15px] font-semibold text-ink">{value}</p></div>;
}

function TabButton({ active, onClick, icon, label, badge }: { active: boolean; onClick: () => void; icon: ReactNode; label: string; badge?: number }) {
  return <button type="button" onClick={onClick} className={cls("inline-flex items-center gap-1.5 rounded-lg px-3.5 py-2 text-[10.5px] font-semibold transition", active ? "bg-violet-soft text-violet" : "text-mute hover:bg-paper hover:text-ink")}>{icon}{label}{badge !== undefined && <span className={cls("rounded-full px-1.5 py-0.5 font-mono text-[8px]", active ? "bg-card/80" : "bg-edge/55")}>{badge}</span>}</button>;
}

function BatchRegistry({ batches, profiles, selectedBatchId, refreshRevision, onSelect, onChanged }: { batches: ModelLabBatch[]; profiles: ModelLabProfile[]; selectedBatchId: string | null; refreshRevision: number; onSelect: (id: string | null) => void; onChanged: () => void }) {
  const [results, setResults] = useState<ModelLabResults | null>(null);
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const [actionId, setActionId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);
  const resultInFlightRef = useRef(false);
  const desiredBatchIdRef = useRef<string | null>(selectedBatchId);
  const queuedRefreshRef = useRef<{ batchId: string; showLoading: boolean } | null>(null);
  const fetchRunnerRef = useRef<(batchId: string, showLoading: boolean) => Promise<void>>(async () => undefined);

  const fetchResults = useCallback(async (batchId: string, showLoading: boolean) => {
    desiredBatchIdRef.current = batchId;
    if (resultInFlightRef.current) {
      queuedRefreshRef.current = { batchId, showLoading };
      return;
    }
    resultInFlightRef.current = true;
    if (showLoading && mountedRef.current) setLoadingId(batchId);
    if (mountedRef.current) setError(null);
    try {
      const next = await api.modelLabResults(batchId);
      if (!mountedRef.current || desiredBatchIdRef.current !== batchId) return;
      setResults(next);
    } catch (reason) {
      if (!mountedRef.current || desiredBatchIdRef.current !== batchId) return;
      if (showLoading) setResults(null);
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      resultInFlightRef.current = false;
      if (mountedRef.current && desiredBatchIdRef.current === batchId) setLoadingId(null);
      const queued = queuedRefreshRef.current;
      queuedRefreshRef.current = null;
      if (mountedRef.current && queued) void fetchRunnerRef.current(queued.batchId, queued.showLoading);
    }
  }, []);
  fetchRunnerRef.current = fetchResults;

  const open = (batchId: string) => {
    if (batchId === selectedBatchId) void fetchResults(batchId, true);
    else onSelect(batchId);
  };

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      desiredBatchIdRef.current = null;
      queuedRefreshRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (selectedBatchId) void fetchResults(selectedBatchId, true);
  }, [fetchResults, refreshRevision, selectedBatchId]);

  const selectedBatch = batches.find((batch) => batch.id === selectedBatchId);
  useEffect(() => {
    if (!selectedBatchId || !selectedBatch || !batchNeedsRefresh(selectedBatch.status)) return;
    const timer = window.setInterval(() => void fetchResults(selectedBatchId, false), 2500);
    return () => window.clearInterval(timer);
  }, [fetchResults, selectedBatch?.status, selectedBatchId]);

  const action = async (batch: ModelLabBatch, kind: "start" | "cancel") => {
    setActionId(batch.id);
    setError(null);
    try {
      if (kind === "start") await api.modelLabStartBatch(batch.id); else await api.modelLabCancelBatch(batch.id);
      onChanged();
      await fetchResults(batch.id, false);
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setActionId(null); }
  };

  return (
    <div className="grid items-start gap-4 min-[1680px]:grid-cols-[320px_minmax(0,1fr)]">
      <section className="overflow-hidden rounded-xl border border-edge bg-card shadow-card">
        <div className="border-b border-edge px-5 py-4"><h2 className="font-serif text-[15px] font-semibold text-ink">评测批次</h2><p className="mt-1 text-[9px] text-mute">选择批次查看预测与问答各自的进度和结果。</p></div>
        {batches.length === 0 ? <Empty title="还没有评测批次" note="从“新建评测”创建第一组真实测试。" icon={<FlaskConical size={18} />} /> : (
          <div className="max-h-[690px] divide-y divide-edge overflow-y-auto">
            {batches.map((batch) => {
              const selected = selectedBatchId === batch.id;
              const counts = batch.task_counts ?? {};
              const demo = batchIsDemo(batch);
              return (
                <article key={batch.id} className={cls("p-4 transition", selected ? "bg-violet-soft/25" : "hover:bg-paper/70")}>
                  <button type="button" onClick={() => open(batch.id)} className="block w-full text-left">
                    <div className="flex items-start justify-between gap-2"><div className="min-w-0"><div className="flex flex-wrap items-center gap-1.5"><h3 className="truncate text-[10.5px] font-semibold text-ink">{batch.name}</h3>{demo && <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[8px] text-amber-700">DEMO</span>}</div><p className="mt-1 font-mono text-[8px] text-faint">{batch.id.slice(0, 10)} · {relTime(batch.updated_at ?? batch.created_at ?? "")}</p></div><LabStatus status={batch.status} hasWarnings={Boolean(batch.warning_count || batch.completion_quality === "completed_with_warnings")} /></div>
                    <div className="mt-3 flex flex-wrap gap-1.5 text-[8px] text-mute"><span className="rounded bg-paper px-2 py-1">API {batch.profile_ids.length}</span><span className="rounded bg-paper px-2 py-1">完成 {counts.done ?? 0}</span><span className="rounded bg-paper px-2 py-1">运行 {counts.running ?? 0}</span><span className="rounded bg-paper px-2 py-1">失败 {counts.failed ?? 0}</span></div>
                  </button>
                  <div className="mt-3 flex items-center gap-1.5 border-t border-edge/60 pt-3">
                    {["pending", "failed", "cancelled"].includes(batch.status) && <button type="button" onClick={() => void action(batch, "start")} disabled={actionId !== null} className="inline-flex items-center gap-1 rounded-md border border-jade/20 bg-jade-soft px-2 py-1 text-[8.5px] font-medium text-jade">{actionId === batch.id ? <Loader2 size={9} className="animate-spin" /> : <Play size={9} />}启动</button>}
                    {batch.status === "running" && <button type="button" onClick={() => void action(batch, "cancel")} disabled={actionId !== null} className="inline-flex items-center gap-1 rounded-md border border-rise/15 bg-rise/5 px-2 py-1 text-[8.5px] font-medium text-rise"><Square size={9} />取消</button>}
                    <button type="button" onClick={() => open(batch.id)} className="ml-auto inline-flex items-center gap-1 text-[8.5px] font-medium text-violet">查看结果 <ChevronDown size={9} /></button>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>

      <section className="min-h-[420px] overflow-hidden rounded-xl border border-edge bg-card shadow-card">
        {error && <div className="m-4 rounded-lg border border-rise/20 bg-rise/5 px-3 py-2.5 text-[9.5px] text-rise">{error}</div>}
        {loadingId ? <div className="grid min-h-[420px] place-items-center text-[10px] text-mute"><span className="inline-flex items-center gap-2"><Loader2 size={13} className="animate-spin" />读取预测和问答结果…</span></div> : results ? <ModelLabResultsPanel results={results} profiles={profiles} onUpdated={() => void fetchResults(results.batch.id, false)} /> : <Empty title="选择一个评测批次" note="结果区会显示独立任务进度、API 对比图、明细表与导出。" icon={<BarChart3 size={18} />} />}
      </section>
    </div>
  );
}
