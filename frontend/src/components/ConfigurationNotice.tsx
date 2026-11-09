import { useSettingsStatus } from "../settingsClient";

export default function ConfigurationNotice({ feature = "model" }: { feature?: "model" | "simulation" }) {
  const { value, error } = useSettingsStatus();
  const state = value?.[feature];
  const toolsUnavailable = feature === "model" && value?.model.research_ready === false && state?.ready;
  if (state?.ready && !toolsUnavailable) return null;
  return <div role="status" className="mx-4 my-2 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
    {feature === "model" ? "平台模型" : "推演网关"}：{error || (toolsUnavailable ? "此连接未通过工具能力验证，请前往设置重新验证或选择其他连接" : state?.label) || "正在读取配置…"}
    {state?.missing.length ? `（缺少 ${state.missing.join("、")}）` : ""}
    {state?.message ? ` · ${state.message}` : ""}
    <a className="ml-3 underline" href={state?.settings_url || "/settings/models"}>前往设置</a>
  </div>;
}
