import type { ModelLabProfile } from "../../types";
export default function EventModelConnectionEditor({ disabled = false }: { onCreated: (profile: ModelLabProfile) => void; disabled?: boolean }) {
  return <a href="/settings/models" aria-disabled={disabled} className="text-sm text-violet underline">前往设置管理大模型连接</a>;
}
