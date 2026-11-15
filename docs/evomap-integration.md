# Pronoia 与官方 EvoMap 的深度融合

> 接入边界审计见 [EvoMap 接入审计与候选 Gene 诊断](evomap-diagnosis-20261112.md)。这里的集成是“官方 Evolver v2 周期引擎 + Pronoia 金融领域实验控制器”：官方包负责协议资产、信号、选择、Mutation、完整周期、人格、经验库和记忆图；Pronoia 负责封存数据、金融任务执行、候选回测和预注册晋升。

已打通真实任务 → 不可变语料 → 信号 → Gene 编译及选择 → Pronoia 预测 → 独立评分 → Capsule / EvolutionEvent / 记忆反馈 → 下一轮候选，并接入回测中心「05 自进化」。协议接通、测试通过或生成曲线，不等于已经证明准确率稳定提高。

## 实际使用的组件

`integrations/evomap` 独立 Node 进程只从官方根导出调用固定版本 `@evomap/evolver@2.0.51` 与 `@evomap/evolver-core@2.0.51`。它使用官方 `LocalJsonlProvider`、`Ingestor`、`CycleEngine`、`runEvolutionCycle`、选择器、人格存储、经验库、记忆图和配对基准。没有导入包内私有路径，也不执行 Gene 中的 shell 字符串、安装编辑器 hooks 或自动发布资产。

本地模式不连接 EvoMap Hub，不需要 Hub 身份，不发布资产，也不参加 Worker Pool 或 Evolution Circle。v2 的 POSIX `EventStore` 要求由管理员预配置独立 UID 的审计服务；本地单机实验改为把官方 `Ingestor` 接到追加写、`fsync` 的本地审计存储，并由 Python 进程锁串行化每个实验目录。这个边界写入实验清单，不能与 Hub 的多主体审计保证混为一谈。

Python 接口位于 `backend/app/evolution`：

| 模块 | 职责 |
| --- | --- |
| `gep.py` | 串行保护资产更新，调用真实官方依赖 |
| `policy.py` | 将结构化金融规则编译成不可变 Gene，验证基于学习证据的修改，调用官方选择器 |
| `runtime.py` | 任务范围内注入 Gene 与历史案例，支持结构化预测和真实完整团队，任务结束后清除 |
| `corpus.py` | 输入、预测、使用的 Gene、轨迹与模型版本；学习反馈单独保存并按哈希关联 |
| `prepare.py` | 新公开源、真实价格锚点、按时间和公司隔离、不可变数据清单 |
| `experiment.py` | 冻结模型、断点续跑、错误归因、候选生成、回测及晋升、最终测试锁 |
| `score.py` | 独立 Oracle 评分进程，不向优化器提供验证/测试逐条答案 |
| `meter.py` | 所有嵌套模型请求发送前预留预算，保存用量；无计费信息时保守计入 |
| `report.py` | 实际曲线、PNG / SVG / CSV / JSON 与独立 HTML 报告 |
| `service.py` | 有界后台进程、实验目录、只读产物 API、人工激活与回滚登记 |

