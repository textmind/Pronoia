from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.event_backtest import engine, orchestrator
from app.event_backtest.models import EventRecord
from app.evolution.runtime import evolution_context


def _event() -> EventRecord:
    return EventRecord(
        event_id="progress-event-1",
        symbol="sh600000",
        market="CN",
        event_time="2025-01-02T15:01:00+08:00",
        event_type_l2="财报",
        title="历史事件进度测试",
        event_text="公司披露历史财务数据。",
        source_url="https://example.com/progress-event-1",
    )


def test_team_full_emits_all_observable_stages_without_changing_prediction(tmp_path):
    stages: list[dict] = []

    async def fake_run_team(_question, *, state, **_kwargs):
        yield {"type": "agent_step", "phase": "plan", "plan": [
            {"agent": "market_analyst"},
            {"agent": "fundamentals_analyst"},
        ]}
        yield {"type": "agent_step", "phase": "agent_start", "agent": "market_analyst"}
        yield {"type": "agent_step", "phase": "agent_done", "agent": "market_analyst"}
        yield {"type": "agent_step", "phase": "agent_start", "agent": "fundamentals_analyst"}
        yield {"type": "agent_step", "phase": "agent_done", "agent": "fundamentals_analyst"}
        yield {"type": "agent_step", "phase": "signal_routing", "note": "结构化信号已对齐"}
        yield {"type": "thinking", "agent": "verifier", "delta": "正在复核事实与逻辑"}
        yield {"type": "agent_step", "phase": "verified", "agent": "verifier", "note": "verdict=pass"}
        yield {"type": "thinking", "agent": "router", "delta": "正在提炼可证伪的研究假设"}
        yield {"type": "logic_items", "items": [{"hypothesis": "测试假设"}]}
        state["content"] = (
            "【最终方向】 up\n"
            "【置信度】 0.70\n"
            "【中文理由】 历史可得信息支持方向判断。\n"
            "【依据原文片段】 公司披露历史财务数据。"
        )

    with patch("app.agents.team.run_team", new=fake_run_team):
        prediction = asyncio.run(engine.run_team_full_one_event(
            _event(),
            run_id="progress-run",
            model_version="fake-model",
            trajectory_ckpt_dir=tmp_path,
            on_stage_callback=stages.append,
        ))

    observed = {item["stage"] for item in stages}
    assert {
        "planning", "expert_research", "synthesis", "review", "hypothesis_extraction",
    }.issubset(observed)
    assert prediction.pred_direction == "up"
    assert prediction.confidence == 0.70
    assert prediction.abstain is False


def test_bounded_team_full_keeps_all_stages_and_passes_cost_profile(tmp_path):
    captured={}
    async def fake_run_team(question, *, state, **kwargs):
        captured.update(question=question,kwargs=kwargs)
        state['content']=("【预测状态】 available\n【最终方向】 down\n【置信度】 0.60\n"
                          "【中文理由】\n事前证据偏空。\n【预期收益率】 null")
        yield {'type':'done'}
    with patch('app.agents.team.run_team',new=fake_run_team):
        prediction=asyncio.run(engine.run_team_full_one_event(
            _event(),run_id='bounded-run',model_version='fake-model',trajectory_ckpt_dir=tmp_path,
            bounded_full=True))
    assert captured['kwargs']['bounded_full'] is True
    assert captured['kwargs']['team_members']==['fundamentals_analyst','deep_researcher']
    assert 'skip_verify' not in captured['kwargs'] and 'skip_hypothesis' not in captured['kwargs']
    assert 'plan → 专家研究 → deep researcher 证据图 → synthesize → verify → extract hypotheses' in captured['question']
    assert prediction.pred_direction=='down' and prediction.abstain is False


def test_bounded_team_full_receives_task_local_gene_context(tmp_path):
    captured = {}

    async def fake_run_team(question, *, state, **kwargs):
        captured["question"] = question
        state["content"] = "【预测状态】 available\n【最终方向】 up\n【置信度】 0.60\n【中文理由】 gene"
        yield {"type": "done"}

    with patch("app.agents.team.run_team", new=fake_run_team):
        with evolution_context(
            [{"id": "gene_test", "asset_id": "sha256:" + "a" * 64,
              "strategy": ["保留事前证据边界"]}],
            task_id="gene-context-test",
        ):
            prediction = asyncio.run(engine.run_team_full_one_event(
                _event(), run_id="gene-context-run", model_version="fake-model",
                trajectory_ckpt_dir=tmp_path, bounded_full=True,
            ))
    assert "gene_test" in captured["question"]
    assert "保留事前证据边界" in captured["question"]
    assert prediction.pred_direction == "up"


def test_runtime_activity_snapshot_and_arena_redaction():
    run_id = "activity-run"
    orchestrator.clear_run_activity(run_id)
    try:
        orchestrator.record_team_full_stage(run_id, {
            "event_id": "secret-event-id",
            "symbol": "SECRET",
            "market": "CN",
            "title": "私密事件",
            "stage": "expert_research",
            "detail": "正在运行 market_analyst",
            "agent": "market_analyst",
        })
        private = orchestrator.get_run_activity(run_id)
        assert private is not None
        assert private["active_count"] == 1
        assert private["active_events"][0]["event_id"] == "secret-event-id"
        assert private["active_events"][0]["stage_index"] == 2
        assert private["active_events"][0]["elapsed_seconds"] >= 0

        redacted = orchestrator.get_run_activity(run_id, aggregate_only=True)
        assert redacted is not None
        assert redacted["privacy"] == "arena_safe"
        assert "event_id" not in redacted["active_events"][0]
        assert "symbol" not in redacted["active_events"][0]

        orchestrator.finish_team_full_event_activity(run_id, "secret-event-id")
        finished = orchestrator.get_run_activity(run_id)
        assert finished is not None
        assert finished["active_count"] == 0
    finally:
        orchestrator.clear_run_activity(run_id)
