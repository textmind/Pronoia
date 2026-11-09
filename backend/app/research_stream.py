"""Durable display channels for each streamed model call/round.

Body text remains in messages.content for LLM history. The trace also keeps the
display layout so reasoning arriving after body text cannot split Markdown.
"""
from __future__ import annotations


def finish_rounds(trace: list[dict], *, agent: str | None = None) -> None:
    for item in trace:
        if (item.get("type") == "model_round" and item.get("status") == "streaming"
                and (agent is None or item.get("agent") == agent)):
            item["status"] = "interrupted"


def remember_stream_event(trace: list[dict], event: dict) -> bool:
    """Consume display deltas/end events; return False for other event types."""
    kind = event.get("type")
    if kind not in {"thinking", "token", "model_round"}:
        return False
    round_id = event.get("round_id")
    agent = event.get("agent")
    if round_id:
        item = next((x for x in trace if x.get("type") == "model_round"
                     and x.get("round_id") == round_id), None)
        if item is None:
            item = {"type": "model_round", "agent": agent, "round_id": round_id,
                    "round": event.get("round"), "text": "", "reasoning": "",
                    "status": "streaming"}
            trace.append(item)
        if kind == "model_round":
            item["status"] = event.get("status") or "completed"
        else:
            field = "reasoning" if kind == "thinking" else "text"
            item[field] += str(event.get("delta") or "")
        return True
    delta = str(event.get("delta") or "")
    if delta and kind != "model_round":
        # Compatibility for program-generated text and older event producers.
        trace_kind = "thinking" if kind == "thinking" else "text"
        last = trace[-1] if trace else None
        if last and last.get("type") == trace_kind and last.get("agent") == agent:
            last["text"] = str(last.get("text") or "") + delta
        else:
            trace.append({"type": trace_kind, "agent": agent, "text": delta})
    return True