依赖使用原许可证，与前端构建分离；没有将引擎源文件复制改写进应用。上游依据：[EvoMap/evolver](https://github.com/EvoMap/evolver)。安装锁文件与实现哈希共同冻结实际执行版本。

## 任务、Gene、语料与反馈的关系

1. 创建实验时复制当前已验证的默认模型连接，并以加密引用冻结密钥。显示名称、模型、参数与数据哈希进入清单；原始 Key 不写入任务、日志或 API。
2. 初始10条结构化金融规则各编译成一个官方 Gene，内容发生变化时创建新的 Gene ID 与资产哈希，保留库的父版本。
3. 每项任务从事前输入提取信号；官方选择器最多选择4条 Gene。Capsule 只有来自学习集、结果在当前事件前已成熟、且并非当前事件本身时，才可作为历史案例。
4. 选中的策略进入实际预测请求。`team_prompt` 是结构化单次预测，`team_full` 调用真实规划、专家、综合和复核流程；普通研究未启用实验上下文时保持原行为。
5. 每轮候选在验证前生成冻结周期回执，绑定实验、数据、模型、任务、人格、父库、所选 Gene / Capsule 和 Mutation。验证完成后才允许提交；修改回执或任一身份会被拒绝。
6. 保存不可变任务记录，学习结果成熟后写入独立反馈。完整官方周期固化 Mutation、Capsule、EvolutionEvent、MemoryGraph、Experience 和人格状态。单例对错不当作 Gene 的因果贡献。
7. 优化器只看学习错误及成功对照、规则目录与验证汇总。模式须有至少3个不同日期、3家公司及成功反例；每轮最多3次结构化规则编辑，并限制修复尝试。
8. 候选必须在相同验证样本上提高正确数量，且满足覆盖率、审计与输入成本要求，才能晋升。只有晋升候选再用固定50条学习样本刷新经验，避免为每个淘汰候选重复支付学习成本。
9. 所有候选完成后锁定版本、人格、代码、数据及历史哈希。基线和胜出版本在同一100条封存测试集上各重复两次，顺序由预注册种子交错，再由独立进程评分；锁定后禁止新的优化器请求。

冻结请求路由后再发送模型；中断恢复复用路由与已保存预测，不重新抽取案例或改选 Gene。未保存的在途请求可能需要重试，其预留费用保留在预算中。

## 本批数据与实验约束

正式对照使用新准备的学习150、验证60、测试100。学习为2024年，验证为2025年，测试为2026年7月至10月。不同分区公司互斥，并排除仓库中既有数据集出现过的事件、事件组、公司以及公司/日期组合。源数据、价格及内容哈希保留，Oracle 标签另存且不作为预测输入。已有实验的旧测试集不作为新的未见测试集。

仍有数据局限：历史模型预训练是否包含这些公告无法证明；源修订时间缺乏独立时间戳；历史复权价格为当前下载的序列。这是历史事前信息回放，不等同真实前瞻实盘。

主指标为固定分母方向准确率。缺失、无效和弃权算未命中，不移除分母。重复推理只降低运行噪声，独立样本数仍为100。改善必须同时满足：胜出版本准确率更高、至少5个14天块且配对块自举95%区间下界大于0、两臂都无缺失、覆盖率下降不超过2个百分点、审计违规率不高于10%、测试 Token 不超过基线的1.5倍。固定看涨/看跌只作独立参照，不混入主改善判定。

正式真实实验为3轮候选、并发2、5000万 Token 硬上限，全程 `team_full`，最终每臂重复2次。持久任务契约另设总墙钟、阶段超时、重试和日志上限。无服务商用量时按文本字节、输出上限和 SDK 重试次数保守计费，预算不足在请求前停止。页面关闭不影响进程；操作系统重启后可从检查点恢复。

## 使用与产物

### 无感聊天封存与在线回测

普通聊天和团队聊天完成后会异步写入 `backtesting/evomap/online/episodes/`，不会阻塞聊天响应。后台使用官方 EvoMap v2 的 signals、Gene/Capsule selector、EvolutionEvent 和本地 audit store；新策略先进入 shadow，重复出现三次后才成为 active。候选策略只在 `active` 状态参与下一轮选择，聊天中断、重复提交和服务重启不会改变已落盘 episode 的内容。

查看在线队列：

```sh
curl http://127.0.0.1:8000/api/evolution/online/status
```

下一轮 100 条严格在线 team_full 对照使用同一事件顺序和模型连接：

```sh
PYTHONPATH=backend backend/.venv/bin/python -m app.evolution.online_backtest \
  --dataset data/self_evolving_500_v2 \
  --out backtesting/evomap/team_full_online_100_v1
```

结果写入 `baseline/`、`evolution/` 和 `result.json`。进化组只在下一条事件的 as-of 时间已经到达时读取上一条事件的成熟标签；当前事件的标签不会参与当前 Gene 选择。报告同时给出固定分母准确率、覆盖率、配对差值、bootstrap 区间及 `improvement_proven`。

首次安装：

```sh
cd integrations/evomap
npm ci --ignore-scripts
```

前端入口 `/backtest/evolution`。新建实验只选择已经冻结的数据集，使用当前已验证的默认模型；缺失配置沿用409和设置链接。页面可查看实验状态、实际准确率、Gene 版本、任务语料与学习反馈。任务范围内的策略不会自动替换普通研究的全局默认规则。

可复现的 CLI：

```sh
PYTHONPATH=backend backend/.venv/bin/python -m app.evolution.experiment \
  --dataset data/evomap_310_20261112_v2 \
  --out backtesting/evomap/evomap_310_20261112_v2_r1 \
  --rounds 3 --concurrency 2 --max-tokens 50000000 \
  --mode team_full --repetitions 2 --refresh-count 50
```

后台 API：`GET /api/evolution/datasets`、`GET/POST /api/evolution/runs`、`GET /api/evolution/runs/{id}`、`POST /api/evolution/runs/{id}/resume`、`POST /api/evolution/runs/{id}/cancel`、`POST /api/evolution/runs/{id}/activate`、`GET /api/evolution/policy/active` 和 `POST /api/evolution/policy/rollback`。创建/恢复返回202；相同任务锁定执行，已有实验运行时不并发启动另一轮昂贵实验。通过全部独立测试门槛也不会自动改变线上策略，必须人工激活。

每个实验目录保存：`manifest.json`、冻结模型私有引用、请求用量、任务请求及预测检查点、corpus、官方 GEP 资产/记忆、每轮归因/候选/拒绝记录、`history.json`、`final_lock.json`、`result.json` 和曲线文件。私有模型、日志、Oracle 及逐条验证评分不通过下载 API 提供。

最终报告以实际结果说明是否改善。若置信区间跨零、样本不足或不优于固定方向基线，必须明确写明尚未证明稳定提升。

## 多轮候选与修复续跑

候选会收到既往候选及验证汇总；相同规则、无实际变化的修改不再反复测量，防止把随机回复波动误当作规则改善。演化反馈按实验、轮次和候选版本分别编号；同一轮重试幂等，不同轮次不覆盖彼此。

控制器修复时允许显式 `--baseline-from <旧实验目录>` 复用已完成的结构化基线。只允许已停止、尚未封存测试集的来源，且数据、冻结模型和连接、事前输入、实际预测/传输/规则/预算代码保持一致；来源源码归档与不可变 corpus 逐条核验。仅导入基线预测，不导入候选或测试预测。旧请求的全部计费及在途预留一起计入新任务预算。新实验有独立清单与导入来源，旧实验不改写为成功。
