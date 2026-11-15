# EvoMap 接入审计与候选 Gene 诊断

## 结论

`evomap_180_20261112_r4` 不是“完整复现官方 Evolver 主循环”的证据。它确实调用了官方 `@evomap/evolver@1.94.0` 的 GEP 资产、信号、选择器和记忆图组件，但候选 Mutation/Gene 的生成和回测晋升由 Pronoia 的 Python 控制器与模型请求负责。官方包没有直接生成本项目的金融规则候选。

## r4 的事实证据

- 初始 Gene：10 个，位于 `assets/runtime/gep/genes.json`。
- 学习反馈：60 个 `EvolutionEvent`，其中成功 25、失败 35，位于 `assets/runtime/gep/events.jsonl`。
- 学习 Capsule：25 个成功案例，位于 `assets/runtime/gep/capsules.json`。
- MemoryGraph：540 条记录，位于 `assets/runtime/memory_graph.jsonl`。
- Gene 选择：`g0` 的学习/验证/测试预测都包含官方选择器返回的 Gene IDs；baseline 则不注入演化上下文，这是设计上的对照组。
- 候选库：没有 `rounds/*/candidate.json`，因为候选生成前的模式校验每轮都得到空的 `patterns.validated.json`。

## 根因

模型返回的 `counterexamples` 带有解释文本，例如：

```text
evo_cn_300711_093df926c32d（2024-04-11，扭亏但主要靠政府补助，预测down实际down）
```

原校验使用完整字符串去查找 `by_id` 中的纯事件 ID，导致每个模式都被判为没有合格反例，五轮都跳过并记录 `insufficient_repeated_learning_evidence`。这发生在 Mutation 生成之前，所以不是“候选全部验证失败”，而是“候选根本没有生成”。

修复后，校验只从本轮已采样的学习样本中解析事件 ID，保留解释文本但不允许引入新样本；对应回归测试覆盖了带括号说明的反例格式。

## 官方组件边界

当前桥接实际调用：`seal`、`install`、`extract`、`select`、`attempt`、`feedback`、`feedback_batch`、`snapshot`。`@evomap/gep-sdk` 负责 schema 和内容哈希，官方 Evolver 负责信号提取、Gene/Capsule 选择、资产存储和 MemoryGraph。

金融规则的归因、Mutation JSON 生成、候选规则编辑、Pronoia 回测和验证集晋升仍由宿主控制器负责。这是一个“官方 GEP runtime + Pronoia domain evolution controller”的集成，而不是把官方 CLI 的所有运行模式嵌入回测器。

## 修复后的验证

- `backend/tests/test_evomap_experiment.py` 与 `backend/tests/test_evomap_api.py`：17 passed。
- r5 在第 1 轮归因阶段失败：`deepseek-v4-flash` 将 8196 输出预算耗尽在 reasoning，`content` 为空，未生成 `attribution.json`。这不是候选校验失败。
- 已将 `reasoning_effort="none"` 限定到 EvoMap 归因/Mutation 优化器请求；普通预测请求不受影响。对应请求仍写入 `*.request.json` 和 `*.usage.json`。
- r6 使用 `deepseek-v4-flash`、8196 最大输出、5 轮、4 并发、1200 万 Token 上限，已完整结束：5/5 轮均有 `patterns.validated.json`、`candidate.json`、`mutation.json`。
- 验证集晋升：`g3` 为 21/40（52.5%），`g5` 为 23/40（57.5%），最终选择 `g5`；`g1`、`g2`、`g4` 因无验证集准确率增益被拒绝。
- 独立测试集：baseline 35/80（43.75%），g5 35/80（43.75%），`improvement_proven=false`。因此当前结论是“候选生成和验证晋升链路已打通”，但“最终独立测试提升”尚未被证明。
- 完整结果：[evomap_180_20261112_r6/result.json](../backtesting/evomap/evomap_180_20261112_r6/result.json)，锁定版本：[final_lock.json](../backtesting/evomap/evomap_180_20261112_r6/final_lock.json)。
