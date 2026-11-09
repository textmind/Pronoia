import { useState } from "react";
import { Brain, ChevronRight } from "lucide-react";
import { useStore } from "../store";
import { agentColor, agentName } from "../names";
import { cls } from "../utils";

export interface ThinkingSegment {
  agent?: string;
  text: string;
}

/** 接口返回的思考文本：默认展开，稳定的父级 key 保留用户折叠选择。 */
export default function ThinkingBlock({
  segments,
  streaming,
}: {
  segments: ThinkingSegment[];
  streaming?: boolean;
}) {
  const [open, setOpen] = useState(true); // 默认展开，减少"下面卡住"的等待感
  const agents = useStore((s) => s.agents);

  const totalLen = segments.reduce((sum, s) => sum + s.text.length, 0);
  const distinctAgents = Array.from(new Set(segments.map((s) => s.agent).filter(Boolean) as string[]));
  const subtitle =
    distinctAgents.length > 1
      ? `${distinctAgents.length} 位专家`
      : distinctAgents.length === 1
        ? agentName(distinctAgents[0], agents)
        : "推理";

  return (
    <div className="rounded-card border border-edge bg-[#FBFAF8] overflow-hidden">
      <button
        type="button"
        aria-expanded={open}
        aria-label={open ? "收起思考过程" : "展开思考过程"}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3.5 py-2.5 text-left text-[12.5px] text-mute hover:bg-[#F4F2EE] transition-colors"
      >
        <Brain size={14} className="text-faint shrink-0" />
        <span className="font-medium">思考过程</span>
        {distinctAgents.length > 0 && (
          <span className="text-faint text-[11.5px]">· {subtitle}</span>
        )}
        {streaming && (
          <span className="relative flex h-1.5 w-1.5">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-jade/60" />
            <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-jade" />
          </span>
        )}
        <span className="ml-auto text-faint text-[11px]">{open ? "" : `${totalLen} 字`}</span>
        <ChevronRight
          size={14}
          className={cls("text-faint transition-transform duration-200", open && "rotate-90")}
        />
      </button>
      {open && (
        <div className="border-t border-edge/70 px-4 py-3 text-[12.5px] leading-[1.75] text-mute animate-fadeUp">
          {segments.map((seg, i) => {
            const color = agentColor(seg.agent, agents);
            const name = agentName(seg.agent, agents);
            const isLast = i === segments.length - 1;
            return (
              <div key={i} className="relative">
                {/* 左侧色条：贯穿段落 */}
                <span
                  aria-hidden
                  className="absolute left-[7px] top-[6px] bottom-0 w-px"
                  style={{ backgroundColor: `${color}55` }}
                />
                {/* 步骤节点：圆点 + 序号 + agent 名 */}
                <div className="relative flex items-center gap-2 pl-0">
                  <span
                    className="relative z-10 flex h-[15px] w-[15px] shrink-0 items-center justify-center rounded-full ring-2 ring-[#FBFAF8]"
                    style={{ backgroundColor: color }}
                  >
                    <span className="text-[9px] font-semibold leading-none text-white">{i + 1}</span>
                  </span>
                  <span className="text-[12px] font-semibold" style={{ color }}>
                    {name}
                  </span>
                  <span className="text-faint text-[10.5px]">· {seg.text.length} 字</span>
                </div>
                {/* 段落正文：缩进对齐到节点右侧 */}
                <div className="ml-[26px] mt-1 whitespace-pre-wrap text-[12.5px] text-ink/85">
                  {seg.text}
                  {streaming && isLast && (
                    <span className="ml-0.5 inline-block h-3 w-[2px] translate-y-[2px] bg-jade/70 animate-blink" />
                  )}
                </div>
                {!isLast && <div className="h-3" />}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
