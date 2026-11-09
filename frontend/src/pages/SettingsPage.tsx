import { cloneElement, useEffect, useId, useRef, useState, type ReactElement } from "react";
import { useStore } from "../store";
import { Configuration, ServiceConnection, SettingsStatus, settingsChanged, settingsRequest } from "../settingsClient";
import { ModelLabProfile } from "../types";

const input = "mt-1 w-full rounded-lg border border-edgeDark bg-card px-3 py-2 text-sm text-ink";
const button = "rounded-lg border border-edge bg-card px-4 py-2 text-sm text-ink disabled:opacity-40";
const section = "rounded-xl border border-edge bg-card p-5 space-y-4";
type Profile = ModelLabProfile & { configuration: Configuration; thinking_supported: boolean };
const emptyModel = () => ({ id: "", name: "", provider: "custom", base_url: "", model_id: "", api_key: "", max_output_tokens: 65536, timeout_seconds: 600, thinking_mode: "auto", is_active: true });
const emptyService = () => ({ id: "", name: "", kind: "event" as "event" | "quant", endpoint: "", auth_mode: "bearer", header_name: "X-API-Key", api_key: "", secret_env_ref: "", timeout_seconds: 30, version: "", is_active: true, revision: "" });

const sections = [
  { id: "models", title: "大模型连接", optional: false, description: "保存模型服务的地址、密钥和模型 ID。一条连接代表一套可调用的模型配置，可以保存多条供不同任务选择。", when: "普通研究需要至少一条已验证连接。" },
  { id: "defaults", title: "平台默认模型", optional: false, description: "从已保存的大模型连接中，选择研究工作台等功能默认使用的模型。这里不需要重复填写地址和密钥。", when: "使用普通研究前选择一次；以后需要更换时再调整。" },
  { id: "services", title: "外部预测服务", optional: true, description: "接入直接返回事件预测结果或量化目标仓位的专用服务。它的接口格式与聊天模型不同，普通大模型地址不能直接填在这里。", when: "使用自有或第三方预测服务时配置；未设置不影响普通研究和内置量化回测。" },
  { id: "simulation", title: "事件推演", optional: true, description: "填写负责启动、管理推演任务的独立服务地址。当前网关连接不需要模型 API Key；推演程序内部的模型配置由该程序管理。", when: "使用独立事件推演功能时配置；未设置不影响普通研究。" },
] as const;
const nextSteps: Record<string, string> = {
  unset: "填写下方配置并保存，再点击验证连接。",
  incomplete: "请补齐缺少的字段并保存，再点击验证连接。",
  unverified: "已保存，尚未验证。请点击验证连接；修改连接参数后也需要重新验证。",
  verified: "连接已就绪，可以供相关任务选择使用。",
  failed: "请按失败原因检查配置或测试预算，再重新验证。",
  disabled: "如需使用，请编辑并启用连接，再确认验证状态。",
};

function State({ state, action = "验证连接", isDefault = false }: { state?: Configuration; action?: string; isDefault?: boolean }) {
  const hint = isDefault
    ? state?.ready ? "默认模型已就绪，可以开始普通研究。" : "请先到「大模型连接」补全并验证连接，再在这里选择并应用默认模型。"
    : (nextSteps[state?.state || "unset"] || "").split("验证连接").join(`「${action}」`);
  return <div className="space-y-1 text-sm"><p className={state?.ready ? "text-jade" : "text-amber-700"}>{state?.label || "未设置"}{state?.missing.length ? ` · 缺少 ${state.missing.join("、")}` : ""}{state?.message ? ` · ${state.message}` : ""}</p><p className="text-xs leading-relaxed text-mute">{hint}</p></div>;
}
function Field({ label, hint, children }: { label: string; hint?: string; children: ReactElement }) {
  const id = useId();
  return <div className="min-w-0"><label htmlFor={id} className="block text-sm font-medium text-ink">{label}</label>{cloneElement(children, { id, "aria-describedby": hint ? id + "-hint" : undefined })}{hint && <p id={id + "-hint"} className="mt-1.5 text-xs leading-relaxed text-mute">{hint}</p>}</div>;
}
function OptionalBadge() { return <span className="rounded-full border border-edge bg-paper px-2 py-0.5 text-[11px] font-normal text-mute">可选</span>; }

