import type { Part } from "../types";
import { agentColor, agentName } from "../names";
import { useStore } from "../store";
import ThinkingBlock from "./ThinkingBlock";
import Markdown from "./Markdown";

export default function ModelRound({ part, pending, showBadge }: {
  part: Extract<Part, { type: "model_round" }>;
  pending: boolean;
  showBadge: boolean;
}) {
  const agents = useStore((s) => s.agents);
  const streaming = pending && part.status === "streaming";
  const color = agentColor(part.agent, agents);
  return (
    <div className="space-y-2.5" data-model-round={part.roundId}>
      {part.reasoning && <ThinkingBlock segments={[{ agent: part.agent, text: part.reasoning }]} streaming={streaming} />}
      {part.text && (
        <div>
          {showBadge && part.agent && (
            <div className="mb-1.5 flex items-center gap-1.5">
              <span className="h-2 w-2 rounded-full" style={{ backgroundColor: color }} />
              <span className="text-[12px] font-medium" style={{ color }}>{agentName(part.agent, agents)}</span>
            </div>
          )}
          <Markdown text={part.text} />
          {streaming && <span className="ml-0.5 inline-block h-[15px] w-[2.5px] translate-y-[2px] rounded-full bg-brand animate-blink" />}
        </div>
      )}
    </div>
  );
}
