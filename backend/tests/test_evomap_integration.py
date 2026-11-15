import copy
import datetime as dt
from pathlib import Path

import pytest

from app.evolution.corpus import Corpus
from app.evolution.gep import EvoMap, GEPError


def test_python_calls_official_engine_not_a_local_selector_stub(tmp_path):
    engine = EvoMap(tmp_path / "runtime")
    identity = engine.call("inspect")
    assert identity["engine"] == "@evomap/evolver" and identity["engine_version"] == "2.0.51"
    assert identity["public_api_only"] is True and identity["protocol_version"] == 2
    assert identity["network_publication"] is False
    asset = engine.seal({"type": "Gene", "id": "gene_finance_v1", "category": "optimize", "signals_match": ["earnings"],
                         "strategy": ["Use only as-of earnings facts"], "constraints": {"max_files": 1, "forbidden_paths": ["oracle"]},
                         "validation": ["pronoia:heldout_accuracy"]})
    engine.install([asset])
    chosen = engine.select(["earnings"], capsules=[])
    assert chosen["selectedGene"]["asset_id"] == asset["asset_id"]
    assert chosen["selector"] and chosen["memoryAdvice"]
    with pytest.raises(GEPError, match="schema rejected"):
        engine.seal({"type": "Gene", "id": "invalid"})


def task(corpus, split="learn", **updates):
    arguments = {"task_id": "task_1", "split": split, "event": {"event_id": "event_1", "event_time": "2024-01-01"},
                 "prediction": {"pred_direction": "up", "abstain": False}, "genes": [{"id": "gene_1", "asset_id": "sha256:" + "1" * 64}],
                 "trace": {"phases": ["prediction"]}, "model_revision": "model_revision_1", "generation": 0}
    return corpus.record(**{**arguments, **updates})


def label():
    return {"event_id": "event_1", "label_t3": "up", "label_available_at": "2024-01-05T15:00:00+08:00"}


def test_tasks_are_immutable_and_retries_are_idempotent(tmp_path):
    corpus = Corpus(tmp_path)
    first = task(corpus)
    assert task(corpus) == first
    with pytest.raises(ValueError, match="immutable"):
        task(corpus, prediction={"pred_direction": "down", "abstain": False})


@pytest.mark.parametrize("bad", [{"label_t3": "up"}, {"nested": {"api_key": "not-a-real-key"}}, {"headers": {"Authorization": "Bearer synthetic"}}])
def test_predictor_corpus_rejects_answers_and_credentials(tmp_path, bad):
    with pytest.raises(ValueError, match="corpus"):
        task(Corpus(tmp_path), trace=bad)


@pytest.mark.parametrize("split", ["validation", "test"])
def test_heldout_answers_cannot_feed_learning_memory(tmp_path, split):
    corpus = Corpus(tmp_path)
    record = task(corpus, split=split)
    with pytest.raises(ValueError, match="only learning"):
        corpus.learning_feedback(record, label(), label_hash="1" * 64)


def test_task_feedback_is_linked_and_observable_before_recall(tmp_path):
    corpus = Corpus(tmp_path)
    record = task(corpus)
    feedback = corpus.learning_feedback(record, label(), label_hash="1" * 64)
    assert feedback["correct"] and feedback["task_hash"] == record["content_hash"]
    assert corpus.feedback_before("2024-01-05T14:59:59+08:00") == []
    assert corpus.feedback_before("2024-01-05T15:00:00+08:00") == [feedback]
    assert corpus.learning_feedback(record, label(), label_hash="1" * 64) == feedback
    with pytest.raises(ValueError, match="immutable"):
        corpus.learning_feedback(record, {**label(), "label_t3": "down"}, label_hash="1" * 64)
    with pytest.raises(ValueError, match="another event"):
        corpus.learning_feedback(record, {**label(), "event_id": "other"}, label_hash="1" * 64)


def test_future_or_undated_outcomes_do_not_create_experience(tmp_path):
    corpus = Corpus(tmp_path)
    record = task(corpus)
    for stamp in (None, "2024-01-05T15:00:00", "2099-01-05T15:00:00+08:00"):
        with pytest.raises(ValueError):
            corpus.learning_feedback(record, {**label(), "label_available_at": stamp}, label_hash="1" * 64)


def test_desktop_server_can_find_supported_node_without_interactive_path(tmp_path,monkeypatch):
    from app.evolution.gep import node_binary
    monkeypatch.setenv('PATH','/usr/bin:/bin')
    node_binary.cache_clear()
    try:
        result=EvoMap(tmp_path/'desktop-runtime').call('inspect')
        assert result['engine_version']=='2.0.51'
    finally:
        node_binary.cache_clear()
