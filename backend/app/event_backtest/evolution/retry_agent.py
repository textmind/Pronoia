"""Explicit, logged recovery of a truncated optimizer request, never a prediction.

Replays exactly the saved system/input payload. It cannot create a new candidate
request, alter the prediction policy, or access Oracle files.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from ... import config
from ...llm import LLMRuntimeTarget, complete_json_diagnostic, runtime_target_context
from .prepare import write_json
from .rubric import digest


async def recover(root: Path, round_number: int, agent: str, output_limit: int):
    if (root / "final_lock.json").exists():
        raise ValueError("cannot recover optimizer after final lock")
    manifest = json.loads((root / "manifest.json").read_text())
    if (manifest["model"]["model"] != config.LLM_MODEL
            or manifest["model"]["endpoint_hash"] != digest(config.LLM_BASE_URL)):
        raise ValueError("model or endpoint changed")
    folder = root / "rounds" / f"{round_number:02d}"
    path = folder / f"{agent}.json"
    if path.exists() or (folder / "decision.json").exists():
        raise ValueError("optimizer result/decision already exists")
    request = json.loads(path.with_suffix(".request.json").read_text())
    if request["hash"] != digest([request["system"], request["input"]]):
        raise ValueError("saved request was altered")
    recovery_path = folder / f"{agent}.recovery.json"
    if recovery_path.exists():
        raise ValueError("one explicit recovery per request; already attempted")
    record = {"reason": "output_length_exhausted", "request_hash": request["hash"],
              "old_max_output_tokens": manifest["model"]["max_output_tokens"],
              "new_optimizer_max_output_tokens": output_limit,
              "prediction_config_changed": False, "status": "started"}
    write_json(recovery_path, record)
    target = LLMRuntimeTarget(config.LLM_BASE_URL, config.LLM_API_KEY, config.LLM_MODEL,
                             timeout_seconds=300, max_output_tokens=output_limit, thinking_mode="disabled")
    with runtime_target_context(target):
        response = await complete_json_diagnostic(request["system"],
            json.dumps(request["input"], ensure_ascii=False), max_tokens=output_limit)
    usage_path = path.with_suffix(".usage.json")
    old_usage = json.loads(usage_path.read_text()) if usage_path.exists() else {}
    combined = {k: int(old_usage.get(k, 0)) + int(response.usage.get(k, 0))
                for k in set(old_usage) | set(response.usage)}
    write_json(usage_path, combined)
    record.update(status="completed" if response.value is not None else "failed",
                  response_audit=response.audit_metadata())
    write_json(recovery_path, record)
    if response.value is None:
        raise RuntimeError("bounded optimizer recovery failed")
    write_json(path, response.value)
    print("RECOVERED", agent, "round", round_number, "tokens", response.usage, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--round", type=int, required=True, choices=range(1, 6))
    parser.add_argument("--agent", choices=["attribution", "proposal"], required=True)
    parser.add_argument("--output-limit", type=int, default=16384, choices=[16384])
    args = parser.parse_args()
    asyncio.run(recover(args.experiment, args.round, args.agent, args.output_limit))
