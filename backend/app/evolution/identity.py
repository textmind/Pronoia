"""Freeze executable prediction/optimization/scoring policy, excluding presentation."""
from pathlib import Path
from ..event_backtest.evolution.rubric import digest


def implementation_sources():
    root = Path(__file__).resolve().parents[3]
    files = ["backend/app/evolution/" + name + ".py" for name in ("experiment", "policy", "runtime", "score", "corpus", "gep", "identity", "meter", "baseline")]
    files += ["backend/app/event_backtest/engine.py", "backend/app/agents/team.py", "backend/app/llm.py",
              "backend/app/event_backtest/evolution/rubric.py", "backend/app/event_backtest/evolution/structured.py",
              "backend/app/event_backtest/evolution/evaluation.py", "integrations/evomap/bridge.mjs", "integrations/evomap/package-lock.json"]
    return {filename: (root / filename).read_text() for filename in files}


def implementation_hash():
    return digest(implementation_sources())
