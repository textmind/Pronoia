import unittest
from types import SimpleNamespace
from unittest.mock import patch

import backend.app.agents.team as team_mod
import backend.app.llm as llm_mod
from backend.app.agents.roster import (
    get_agent,
    resolve_deep_researcher_prompt_variant,
    system_prompt,
)
from backend.app.agents.research_context import ResearchContext
from backend.app.llm import run_agent
from backend.app.agents.roster import AGENTS
from backend.app.skills.evidence_graph import EvidenceGraph
from backend.app.skills.registry import ensure_skills_loaded, tools_for_agent


class TestAgentAndTeam(unittest.IsolatedAsyncioTestCase):
    def test_deep_researcher_prompt_variants_are_registered(self):
        self.assertEqual(resolve_deep_researcher_prompt_variant("v0"), "deep_researcher_v0")
        self.assertEqual(
            resolve_deep_researcher_prompt_variant("claim_v2"),
            "deep_researcher_claim_v2",
        )
        old_prompt = system_prompt("deep_researcher", "deep_researcher_v0")
        new_prompt = system_prompt("deep_researcher", "deep_researcher_claim_v2")
        # v0 = 分支 RLVR 版（含量价 regime 校验 step 0）
        self.assertIn("量价 regime 校验", old_prompt)
        self.assertIn("Evidence → Claim → Link → audit → export", new_prompt)
        self.assertIn("audit()", new_prompt)
        self.assertNotEqual(
            get_agent("deep_researcher", "v0")["persona"],
            get_agent("deep_researcher", "claim_v2")["persona"],
        )

    def test_predictor_owns_async_scenario_handoff_without_probability_claim(self):
        # 分支 RLVR 设计：predictor 改用世界模型三情景（乐观/中性/悲观），
        # 不再使用 main 的「异步多智能体事件推演」hand-off 表述。
        predictor = AGENTS["predictor"]
        self.assertIn("后市推演", predictor["description"])
        self.assertIn("前瞻推演", predictor["persona"])
        self.assertIn("乐观", predictor["persona"])
        self.assertIn("悲观", predictor["persona"])

    def test_agent_tools_visibility_filters_internal(self):
        ensure_skills_loaded()
        tools = tools_for_agent("router")
        names = [t["function"]["name"] for t in tools]
        self.assertIn("announcement_onepager", names)
        self.assertIn("evidence_ledger", names)
        self.assertIn("search_stock", names)
        self.assertIn("get_current_date", names)
        self.assertNotIn("get_stock_news", names)

    async def test_team_run_team_filters_plan_by_team_members(self):
        async def fake_complete_json(system, user, *, max_tokens=2000):
            return {
                "tasks": [
                    {"agent": "market_analyst", "task": "t1"},
                    {"agent": "fundamentals_analyst", "task": "t2"},
                    {"agent": "deep_researcher", "task": "t3"},
                ]
            }

        async def fake_run_expert_serial(agent_id, task, question, artifact_store, **kwargs):
            yield {"type": "agent_step", "phase": "agent_done", "agent": agent_id, "note": "done"}
            yield {"type": "agent_findings", "agent": agent_id, "findings": f"{agent_id}:{task}", "tool_trace": []}

        async def fake_run_agent(agent_id, messages, *, agent_def, state, artifact_store,
                                 skill_executor=None, max_rounds=3, emit_thinking=True):
            state["content"] += "final"
            yield {"type": "token", "agent": agent_id, "delta": "final"}

        async def fake_artifact_store(kind, title, payload):
            return {"id": "a1", "kind": kind, "title": title, "payload": payload}

        with (
            patch.object(team_mod, "complete_json", fake_complete_json),
            patch.object(team_mod, "_run_expert_serial", fake_run_expert_serial),
            patch.object(team_mod, "run_agent", fake_run_agent),
        ):
            state = {"content": "", "tool_trace": []}
            evs = []
            async for ev in team_mod.run_team(
                "q",
                history=[],
                state=state,
                artifact_store=fake_artifact_store,
                team_members=["market_analyst"],
            ):
                evs.append(ev)

        plan_ev = next(e for e in evs if e.get("type") == "agent_step" and e.get("phase") == "plan")
        plan_agents = [p["agent"] for p in plan_ev["plan"]]
        self.assertIn("market_analyst", plan_agents)
        self.assertIn("deep_researcher", plan_agents)
        self.assertNotIn("fundamentals_analyst", plan_agents)
        self.assertEqual(plan_agents[-1], "deep_researcher")
        self.assertEqual(
            [item["agent"] for item in state["team_plan"]], plan_agents
        )
        self.assertTrue(state["content"].endswith("final"))

    async def test_team_context_reuses_result_and_suppresses_duplicate_artifact(self):
        """The same tool result is shown to both agents but stored once."""
        def chunk(*, tool_call=None, content=None, finish_reason=None):
            delta = SimpleNamespace(content=content, reasoning_content=None,
                                    tool_calls=tool_call)
            return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)])

        def tool_stream(call_id):
            async def gen():
                function = SimpleNamespace(name="shared_skill", arguments='{"symbol":"000001"}')
                tool_call = SimpleNamespace(index=0, id=call_id, function=function)
                yield chunk(tool_call=[tool_call], finish_reason="tool_calls")
            return gen()

        def final_stream():
            async def gen():
                yield chunk(content="完成", finish_reason="stop")
            return gen()

        class FakeCompletions:
            def __init__(self):
                self.n = 0

            async def create(self, **kwargs):
                self.n += 1
                return tool_stream(f"call-{self.n}") if self.n % 2 else final_stream()

        completions = FakeCompletions()
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        executions = 0
        artifacts = []

        async def raw_executor(name, args):
            nonlocal executions
            executions += 1
            return {"ok": True, "data": [{"symbol": args["symbol"]}],
                    "meta": {"source": "test", "rows": 1},
                    "artifact": {"kind": "table", "title": "共享行情", "payload": {"rows": 1}}}

        async def artifact_store(kind, title, payload):
            artifacts.append((kind, title, payload))
            return {"id": f"a{len(artifacts)}"}

        context = ResearchContext()

        async def context_executor(name, args):
            return await context.execute(raw_executor, name, args)

        async def one_agent(agent_id):
            state = {"content": "", "tool_trace": [], "rounds": 0}
            events = []
            async for event in run_agent(
                agent_id, [{"role": "user", "content": "test"}],
                agent_def={"skills": []}, state=state, artifact_store=artifact_store,
                skill_executor=context_executor, max_rounds=2,
            ):
                events.append(event)
            return state, events

        with (
            patch.object(llm_mod, "get_client", return_value=fake_client),
            patch.object(llm_mod, "ensure_skills_loaded"),
            patch.object(llm_mod, "tools_for_agent", return_value=[{"type": "function", "function": {"name": "shared_skill", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}}}]),
        ):
            first_state, first_events = await one_agent("first")
            second_state, second_events = await one_agent("second")

        self.assertEqual(executions, 1)
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(context.stats(), {"calls": 2, "unique_calls": 1, "reuses": 1, "slices": 0})
        self.assertFalse(first_state["tool_trace"][0]["reused"])
        self.assertTrue(second_state["tool_trace"][0]["reused"])
        self.assertEqual(len([e for e in first_events + second_events if e["type"] == "artifact"]), 1)

    async def test_team_context_normalizes_equivalent_news_intel_arguments(self):
        """Equivalent news_intel forms should share one run-scoped result."""
        executions = 0

        async def raw_executor(name, args):
            nonlocal executions
            executions += 1
            return {"ok": True, "data": {"name": name, "args": args}, "meta": {}}

        context = ResearchContext()
        first = await context.execute(raw_executor, "news_intel", {
            "symbol": "sh600031", "kind": ["announcement", "news", "news"], "limit": 8,
        })
        second = await context.execute(raw_executor, "news_intel", {"symbol": "600031"})

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertTrue(second["_team_shared"])
        self.assertEqual(executions, 1)
        self.assertEqual(context.stats(), {"calls": 2, "unique_calls": 1, "reuses": 1, "slices": 0})

    async def test_team_context_keeps_distinct_news_intel_limits_separate(self):
        """Limit slicing is not implemented yet, so only equivalent limits share."""
        executions = 0

        async def raw_executor(name, args):
            nonlocal executions
            executions += 1
            return {"ok": True, "data": {"name": name, "args": args}, "meta": {}}

        context = ResearchContext()
        await context.execute(raw_executor, "news_intel", {"symbol": "600031", "limit": 8})
        await context.execute(raw_executor, "news_intel", {"symbol": "600031", "limit": 10})

        self.assertEqual(executions, 2)
        self.assertEqual(context.stats(), {"calls": 2, "unique_calls": 2, "reuses": 0, "slices": 0})

    async def test_team_context_slices_wider_news_intel_result(self):
        executions = 0

        async def raw_executor(name, args):
            nonlocal executions
            executions += 1
            return {"ok": True, "data": {"kind": args["kind"], "data_points": [[
                {"title": f"n{i}"} for i in range(args["limit"])
            ]]}, "meta": {}}

        context = ResearchContext()
        await context.execute(raw_executor, "news_intel", {
            "symbol": "600031", "kind": ["news", "announcement"], "limit": 20,
        })
        sliced = await context.execute(raw_executor, "news_intel", {
            "symbol": "600031", "kind": ["announcement", "news"], "limit": 10,
        })

        self.assertEqual(executions, 1)
        self.assertTrue(sliced["_team_shared"])
        self.assertTrue(sliced["_team_slice"])
        self.assertEqual(len(sliced["data"]["data_points"][0]), 10)
        self.assertEqual(context.stats(), {"calls": 2, "unique_calls": 1, "reuses": 1, "slices": 1})

    async def test_team_context_slices_price_only_market_research(self):
        executions = 0
        rows = [{"date": f"2026-08-{day:02d}", "close": day} for day in range(1, 19)]

        async def raw_executor(name, args):
            nonlocal executions
            executions += 1
            return {"ok": True, "data": {
                "market": "A股", "data_points": [rows], "price_metrics": {},
            }, "meta": {}}

        context = ResearchContext()
        await context.execute(raw_executor, "market_research", {
            "symbol": "sh600031", "lookback_days": 120, "focus": ["price"],
        })
        sliced = await context.execute(raw_executor, "market_research", {
            "symbol": "600031", "lookback_days": 60, "focus": ["price"],
        })

        self.assertEqual(executions, 1)
        self.assertTrue(sliced["_team_slice"])
        self.assertEqual(sliced["data"]["lookback_days"], 60)
        self.assertEqual(sliced["data"]["price_metrics"]["latest_close"], 18.0)

    async def test_team_context_slices_wider_evidence_ledger_result(self):
        executions = 0

        async def raw_executor(name, args):
            nonlocal executions
            executions += 1
            return {"ok": True, "data": {"rows": [{"title": str(i)} for i in range(args["limit"])]},
                    "meta": {"rows": args["limit"]}}

        context = ResearchContext()
        await context.execute(raw_executor, "evidence_ledger", {"symbol": "600031", "limit": 20})
        sliced = await context.execute(raw_executor, "evidence_ledger", {"symbol": "600031", "limit": 8})

        self.assertEqual(executions, 1)
        self.assertTrue(sliced["_team_slice"])
        self.assertEqual(len(sliced["data"]["rows"]), 8)
        self.assertEqual(sliced["meta"]["rows"], 8)

    async def test_team_repairs_planner_plan_that_omits_deep_researcher(self):
        expert_calls = []

        async def fake_complete_json(system, user, *, max_tokens=2000):
            return {
                "tasks": [
                    {"agent": "event_scout", "task": "events"},
                    {"agent": "market_analyst", "task": "market"},
                    {"agent": "fundamentals_analyst", "task": "fundamentals"},
                    {"agent": "predictor", "task": "forecast"},
                ]
            }

        async def fake_run_expert_serial(agent_id, task, question, artifact_store, **kwargs):
            expert_calls.append((agent_id, kwargs.get("prior_findings")))
            yield {"type": "agent_findings", "agent": agent_id, "findings": agent_id, "tool_trace": []}

        async def fake_run_agent(agent_id, messages, *, agent_def, state, artifact_store, max_rounds=3, emit_thinking=True):
            state["content"] = "final"
            if False:
                yield {}

        async def fake_artifact_store(kind, title, payload):
            return {"id": "a1", "kind": kind, "title": title, "payload": payload}

        with (
            patch.object(team_mod, "complete_json", fake_complete_json),
            patch.object(team_mod, "_run_expert_serial", fake_run_expert_serial),
            patch.object(team_mod, "run_agent", fake_run_agent),
        ):
            state = {"content": "", "tool_trace": []}
            events = [
                event
                async for event in team_mod.run_team(
                    "预测 600519 未来30天",
                    history=[],
                    state=state,
                    artifact_store=fake_artifact_store,
                    team_members=None,
                )
            ]

        plan = next(event["plan"] for event in events if event.get("phase") == "plan")
        agents = [item["agent"] for item in plan]
        self.assertEqual(len(agents), 5)
        self.assertEqual(agents[-2:], ["deep_researcher", "predictor"])
        self.assertEqual(set(agents), set(team_mod.EXPERT_IDS))
        predictor_prior = next(prior for agent, prior in expert_calls if agent == "predictor")
        self.assertIn("deep_researcher", predictor_prior)

    async def test_graph_and_predictor_handoff_do_not_repeat_source_fetches(self):
        captured = {}

        async def fake_run_agent(agent_id, messages, *, agent_def, state, artifact_store, **kwargs):
            captured[agent_id] = list(agent_def["skills"])
            state["content"] = "完成"
            if False:
                yield {}

        async def fake_artifact_store(kind, title, payload):
            return {"id": f"{kind}_1", "kind": kind, "title": title, "payload": payload}

        predictor_events = []
        with patch.object(team_mod, "run_agent", fake_run_agent):
            for agent_id in ("deep_researcher", "predictor"):
                events = [
                    event
                    async for event in team_mod._run_expert_serial(
                        agent_id,
                        "任务",
                        "研究 600519",
                        fake_artifact_store,
                        prior_findings={"market_analyst": "已有行情证据"},
                        prior_tool_trace=[],
                    )
                ]
                if agent_id == "predictor":
                    predictor_events = events

        self.assertEqual(captured["deep_researcher"], ["evidence_graph"])
        self.assertNotIn("predictor", captured)
        predictor_text = "".join(
            event.get("delta", "") for event in predictor_events
            if event.get("type") == "token"
        )
        self.assertIn("不在聊天阶段重复取数", predictor_text)
        self.assertIn("不代表校准概率", predictor_text)

    def test_graph_fallback_is_explicitly_insufficient_and_context_only(self):
        graph = EvidenceGraph(question="未来30天怎么走")
        evidence_id = graph.add_evidence(
            "akshare", "market_research(600519)", "最新行情", "收盘价来自行情工具"
        )

        team_mod._ensure_minimum_graph_structure(graph, graph.question)

        payload = graph.to_payload()
        claim = next(node for node in payload["nodes"] if node["kind"] == "claim")
        self.assertEqual(claim["status"], "insufficient")
        self.assertEqual(claim["confidence"], 0.0)
        self.assertEqual(payload["edges"][0]["relation"], "context")
        self.assertEqual(payload["edges"][0]["dst"], evidence_id)
        self.assertEqual(payload["stats"]["n_missing"], 1)
