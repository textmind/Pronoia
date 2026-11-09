import { useEffect, useRef, useState } from "react";
import { SendHorizontal, Square, Users, Zap, UserCircle2, ChevronDown } from "lucide-react";
import { cls } from "../utils";
import { useStore } from "../store";
import type { Mode } from "../types";
import AgentPicker from "./AgentPicker";
import TeamMemberPicker from "./TeamMemberPicker";
import ConfigurationNotice from "./ConfigurationNotice";
import { useSettingsStatus, useTaskDraft } from "../settingsClient";

/** 底部输入区：
 *  - 模式 segmented（auto/agent/team）
 *  - agent 模式时显示 chip 触发 Agent 选择对话框
 *  - team 模式时显示 chip 触发团队成员多选对话框
 *  - 自动增高 textarea + 发送/停止 */
export default function Composer() {
  const [text, setText] = useState("");
  useTaskDraft("pronoia.chat-composer-draft", text, setText);
  const { value: configuration } = useSettingsStatus();
  const [pickerOpen, setPickerOpen] = useState(false);
  const [teamPickerOpen, setTeamPickerOpen] = useState(false);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const streaming = useStore((s) => s.streaming);
  const researchError = useStore((s) => s.researchError);
  const mode = useStore((s) => s.mode);
  const setMode = useStore((s) => s.setMode);
  const sendMessage = useStore((s) => s.sendMessage);
  const stop = useStore((s) => s.stop);
  const agents = useStore((s) => s.agents);
  const selectedAgent = useStore((s) => s.selectedAgent);
  const setSelectedAgent = useStore((s) => s.setSelectedAgent);
  const teamMembers = useStore((s) => s.teamMembers);
  const setTeamMembers = useStore((s) => s.setTeamMembers);
  const promptSeed = useStore((s) => s.promptSeed);
  const setPromptSeed = useStore((s) => s.setPromptSeed);

  // 外部（chip / hot topic）填入 prompt：写入并 focus textarea
  useEffect(() => {
    if (!promptSeed) return;
    setText(promptSeed);
    setPromptSeed("");
    requestAnimationFrame(() => {
      const ta = taRef.current;
      if (ta) {
        ta.focus();
        // 移到末尾，方便继续编辑
        const len = ta.value.length;
        ta.setSelectionRange(len, len);
        ta.style.height = "0px";
        ta.style.height = Math.min(ta.scrollHeight, 180) + "px";
      }
    });
  }, [promptSeed, setPromptSeed]);

  // 自动增高
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "0px";
    ta.style.height = Math.min(ta.scrollHeight, 180) + "px";
  }, [text]);

  const canSend = text.trim().length > 0 && !streaming && configuration?.model.ready === true && configuration.model.research_ready !== false;
  const currentAgent = agents.find((a) => a.id === selectedAgent) || agents[0];
  // 过滤出可单独调度的"前台"专家（去掉内部调度/调度辅助 agent）
  const dispatchableAgents = agents.filter(
    (a) =>
      !["router", "planner", "synthesizer", "verifier", "report_writer"].includes(a.id),
  );

  const doSend = () => {
    if (!canSend) return;
    const t = text.trim();
    setText("");
    void sendMessage(t);
  };

  const modes: { id: Mode; label: string; icon: React.ReactNode; hint: string; color: string }[] = [
    { id: "auto", label: "快速", icon: <Zap size={13} />, hint: "主理人 · 工具循环", color: "bg-brand" },
    { id: "agent", label: "专家", icon: <UserCircle2 size={13} />, hint: "直接调度单个专家", color: "bg-violet" },
    { id: "team", label: "团队", icon: <Users size={13} />, hint: "Planner 拆解 · 专家串行 · 复核", color: "bg-jade" },
  ];

  const placeholder =
    mode === "team"
      ? `研究团队 主理人 + ${teamMembers.length + 1} 专家调度 · Planner 拆解 + 专家串行 + 复核…`
      : mode === "agent" && currentAgent
        ? `直接问「${currentAgent.name}」· 单专家 ≤8 轮工具循环…`
        : "主理人调度 · 任何财经事件、行情、公告、宏观问题…";

  const teamChipText = `${teamMembers.length + 1} 个专家`;

  return (
    <div className="border-t border-edge bg-paper/90 px-4 pb-4 pt-3 backdrop-blur">
      <ConfigurationNotice />
      {researchError && <p role="alert" className="mx-auto mb-2 max-w-3xl text-xs text-rise">{researchError}</p>}
      <div className="mx-auto w-full max-w-3xl">
        <div className="rounded-card border border-edge bg-card shadow-card transition-shadow focus-within:shadow-pop focus-within:border-edgeDark">
          {/* 模式切换（独占一行） */}
          <div className="flex items-center justify-between px-3 pt-2.5">
            <div className="flex items-center gap-2">
              <div className="flex rounded-lg border border-edge bg-paper p-0.5" role="tablist">
                {modes.map((m) => (
                  <button
                    key={m.id}
                    role="tab"
                    aria-selected={mode === m.id}
                    title={m.hint}
                    onClick={() => setMode(m.id)}
                    className={cls(
                      "flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[12px] font-medium transition-all duration-200",
                      mode === m.id
                        ? cls(m.color, "text-card shadow-sm")
                        : "text-mute hover:text-ink",
                    )}
                  >
                    {m.icon}
                    {m.label}
                  </button>
                ))}
              </div>
              {/* agent 模式：紧凑的 chip 触发器，点开弹出对话框 */}
              {mode === "agent" && currentAgent && (
                <button
                  onClick={() => setPickerOpen(true)}
                  className="inline-flex items-center gap-1.5 rounded-full border border-violet/40 bg-violet-soft px-2 py-0.5 text-[11px] font-medium text-violet transition-all hover:bg-violet hover:text-card"
                  title="选择 Agent"
                >
                  <span
                    className="inline-block h-1.5 w-1.5 rounded-full"
                    style={{ background: currentAgent.avatar_color || "#7C3AED" }}
                  />
                  {currentAgent.name}
                  <ChevronDown size={10} className="opacity-70" />
                </button>
              )}
              {/* team 模式：chip 显示已选专家数 */}
              {mode === "team" && (
                <button
                  onClick={() => setTeamPickerOpen(true)}
                  className="inline-flex items-center gap-1.5 rounded-full border border-jade/40 bg-jade-soft px-2 py-0.5 text-[11px] font-medium text-jade transition-all hover:bg-jade hover:text-card"
                  title="选择团队专家（深度研究者必派，主理人综合）"
                >
                  <Users size={10} />
                  {teamChipText}
                  <ChevronDown size={10} className="opacity-70" />
                </button>
              )}
            </div>
          </div>

          <textarea
            ref={taRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                doSend();
              }
            }}
            rows={1}
            placeholder={placeholder}
            className="mt-2 block w-full resize-none bg-transparent px-4 py-2.5 text-[14px] leading-relaxed text-ink placeholder:text-faint focus:outline-none"
          />

          <div className="flex items-center justify-between px-3 pb-2.5">
            <span className="text-[11px] text-faint">Enter 发送 · Shift+Enter 换行 · 数据来自 akshare 真实接口</span>
            {streaming ? (
              <button
                onClick={stop}
                className="flex items-center gap-1.5 rounded-lg border border-rise/40 bg-rise/5 px-3 py-1.5 text-[12.5px] font-medium text-rise transition-colors hover:bg-rise hover:text-card"
              >
                <Square size={12} fill="currentColor" />
                停止
              </button>
            ) : (
              <button
                onClick={doSend}
                disabled={!canSend}
                className={cls(
                  "flex items-center gap-1.5 rounded-lg px-3.5 py-1.5 text-[12.5px] font-medium transition-all duration-200",
                  canSend
                    ? "bg-brand text-card hover:bg-brand-hover shadow-sm"
                    : "cursor-not-allowed bg-edge/60 text-faint",
                )}
              >
                <SendHorizontal size={13} />
                发送
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Agent 选择对话框（弹窗） */}
      <AgentPicker
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        agents={dispatchableAgents}
        selectedId={selectedAgent}
        onSelect={(id) => {
          setSelectedAgent(id);
          // 保留焦点：让用户继续在 textarea 输入
        }}
      />

      {/* 团队成员多选对话框（team 模式） */}
      <TeamMemberPicker
        open={teamPickerOpen}
        onClose={() => setTeamPickerOpen(false)}
        agents={dispatchableAgents}
        selectedIds={teamMembers}
        onChange={setTeamMembers}
      />
    </div>
  );
}
