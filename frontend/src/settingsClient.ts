import { useEffect, useState } from "react";
import { readTaskDraft, saveTaskDraft } from "./lib/taskDrafts";

export type Configuration = { state: string; label: string; ready: boolean; missing: string[]; message: string; settings_url: string; validated_at?: string; };
export type SettingsStatus = { initialized: boolean; model: Configuration & { name?: string; model_id?: string; profile_id?: string; research_ready?: boolean }; simulation: Configuration; services: ServiceConnection[] };
export type ServiceConnection = { id: string; name: string; kind: "event" | "quant"; endpoint: string; auth_mode: "none" | "bearer" | "header" | "env"; header_name: string; timeout_seconds: number; version: string; is_active: boolean; revision: string; secret_configured: boolean; configuration: Configuration };
export async function settingsRequest<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch("/api" + path, { method, headers: { "Content-Type": "application/json" }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  const data = await response.json();
  if (!response.ok) {
    const detail = data.detail;
    throw new Error(typeof detail === "string" ? detail : detail?.message ?? (Array.isArray(detail) ? detail.map((e: { msg: string }) => e.msg).join("；") : "设置操作失败，请重试"));
  }
  return data as T;
}
export function settingsChanged() { window.dispatchEvent(new Event("pronoia:settings-changed")); }
export function useSettingsStatus() {
  const [value, setValue] = useState<SettingsStatus | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    const load = () => settingsRequest<SettingsStatus>("/settings/status").then((data) => { if (active) { setValue(data); setError(""); } }).catch(() => { if (active) setError("无法读取配置状态，请刷新重试"); });
    void load();
    window.addEventListener("pronoia:settings-changed", load);
    window.addEventListener("focus", load);
    return () => { active = false; window.removeEventListener("pronoia:settings-changed", load); window.removeEventListener("focus", load); };
  }, []);
  return { value, error };
}

// Task drafts contain only public configuration, never credentials.
export function useTaskDraft<T>(key: string, value: T, restore: (value: T) => void) {
  useEffect(() => {
    const saved = readTaskDraft<T>(key); if (saved) restore(saved);
    // Restore only on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    saveTaskDraft(key, value);
  }, [key, value]);
}
