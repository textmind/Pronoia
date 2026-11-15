# EvoMap Team Full Online 500 回测

## 实验定义

本实验把 `data/self_evolving_500_v2` 的 500 条事件作为一个统一的在线 test population。`learn`、`validation`、`test` 只是准备阶段的文件名，本实验不赋予它们训练集或验证集语义。

执行顺序是冻结的 100 条前缀加新增 400 条事件。前缀来自 `team_full_online_100_v2`，已完成的 baseline/evolution 请求不重复调用；新增事件继续使用原 evolution runtime 和按事件成熟时间释放反馈的机制。

## Pipeline

```text
episode
  -> as-of signal extraction
  -> financial bucket routing
  -> official EvoMap Gene/Capsule selection
  -> Team Full execution
  -> label maturity gate
  -> EvolutionEvent + Memory Graph
  -> probation promotion or rejection
```

分桶信号包括事件类型、增长区间、利润质量、事件前相对基准强弱、市场和预测周期。成功结果可形成 Capsule；失败结果只进入 Event/Memory。官方晋升门槛是至少 3 次真实成功且 0 次失败。

## 结果

| 指标 | Baseline | Evolution |
|---|---:|---:|
| 正确数 | 242/500 | 250/500 |
| 准确率 | 48.4% | 50.0% |
| 覆盖率 | 99.2% | 98.6% |
| 无效/弃答 | 4 | 7 |
| 高置信错误率 | 47.7% | 43.1% |

配对结果：Evolution 独有正确 34 条，Baseline 独有正确 26 条，净提升 `+1.6pp`，配对 95% CI 为 `[-1.2pp, +4.8pp]`。因此点估计有提升，但 `improvement_proven=false`，结论为 `insufficient_evidence`，不能宣称统计意义上的稳定改善。

Evolution 资产共 92 个 Gene、289 个 Capsule、499 个 EvolutionEvent 和 504 条 Memory 记录。

完整机器结果保存在本地运行产物：

```text
backtesting/evomap/team_full_online_500_v1/result.json
```

lho run：`d4ec170a-d9d8-477d-a432-e79ccd63796f`，状态为 `SUCCEEDED`。

## 限制

- `confidence` 是未校准的证据信心，分桶和高置信错误率仅作诊断。
- 汇总 JSON 的 `total_tokens` 字段尚未接通逐轨迹 token 汇总；逐轨迹统计已保留。
- 500 条结果的配对区间仍跨 0，需要更长在线流或更多独立事件继续验证。
