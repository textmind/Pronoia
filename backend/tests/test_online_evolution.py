import json
from pathlib import Path

from app.evolution import online


def test_episode_redacts_credentials_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(online, "ROOT", tmp_path)
    monkeypatch.setattr(online, "EPISODES", tmp_path / "episodes")
    monkeypatch.setattr(online, "QUEUE", tmp_path / "queue")
    monkeypatch.setattr(online, "CANDIDATES", tmp_path / "candidates")
    monkeypatch.setattr(online, "RUNTIME", tmp_path / "runtime")
    eid = online.capture_episode(
        case_id="case", message_id="message", user_text="token=secret-value question",
        assistant_text="answer", trace=[{"type": "tool_result", "value": "Authorization: Bearer abc"}],
        mode="team", model_revision="model-v1",
    )
    online.capture_episode(
        case_id="case", message_id="message", user_text="token=secret-value question",
        assistant_text="answer", trace=[{"type": "tool_result", "value": "Authorization: Bearer abc"}],
        mode="team", model_revision="model-v1",
    )
    episode = json.loads((tmp_path / "episodes" / f"{eid}.json").read_text())
    assert "secret-value" not in json.dumps(episode)
    assert "Bearer abc" not in json.dumps(episode)
    assert json.loads((tmp_path / "queue" / f"{eid}.json").read_text())["status"] == "queued"


def test_candidate_reaches_active_after_three_same_signal_episodes(tmp_path, monkeypatch):
    monkeypatch.setattr(online, "ROOT", tmp_path)
    monkeypatch.setattr(online, "EPISODES", tmp_path / "episodes")
    monkeypatch.setattr(online, "QUEUE", tmp_path / "queue")
    monkeypatch.setattr(online, "CANDIDATES", tmp_path / "candidates")
    monkeypatch.setattr(online, "RUNTIME", tmp_path / "runtime")

    class FakeEngine:
        def __init__(self):
            self.genes = []

        def snapshot(self):
            return {"genes": self.genes, "capsules": []}

        def call(self, op, **kwargs):
            if op == "extract":
                return {"signals": ["research"]}
            if op == "snapshot":
                return self.snapshot()
            if op == "select":
                return {"selectedGene": kwargs["genes"][0], "selectedCapsule": None,
                        "selector": {}, "memoryAdvice": {}}
            if op == "feedback":
                return {"duplicate": False}
            raise AssertionError(op)

        def seal(self, asset):
            asset = dict(asset)
            asset["asset_id"] = "sha256:" + str(abs(hash(asset["id"]))).zfill(64)[-64:]
            return asset

        def install(self, genes):
            for gene in genes:
                if not any(item["id"] == gene["id"] for item in self.genes):
                    self.genes.append(gene)

    fake = FakeEngine()
    monkeypatch.setattr(online, "_engine", lambda: fake)
    for index in range(3):
        eid = online.capture_episode(case_id="c", message_id=str(index), user_text="same", assistant_text="answer")
        online.process_episode(eid)
    candidates = list((tmp_path / "candidates").glob("*.json"))
    assert len(candidates) == 1
    assert json.loads(candidates[0].read_text())["status"] == "active"

    context = online.quick_context("请分析公司财报", task_id="chat-next")
    assert context["genes"][0]["id"].startswith("gene_pronoia_chat_")