export default function SettingsPage() {
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const tab = view.replace("settings-", "");
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [status, setStatus] = useState<SettingsStatus | null>(null);
  const [services, setServices] = useState<ServiceConnection[]>([]);
  const [model, setModel] = useState(emptyModel);
  const [external, setExternal] = useState(emptyService);
  const [gateway, setGateway] = useState({ endpoint: "", timeout_seconds: 15, is_active: true, revision: "" });
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [probe, setProbe] = useState(2048);
  const modelForm = useRef<HTMLFormElement>(null);
  const serviceForm = useRef<HTMLFormElement>(null);
  useEffect(() => { setError(""); setNotice(""); }, [tab]);
  const load = async () => {
    const [state, models, serviceList, gw] = await Promise.all([
      settingsRequest<SettingsStatus>("/settings/status"),
      settingsRequest<{ items: Profile[] }>("/model-lab/profiles"),
      settingsRequest<{ items: ServiceConnection[] }>("/settings/services"),
      settingsRequest<typeof gateway>("/settings/simulation"),
    ]);
    setStatus(state); setProfiles(models.items); setServices(serviceList.items); setSelected(state.model.profile_id || "");
    setGateway({ endpoint: gw.endpoint || "", timeout_seconds: gw.timeout_seconds || 15, is_active: gw.is_active !== false, revision: gw.revision });
    settingsChanged();
  };
  useEffect(() => { let active = true; setBusy(true); void settingsRequest("/settings/initialize", "POST", {}).then(() => { if (active) return load(); }).catch((e: Error) => { if (active) setError(e.message); }).finally(() => { if (active) setBusy(false); }); return () => { active = false; }; }, []);
  const act = async (work: () => Promise<unknown>, success: string) => {
    setBusy(true); setError(""); setNotice("");
    try { const result = await work() as { ok?: boolean; message?: string }; await load(); if (result?.ok === false) setError(result.message || "验证未完成"); else setNotice(success); }
    catch (e) { setError(e instanceof Error ? e.message : "操作失败，请重试"); }
    finally { setBusy(false); }
  };
  const saveModel = async () => {
    const { id, api_key, ...fields } = model;
    const result = await settingsRequest<Profile>(id ? `/model-lab/profiles/${id}` : "/model-lab/profiles", id ? "PATCH" : "POST", { ...fields, ...(api_key.trim() ? { api_key: api_key.trim() } : {}) });
    setModel({ ...model, id: result.id, api_key: "" });
    return result;
  };
  const edit = (p: Profile) => setModel({ id: p.id, name: p.name, provider: p.provider, base_url: p.base_url, model_id: p.model_id, api_key: "", max_output_tokens: p.max_output_tokens, timeout_seconds: p.timeout_seconds, thinking_mode: p.thinking_mode, is_active: p.is_active });
  const thinkingSupported = (() => { try { return new URL(model.base_url).hostname === "api.deepseek.com"; } catch { return false; } })();
  const currentSection = sections.find((item) => item.id === tab) || sections[0];
  return <div className="min-w-0 flex-1 overflow-y-auto bg-paper p-5 sm:p-8"><div className="mx-auto max-w-5xl space-y-6">
    <header><h1 className="font-serif text-2xl text-ink">设置</h1><p className="mt-2 text-sm text-mute">集中管理共享连接，验证成功后即可用于相关任务。</p></header>
    <section aria-label="设置入门指引" className="space-y-3 rounded-xl border border-jade/20 bg-jade-soft/20 p-4 sm:p-5">
      <h2 className="text-sm font-semibold text-ink">只做普通研究？完成这两步即可</h2>
      <p className="text-sm leading-relaxed text-mute">① <a className="font-medium text-jade underline underline-offset-2" href="/settings/models">添加并验证大模型连接</a><span className="mx-2">→</span>② <a className="font-medium text-jade underline underline-offset-2" href="/settings/defaults">选择平台默认模型</a>。外部预测服务和事件推演按需配置。</p>
      <p className="border-t border-jade/10 pt-3 text-xs leading-relaxed text-mute"><span className="font-medium text-ink">一套大模型 API 通常需要：</span>Base URL（请求发给谁）＋ API Key（访问凭证）＋ 模型 ID（调用哪个模型）。四个分区不代表需要填写四套 API。</p>
    </section>
    <nav aria-label="设置分区" className="flex flex-wrap gap-2">{sections.map(({ id, title, optional }) => <button key={id} className={button + " flex items-center gap-2" + (tab === id ? " border-violet/20 bg-violet-soft font-semibold" : " hover:bg-card/70")} aria-current={tab === id ? "page" : undefined} onClick={() => setView(`settings-${id}`)}>{title}{optional && <OptionalBadge />}</button>)}</nav>
    <header className="space-y-2">
      <h2 className="flex items-center gap-2 font-serif text-xl text-ink">{currentSection.title}{currentSection.optional && <OptionalBadge />}</h2>
      <p className="max-w-3xl text-sm leading-relaxed text-mute">{currentSection.description}</p>
      <p className="text-xs leading-relaxed text-mute">{currentSection.when}</p>
    </header>
    {error && <div role="alert" className="rounded-lg bg-rise/10 p-3 text-sm text-rise">{error}<button className="ml-3 underline" onClick={() => void act(() => settingsRequest("/settings/initialize", "POST", {}), "配置已重新读取")}>重试读取</button></div>}
    {notice && <p role="status" className="text-sm text-jade">{notice}</p>}
    {tab === "models" && <>
      <section className={section}>
        <div className="flex items-center justify-between gap-3"><h3 className="text-base font-medium text-ink">已保存连接</h3><button className={button} disabled={busy} onClick={() => { setModel(emptyModel()); modelForm.current?.scrollIntoView({ behavior: "smooth", block: "start" }); }}>新增连接</button></div>
        {!profiles.length && <p className="text-sm leading-relaxed text-mute">未设置大模型连接。请在下方添加；也可以先保存草稿，补全后再验证。</p>}
        {profiles.map((p) => <article key={p.id} className="space-y-3 border-t border-edge pt-4">
          <p className="break-words text-sm font-medium text-ink">{p.name} · {p.model_id || "未设置模型 ID"}{p.id === status?.model.profile_id ? " · 平台默认" : ""}</p>
          <p className="break-all text-xs leading-relaxed text-mute">{p.base_url || "未设置 Base URL"} · {p.secret_configured ? "密钥已保存" : "未设置密钥"}</p>
          <State state={p.configuration} />
          <div className="grid gap-2 rounded-lg bg-paper p-3 text-xs sm:grid-cols-2">
            {([["text", "文本回答"], ["tools", "工具调用"], ["json", "结构化输出"], ["web_search", "原生联网搜索"]] as const).map(([key, label]) => <p key={key} className="text-mute">{label}：<span className={p.capabilities?.[key]?.state === "supported" ? "text-jade" : "text-mute"}>{p.capabilities?.[key]?.label || "未验证"}</span>{key === "web_search" && p.capabilities?.[key]?.state !== "supported" ? "（使用内置资讯源）" : ""}</p>)}
          </div>
          <details className="rounded-lg border border-edge p-3"><summary className="cursor-pointer text-sm text-ink">研究能力验证</summary><p className="my-3 text-xs leading-relaxed text-mute">基础连接验证只检查文本回答。下面的测试由你手动发起，会产生少量 API 用量；原生搜索未验证或不支持时，研究继续使用内置资讯源。</p><div className="flex flex-wrap gap-2">{([["tools", "验证工具调用"], ["json", "验证结构化输出"], ["web_search", "验证原生搜索"]] as const).map(([key, label]) => <button type="button" key={key} className={button} disabled={busy || !p.configuration.ready} onClick={() => void act(() => settingsRequest("/model-lab/profiles/" + p.id + "/validate-capability?capability=" + key, "POST", {}), label + "成功")}>{label}</button>)}</div>{p.capabilities && Object.entries(p.capabilities).filter(([, c]) => c.message && c.state !== "supported").map(([key, c]) => <p key={key} className="mt-2 text-xs text-mute">{({ text: "文本", tools: "工具调用", json: "结构化输出", web_search: "原生搜索" } as Record<string,string>)[key]}：{c.message}</p>)}</details>
          <div className="flex flex-wrap gap-2"><button className={button} disabled={busy} onClick={() => { edit(p); modelForm.current?.scrollIntoView({ behavior: "smooth", block: "start" }); }}>编辑</button><button className={button} disabled={busy || !p.is_active || !p.secret_configured || !p.base_url || !p.model_id} onClick={() => void act(() => settingsRequest("/model-lab/profiles/" + p.id + "/validate?probe_tokens=" + probe, "POST", {}), "模型验证成功，可以到「平台默认模型」选择使用")}>验证连接</button></div>
        </article>)}
        <details className="rounded-lg border border-edge p-3">
          <summary className="cursor-pointer text-sm font-medium text-ink">连接测试高级选项</summary>
          <div className="mt-4"><Field label="连接测试预算（tokens，32–16384）" hint="仅用于连接测试，可能产生少量 API 用量。预算耗尽且没有收到正文时，可调高后重新验证；不会修改正式研究的输出上限。"><input className={input} type="number" min={32} max={16384} value={probe} onChange={(e) => setProbe(Number(e.target.value))} /></Field></div>
        </details>
      </section>
      <form ref={modelForm} className={section + " scroll-mt-6"} onSubmit={(e) => { e.preventDefault(); void act(saveModel, "连接已保存；首次配置或修改连接参数后，请点击上方「验证连接」"); }}>
        <h3 className="text-base font-medium text-ink">{model.id ? "编辑连接" : "添加连接"}</h3>
        <div className="grid gap-x-5 gap-y-5 sm:grid-cols-2">
          <Field label="显示名称" hint="自己起的名称，方便识别，例如“日常研究模型”；不会发送给模型服务。"><input required className={input} value={model.name} onChange={(e) => setModel({ ...model, name: e.target.value })} placeholder="例如：日常研究模型" /></Field>
          <Field label="API 基础地址（Base URL）" hint="从服务商复制的接口基础地址，不是聊天网页地址。按服务商要求填写，不自动补 /v1 或其他路径。"><input className={input} value={model.base_url} onChange={(e) => setModel({ ...model, base_url: e.target.value })} placeholder="粘贴服务商提供的 Base URL" /></Field>
          <Field label="API Key" hint={model.id ? "服务商提供的访问密钥。保存后不显示原文；编辑时留空保留已有密钥。" : "服务商提供的访问密钥。保存后不显示原文；以后编辑时留空保留已有密钥。"}><input className={input} type="password" autoComplete="new-password" value={model.api_key} onChange={(e) => setModel({ ...model, api_key: e.target.value })} placeholder={model.id ? "留空保留已保存密钥" : "粘贴服务商提供的 API Key"} /></Field>
          <Field label="模型 ID" hint="服务商提供的准确模型名称。同一个地址和密钥可能支持多个模型，名称填写错误可能出现“模型不存在”。"><input className={input} value={model.model_id} onChange={(e) => setModel({ ...model, model_id: e.target.value })} placeholder="填写服务商提供的模型 ID" /></Field>
        </div>
        <details className="rounded-lg border border-edge p-3 sm:p-4">
          <summary className="cursor-pointer text-sm font-medium text-ink">高级选项</summary>
          <div className="mt-4 grid gap-5 sm:grid-cols-2">
            <Field label="服务商标记" hint="用于识别连接来源，通常保留现有值即可。实际请求地址由 Base URL 决定。"><input className={input} value={model.provider} onChange={(e) => setModel({ ...model, provider: e.target.value })} placeholder="custom / DeepSeek" /></Field>
            <Field label="输出上限（tokens）" hint="单次请求的输出预算；部分模型会把思考过程计入预算，不是账户总额度。"><input className={input} type="number" min={32} max={200000} value={model.max_output_tokens} onChange={(e) => setModel({ ...model, max_output_tokens: Number(e.target.value) })} /></Field>
            <Field label="请求超时（秒）" hint="单次请求最多等待多久，超过后会停止等待并提示超时。"><input className={input} type="number" min={1} max={900} value={model.timeout_seconds} onChange={(e) => setModel({ ...model, timeout_seconds: Number(e.target.value) })} /></Field>
            <Field label="思考模式" hint="能否控制取决于服务及本项目适配。当前未适配的 MaaS 使用服务商默认行为。">{thinkingSupported ? <select className={input} value={model.thinking_mode} onChange={(e) => setModel({ ...model, thinking_mode: e.target.value })}><option value="auto">自动（当前适配关闭思考）</option><option value="disabled">关闭</option><option value="enabled">启用</option></select> : <p className="mt-2 text-sm text-mute">服务商默认，暂不支持控制</p>}</Field>
          </div>
        </details>
        <label className="flex gap-2 text-sm"><input type="checkbox" checked={model.is_active} onChange={(e) => setModel({ ...model, is_active: e.target.checked })} />启用连接</label>
        <button className={button} disabled={busy || !model.name.trim()}>保存连接／草稿</button>
      </form>
    </>}
    {tab === "defaults" && <section className={section}>
      <h3 className="text-base font-medium text-ink">选择默认使用的模型</h3>
      <p className="text-sm leading-relaxed text-mute">更换默认模型会影响后续使用默认模型的研究、报告和新实验。已创建的实验继续使用各自冻结的配置。</p>
      <div className="space-y-2 rounded-lg bg-paper p-3"><p className="text-sm font-medium text-ink">当前：{status?.model.name || "未设置"}</p><State state={status?.model} isDefault /></div>
      {!status?.model.ready && <p className="text-sm leading-relaxed text-mute">请先添加并验证大模型连接，再在下方选中它并点击「应用默认模型」。</p>}
      <Field label="选择已验证连接" hint="这里只选择已有连接，不需要再次填写 Base URL 或 API Key。"><select className={input} value={selected} onChange={(e) => setSelected(e.target.value)}><option value="">请选择</option>{profiles.map((p) => <option key={p.id} value={p.id} disabled={!p.configuration?.ready}>{p.name} · {p.model_id || "未设置"} · {p.configuration?.label}</option>)}</select></Field>
      <div className="flex flex-wrap items-center gap-4"><button className={button} disabled={busy || !profiles.find((p) => p.id === selected)?.configuration?.ready} onClick={() => void act(() => settingsRequest("/model-lab/profiles/" + selected + "/set-default", "POST", {}), "平台默认模型已更新，可以开始研究")}>应用默认模型</button><a href="/settings/models" className="text-sm text-jade underline underline-offset-2">前往添加模型连接</a></div>
    </section>}
    {tab === "services" && <>
      <section className={section}>
        <div className="flex items-center justify-between gap-3"><h3 className="text-base font-medium text-ink">已保存的预测服务</h3><button className={button} disabled={busy} onClick={() => { setExternal(emptyService()); serviceForm.current?.scrollIntoView({ behavior: "smooth", block: "start" }); }}>新增连接</button></div>
        {!services.length && <p className="text-sm leading-relaxed text-mute">未设置外部预测服务。只有使用这类专用服务时才需要添加，不影响普通研究和内置量化回测。</p>}
        {services.map((s) => <article key={s.id} className="space-y-3 border-t border-edge pt-4">
          <p className="text-sm font-medium text-ink">{s.name} · {s.kind === "event" ? "事件预测" : "量化仓位预测"}</p>
          <p className="break-all text-xs text-mute">{s.endpoint || "未设置完整预测接口地址"}</p>
          <State state={s.configuration} action="用虚拟样例验证" />
          <div className="flex flex-wrap gap-2"><button className={button} disabled={busy} onClick={() => { setExternal({ ...emptyService(), ...s, api_key: "", secret_env_ref: "" }); serviceForm.current?.scrollIntoView({ behavior: "smooth", block: "start" }); }}>编辑</button><button className={button} disabled={busy || !s.endpoint || !s.is_active || (s.auth_mode !== "none" && !s.secret_configured)} onClick={() => void act(() => settingsRequest("/settings/services/" + s.id + "/validate", "POST", {}), "服务验证成功，可以在对应功能中选择使用")}>用虚拟样例验证</button></div>
        </article>)}
        <p className="text-xs leading-relaxed text-mute">验证会向服务发送一条虚拟样例，检查能否收到符合格式的预测结果。</p>
      </section>
      <form ref={serviceForm} className={section + " scroll-mt-6"} onSubmit={(e) => {
        e.preventDefault(); const { id, ...payload } = external;
        void act(async () => {
          const result = await settingsRequest<ServiceConnection>(id ? "/settings/services/" + id : "/settings/services", id ? "PUT" : "POST", { name: payload.name, kind: payload.kind, endpoint: payload.endpoint, auth_mode: payload.auth_mode, header_name: payload.header_name, api_key: payload.api_key, secret_env_ref: payload.secret_env_ref, timeout_seconds: payload.timeout_seconds, version: payload.version, is_active: payload.is_active, revision: payload.revision || null });
          setExternal({ ...emptyService(), ...result, api_key: "", secret_env_ref: "" }); return result;
        }, "服务已保存；首次配置或修改连接参数后，请点击上方「用虚拟样例验证」");
      }}>
        <h3 className="text-base font-medium text-ink">{external.id ? "编辑预测服务" : "添加预测服务"}</h3>
        <div className="grid gap-5 sm:grid-cols-2">
          <Field label="显示名称" hint="自己起的名称，方便在事件或量化任务中选择这个服务。"><input required className={input} value={external.name} onChange={(e) => setExternal({ ...external, name: e.target.value })} /></Field>
          <Field label="接口类型" hint="事件预测返回事件方向、预期收益等结果；量化仓位预测返回目标仓位，例如持有多少比例。"><select className={input} value={external.kind} onChange={(e) => setExternal({ ...external, kind: e.target.value as "event" | "quant" })}><option value="event">事件预测</option><option value="quant">量化仓位预测</option></select></Field>
          <Field label="完整预测接口地址" hint="由服务提供方给出，需要包含具体路径（例如 /predict）。这里填写完整预测接口地址，不是聊天模型的 Base URL。"><input className={input} value={external.endpoint} onChange={(e) => setExternal({ ...external, endpoint: e.target.value })} placeholder="例如：https://service.example/predict" /></Field>
          <Field label="认证方式" hint="Bearer 是常见的密钥传递方式；自定义请求头按服务商要求填写；环境变量引用会读取服务端已配置的密钥变量。无需认证时不必填密钥。"><select className={input} value={external.auth_mode} onChange={(e) => setExternal({ ...external, auth_mode: e.target.value })}><option value="none">无需认证</option><option value="bearer">API Key（Bearer，常见方式）</option><option value="header">自定义请求头</option><option value="env">环境变量引用</option></select></Field>
          {external.auth_mode !== "none" && <Field label={external.auth_mode === "env" ? "服务端密钥变量名" : "API Key"} hint={external.auth_mode === "env" ? "填写部署者已在服务端配置的变量名称，不是密钥原文；编辑时留空保留原引用。" : "填写服务提供方给出的密钥。保存后不显示原文；编辑时留空保留已有密钥。"}><input className={input} type={external.auth_mode === "env" ? "text" : "password"} autoComplete="new-password" value={external.auth_mode === "env" ? external.secret_env_ref : external.api_key} onChange={(e) => setExternal({ ...external, [external.auth_mode === "env" ? "secret_env_ref" : "api_key"]: e.target.value })} /></Field>}
          {["header", "env"].includes(external.auth_mode) && <Field label="认证请求头名称" hint="服务商要求用于传递密钥的字段名，例如 X-API-Key；不确定时请向服务提供方确认。"><input className={input} value={external.header_name} onChange={(e) => setExternal({ ...external, header_name: e.target.value })} /></Field>}
        </div>
        <details className="rounded-lg border border-edge p-3 sm:p-4"><summary className="cursor-pointer text-sm font-medium text-ink">高级选项</summary><div className="mt-4 grid gap-5 sm:grid-cols-2">
          <Field label="请求超时（秒）" hint="单次预测请求最多等待多久。"><input className={input} type="number" min={1} max={300} value={external.timeout_seconds} onChange={(e) => setExternal({ ...external, timeout_seconds: Number(e.target.value) })} /></Field>
          <Field label="版本" hint="记录服务提供方给出的版本，便于区分实验使用的配置；没有版本时可留空。"><input className={input} value={external.version} onChange={(e) => setExternal({ ...external, version: e.target.value })} /></Field>
        </div></details>
        <label className="flex gap-2 text-sm"><input type="checkbox" checked={external.is_active} onChange={(e) => setExternal({ ...external, is_active: e.target.checked })} />启用连接</label><button className={button} disabled={busy || !external.name.trim()}>保存服务／草稿</button>
      </form>
    </>}
    {tab === "simulation" && <form className={section} onSubmit={(e) => { e.preventDefault(); void act(() => settingsRequest("/settings/simulation", "PUT", gateway), "网关已保存，请点击「验证已保存的网关」"); }}>
      <h3 className="text-base font-medium text-ink">推演服务配置</h3>
      <State state={status?.simulation} action="验证已保存的网关" />
      <Field label="推演服务地址（网关）" hint="由推演服务部署者提供。它是负责启动和管理推演任务的程序地址，不是大模型 Base URL；这里无需填写模型 API Key。"><input className={input} value={gateway.endpoint} onChange={(e) => setGateway({ ...gateway, endpoint: e.target.value })} placeholder="例如：http://127.0.0.1:5010" /></Field>
      <details className="rounded-lg border border-edge p-3 sm:p-4"><summary className="cursor-pointer text-sm font-medium text-ink">高级选项</summary><div className="mt-4"><Field label="请求超时（秒）" hint="与推演服务的单次通信最多等待多久，不是整次推演的运行时长。"><input className={input} type="number" min={1} max={300} value={gateway.timeout_seconds} onChange={(e) => setGateway({ ...gateway, timeout_seconds: Number(e.target.value) })} /></Field></div></details>
      <label className="flex gap-2 text-sm"><input type="checkbox" checked={gateway.is_active} onChange={(e) => setGateway({ ...gateway, is_active: e.target.checked })} />启用网关</label>
      <p className="text-xs leading-relaxed text-mute">修改后请先保存，再验证已保存的网关；验证成功后才可以启动新的推演任务。</p>
      <div className="flex flex-wrap gap-2"><button className={button} disabled={busy}>保存网关／草稿</button><button type="button" className={button} disabled={busy || !gateway.endpoint || !gateway.is_active} onClick={() => void act(() => settingsRequest("/settings/simulation/validate", "POST", {}), "网关验证成功，可以启动事件推演")}>验证已保存的网关</button></div>
    </form>}
  </div></div>;
}
