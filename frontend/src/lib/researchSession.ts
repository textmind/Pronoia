import type { Artifact, Message } from "../types";

export interface ResearchSession {
  caseId: string;
  messageId: string;
  question: string;
  runId?: string;
  messages: Message[];
  artifacts: Artifact[];
}

// A stream belongs to its original research, never to the currently open page.
export function researchUpdate(state: { currentCaseId: string | null; activeResearch: ResearchSession | null }, change: (session: ResearchSession) => ResearchSession) {
  if (!state.activeResearch) return {};
  const next = change(state.activeResearch);
  return { activeResearch: next, ...(state.currentCaseId === next.caseId ? { messages: next.messages, artifacts: next.artifacts } : {}) };
}
