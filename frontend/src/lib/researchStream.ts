import type { Part, SSEEvent } from "../types";

/** A stable call/round owns two independent channels, even if they interleave. */
export function appendStreamEvent(parts: Part[], event: SSEEvent): Part[] {
  if (event.round_id) {
    const index = parts.findIndex((p) => p.type === "model_round" && p.roundId === event.round_id);
    const previous = index >= 0 ? parts[index] as Extract<Part, { type: "model_round" }> : undefined;
    const part: Extract<Part, { type: "model_round" }> = previous ? { ...previous } : {
      type: "model_round", roundId: event.round_id, round: event.round, agent: event.agent,
      text: "", reasoning: "", status: "streaming",
    };
    if (event.type === "thinking") part.reasoning += event.delta ?? "";
    if (event.type === "token") part.text += event.delta ?? "";
    if (event.type === "model_round") part.status = event.status ?? "completed";
    return index >= 0 ? parts.map((p, i) => i === index ? part : p) : [...parts, part];
  }
  if (!event.delta || event.type === "model_round") return parts;
  const type = event.type === "thinking" ? "thinking" : "text";
  const last = parts[parts.length - 1];
  if (last?.type === type && last.agent === event.agent) {
    return [...parts.slice(0, -1), { ...last, text: last.text + event.delta }];
  }
  return [...parts, { type, agent: event.agent, text: event.delta }];
}

export function finishStreamRounds(parts: Part[], agent?: string): Part[] {
  return parts.map((p) => p.type === "model_round" && p.status === "streaming" &&
    (agent === undefined || agent === p.agent) ? { ...p, status: "interrupted" } : p);
}
