import { useEffect, useState } from "react";
import { settingsRequest, type ServiceConnection } from "../../settingsClient";
export interface ExternalServiceConfig {
  endpoint: string; version: string; auth_mode: "bearer" | "header" | "none" | "env";
  api_key: string; header_name: string; secret_env_ref: string; timeout_seconds: number;
  connection_id?: string; service_revision?: string; headers?: Record<string, string>;
}
export const defaultExternalService = (): ExternalServiceConfig => ({ endpoint: "", version: "", auth_mode: "bearer", api_key: "", header_name: "X-API-Key", secret_env_ref: "", timeout_seconds: 30 });
export function serviceConfigError(value: ExternalServiceConfig): string | null {
  return value.connection_id && value.endpoint ? null : "请选择已验证的预测服务连接；尚未设置时请前往设置";
}
export async function saveServiceCredentials(value: ExternalServiceConfig): Promise<Record<string, string>> {
  if (!value.connection_id) throw new Error("请先在设置中保存并验证预测服务连接");
  const spec = await settingsRequest<{ headers: Record<string, string> }>("/settings/services/" + value.connection_id + "/spec");
  return spec.headers;
}
export default function ExternalEventServiceEditor({ value, onChange, disabled, serviceKind = "event" }: {
  value: ExternalServiceConfig; onChange: (value: ExternalServiceConfig) => void; datasetId?: string; horizon: string;
  disabled?: boolean; allowPrivateEndpoints?: boolean; serviceKind?: "event" | "quant";
}) {
  const [items, setItems] = useState<ServiceConnection[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  useEffect(() => { let active = true; void settingsRequest<{items: ServiceConnection[]}>("/settings/services").then((data) => { if (active) setItems(data.items.filter((item) => item.kind === serviceKind)); }).catch((e: Error) => { if (active) setError(e.message); }); return () => { active = false; }; }, [serviceKind]);
  const select = async (id: string) => {
    setError("");
    if (!id) { onChange(defaultExternalService()); return; }
    setLoading(true);
    try {
      const spec = await settingsRequest<{endpoint: string; headers: Record<string,string>; timeout_seconds: number; version: string; service_revision: string}>("/settings/services/" + id + "/spec");
      onChange({ ...defaultExternalService(), ...spec, connection_id: id, auth_mode: "none" });
    } catch(e) { setError(e instanceof Error ? e.message : "读取连接失败"); }
    finally { setLoading(false); }
  };
  return <section className="space-y-3 rounded-xl border border-edge bg-card p-4"><label className="block text-sm text-ink">选择已验证的{serviceKind === "event" ? "事件" : "量化"}预测服务<select className="mt-2 w-full rounded-lg border border-edge bg-card px-3 py-2" disabled={disabled || loading} value={value.connection_id || ""} onChange={(e) => void select(e.target.value)}><option value="">未设置，请选择连接</option>{items.map((item) => <option key={item.id} value={item.id} disabled={!item.configuration.ready}>{item.name} · {item.configuration.label}</option>)}</select></label>{value.endpoint && <p className="break-all text-sm text-mute">{value.endpoint}</p>}{error && <p role="alert" className="text-sm text-rise">{error}</p>}<a href="/settings/services" className="text-sm text-violet underline">前往设置管理预测服务</a></section>;
}
