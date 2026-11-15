"""Typed process boundary to official EvoMap; no reimplemented selection algorithm."""
from __future__ import annotations

import fcntl
import json
import subprocess
import os
import re
import shutil
import threading
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BRIDGE = ROOT / "integrations" / "evomap" / "bridge.mjs"


class GEPError(RuntimeError):
    pass


@lru_cache(maxsize=128)
def _process_lock(root):
    """fcntl locks are process-scoped, so concurrent Python threads need one too."""
    return threading.Lock()


@lru_cache(maxsize=1)
def node_binary():
    """Desktop-launched web workers may not inherit the interactive nvm PATH."""
    configured = os.environ.get("PRONOIA_EVOMAP_NODE")
    candidates = [configured] if configured else [shutil.which("node"), "/opt/homebrew/bin/node", "/usr/local/bin/node"]
    if not configured:
        versions = Path.home() / ".nvm/versions/node"
        def version(path):
            match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", path.parent.parent.name)
            return tuple(map(int, match.groups())) if match else (0, 0, 0)
        candidates += [str(p) for p in sorted(versions.glob("v*/bin/node"), key=version, reverse=True)]
    for candidate in dict.fromkeys(candidates):
        if not candidate or not Path(candidate).is_file() or not os.access(candidate, os.X_OK):
            continue
        try:
            output = subprocess.run([candidate, "--version"], text=True, capture_output=True, timeout=5)
            match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", output.stdout.strip())
            parsed = tuple(map(int, match.groups())) if match else (0, 0, 0)
            supported = parsed[0] == 22 and parsed >= (22, 13, 0) or parsed >= (23, 4, 0)
            if output.returncode == 0 and supported:
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    raise GEPError("EvoMap v2 需要 Node.js ^22.13 或 >=23.4；请安装或设置 PRONOIA_EVOMAP_NODE。")


class EvoMap:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def call(self, operation: str, **payload):
        if not (BRIDGE.parent / "node_modules" / "@evomap" / "evolver-core").exists():
            raise GEPError("EvoMap runtime is missing: run npm ci --ignore-scripts in integrations/evomap")
        with _process_lock(str(self.root)):
            with (self.root / ".bridge.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                result = subprocess.run([node_binary(), str(BRIDGE)], input=json.dumps({"protocol_version": 2, "op": operation, "root": str(self.root), **payload}, ensure_ascii=False, allow_nan=False),
                                        text=True, capture_output=True, timeout=60, cwd=ROOT)
        try:
            envelope = json.loads(result.stdout)
        except ValueError as exc:
            raise GEPError("EvoMap bridge returned an invalid response") from exc
        if result.returncode or not envelope.get("ok"):
            raise GEPError(envelope.get("error", "EvoMap bridge failed"))
        return envelope["result"]

    def seal(self, asset):
        return self.call("seal", asset=asset)

    def install(self, genes):
        return self.call("install", genes=genes)

    def select(self, signals, *, genes=None, capsules=None):
        options = {k: v for k, v in {"genes": genes, "capsules": capsules}.items() if v is not None}
        return self.call("select", signals=signals, **options)

    def inspect(self):
        return self.call("inspect")

    def prepare_cycle(self, **payload):
        return self.call("prepare_cycle", **payload)

    def commit_cycle(self, receipt, execution, **payload):
        return self.call("commit_cycle", receipt=receipt, execution=execution, **payload)

    def benchmark(self, pairs):
        return self.call("benchmark", pairs=pairs)

    def snapshot(self):
        return self.call("snapshot")
