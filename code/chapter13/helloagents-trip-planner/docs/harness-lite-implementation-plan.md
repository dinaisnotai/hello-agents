# 旅行规划项目 Harness-lite 实施方案

状态：方案，尚未实施。范围：当前旅行规划项目；表格中的“两者”如还指另一项目，其接入另行评估。

## 结论

可行，而且适合当前项目，但要**复用现有 LangGraph、HelloAgents 工具接口和评测器**。最有价值的交付不是新增一个总控框架，而是让一次规划能回答四个问题：哪个步骤失败、为什么降级、恢复后是否重复调用、修改后真实行程质量是否提高。

建议顺序：**统一事件与关联 ID → 固定故障评测集 → 工具边界的错误分类与重试 → 真实中断恢复 → 小型 registry/policy → 故障注入 → 可选导出**。其中前四项值得做；独立通用 Runtime、全量 OTLP/LangSmith 和 Docker/E2B 暂缓。

## 当前代码盘点

| 能力 | 仓库现状 | 缺口 |
| --- | --- | --- |
| 运行记录 | `PlanningRunTrace` 有 `run_id`、候选景点、路线和质量摘要；LangGraph `PlanningState.trace` 有节点记录；repair skill 另存 JSONL | 缺统一 `step_id`、错误类别、每次工具调用的耗时和重试次数；最终摘要在日志或计划里，不能按步骤查询 |
| 持久化 | 会话/版本在 `trip_sessions.db`；LangGraph 使用单独的 SQLite `SqliteSaver` | checkpoint、会话版本和 trace 未用稳定关联键串起来；`session_id` 在规划结束后才产生 |
| 工具注册 | 三个查询 Agent 各建一个 HelloAgents `ToolRegistry`，分别注册地图工具 | 没有一份元数据说明工具权限、超时、是否可重试；不需要替换 HelloAgents 原 registry |
| 超时 | 高德 HTTP 超时默认 8 秒；LiteAPI 18 秒；Embedding 20 秒 | 多数异常在服务方法内直接转成空结果/估值，调用方无法区分超时、限流、结构错误与真实无结果；暂无统一重试上限 |
| 恢复 | `LangGraphTripWorkflow` 已有 SQLite checkpointer、`run(thread_id=...)` 和 `resume(thread_id)` | 现有测试只对已完成的图调用 `resume()`；没有进程重启和节点中途失败后的恢复验收；API 没有稳定的运行状态/恢复入口 |
| 评测 | `backend/evals/travel_scenarios.json` 有 15 个场景，已有自动 grader 和离线运行脚本 | 缺来自真实投诉的冻结案例、版本化 baseline、按错误类型的指标和每个案例的 trace 关联 |
| 约束/安全 | 已有景点身份、餐厅类型、预算/步行/时间等确定性规则和有界修复 | 需把“谁能调用什么工具、哪些输出能进入行程、何时允许降级”集中成可审计的执行规则；保留现有业务规则 |

以上是**代码现状**，不是已实现的 Harness-lite。尤其“有 checkpointer”不等于“已经通过进程中断恢复测试”。

## 最小架构与数据契约

新增 `backend/app/harness/`，只放执行边界，不搬动规划算法：

```text
API / conversation
  └─ RunContext(run_id, session_id?, request_id?, graph_thread_id)
       ├─ EventSink ── SQLite harness_runs / harness_events
       ├─ ToolCatalog + ToolExecutor ── 现有 Amap/LiteAPI/Embedding 服务
       ├─ Policy ── 工具权限、参数边界、结果来源和降级规则
       └─ EvalHook ── 复用 app/evaluation 的 case/grader
LangGraph ── 继续使用现有 SqliteSaver（图状态），不另造 checkpoint 引擎
```

`run_id` 表示一次生成或一次对话重规划；`session_id` 表示用户会话；`graph_thread_id` 表示可恢复的图执行。三者不混用。首次规划在会话生成前创建 `run_id`，保存会话后补写映射。后续每轮对话生成新 `run_id`，关联相同 `session_id` 和具体 `plan_version`。步骤重试保留相同逻辑 `step_id`，用 `attempt_no` 区分调用；恢复时保留原 `run_id`，新建 `resume_attempt_id`。不能把可重复执行的节点序号当永久唯一 ID。

最小事件字段：`schema_version`、`event_id`、`run_id`、`step_id`、`parent_step_id?`、`attempt_no`、`kind`（`model/tool/validator/repair/checkpoint/eval`）、`name`、`status`、`error_type?`、`duration_ms?`、`timestamp`、`attributes`。例如 `tool.end` 只记录 `tool=amap.route`、耗时、HTTP 状态类别、返回数量和是否使用估值；不存 API key、酒店报价原始响应、用户自由文本、完整 prompt 或完整计划。确需排查输入时记录有盐摘要或受控短摘要，并设置大小上限和保留期。`harness_events` 按 `(run_id, timestamp)` 索引，运行表记录状态、关联 ID、最后 checkpoint 信息和版本号。事件写入失败不得使一次合法旅行规划失败，但必须有可见告警。

`ToolCatalog` 是**项目自己的工具元数据表**，不是第二套让 LLM 任意发现和执行工具的系统。每项写明 `name`、输入/输出类型、`allowed_callers`、`read_only`、`idempotent`、`timeout`、`max_attempts`、`fallback` 和结果可信级别。首批只覆盖 `amap.poi_search`、`amap.poi_detail`、`amap.weather`、`amap.route`、`liteapi.hotel_quote`；Embedding/模型调用先只埋点，确认各自 SDK 的超时与重试行为后再接入。三个 Agent 继续使用现有 HelloAgents `ToolRegistry`，其适配器委托给受控执行器。避免每个 Agent 都能调用全部工具。

事件脱敏不等于 checkpoint 脱敏：当前图状态含原始请求、候选 POI 和计划 JSON。恢复测试前须核对 checkpoint 文件的访问权限、保留期及删除策略；会话消息和计划版本同样需要按 `session_id` 删除。不要把 checkpoint SQLite 文件当成可公开的调试产物。

`Policy` 分两层：调用前只允许已注册、只读工具及合规参数（城市、半径、返回条数、最大次数）；调用后检查来源和类型（医院/充电站不能作为景点或餐厅，酒店报价须有来源与日期）。景点身份、餐饮、步行、营业时间和修复接受规则继续由现有业务校验器负责；Policy 调用这些结果，不复制一套平行规则。拒绝时发 `policy.reject`，不把拒绝伪装为空搜索结果。

## 分阶段交付

| 阶段 | 优先级 | 具体改动 | 可验收结果 | 粗估 |
| --- | --- | --- | --- | --- |
| 0. 冻结基线 | P0 | 跑现有离线评测；加入真实故障案例并保存环境、代码版本、配置快照和报告 | 可一键比较同一数据集的新旧结果；先记录现状，不能预设已有用例全过 | 0.5–1 天 |
| 1. 统一事件 | P0 | 新建事件 schema、SQLite store、`RunContext`；在 API、LangGraph 节点、工具边界、validator 和 repair 处埋点；补齐 `run_id ↔ session_id ↔ plan_version` | 任取一次请求，可查完整节点顺序、耗时、降级来源、修复前后结论；敏感内容不落事件表 | 1–2 天 |
| 2. 超时与分类重试 | P0 | 在服务边界引入 `NetworkTimeout/TransportError/RateLimit/ProviderError/SchemaError/ConstraintError/PolicyError`；按工具配置重试，并记录尝试次数 | 超时/429/指定 5xx 最多有限重试；4xx 参数错误、schema、约束、policy 不重试；现有估值降级仍可用且来源可见 | 1–2 天 |
| 3. 评测接入 | P0 | 扩展现有 grader 和冻结场景；每例写 `case_id/dataset_version/run_id`，输出逐项差异与 trace 引用 | 能阻止“景区内部景点重复、充电站当晚餐、午餐后直奔晚餐、步行超限、酒店位置不合理”等回归 | 1–2 天 |
| 4. 恢复 | P1 | 复用现有 checkpointer；增加运行状态与恢复入口，区分 `not_found/running/failed/completed`；对读工具加结果缓存/幂等键 | 模拟进程结束后用同一 thread 恢复，已完成节点不重复，当前失败节点至多重新调用；仅持久化一次最终计划版本 | 1–2 天 |
| 5. 小型工具目录与策略 | P1 | 将三 Agent 现有工具适配器接到目录/执行器；保留现有业务校验器 | 无注册权限不能调用；每个工具有超时、重试、回退和来源规则；不改变景点选择算法 | 1–2 天 |
| 6. 故障注入 | P2 | 测试环境定点注入超时、空结果、非法 JSON、恢复中断；固定随机种子 | 同一故障可复现，验证错误分类、降级、恢复及 trace；不需要生产环境随机注入 | 0.5–1 天 |
| 7. 导出 | P2 | 先稳定内部事件 schema，再做可关闭的 OTLP/LangSmith exporter | 关闭导出时零外部依赖，导出失败不影响规划；映射不暴露敏感原文 | 0.5–1 天 |

粗估是顺利情况下的开发量，**不是交付承诺**；跨会话恢复、并发 SQLite 写入及线上模型调用可能增加时间。先完成阶段 0–3，得到可量化收益，再决定阶段 4–5 的范围。独立 `ContextBuilder` 和通用 `Runtime` 暂不抽象；现有 `context_governance.py` 已承担上下文裁剪，LangGraph 已承担编排。只有第二个项目确实复用同一执行契约时才抽公共包。

## 超时、重试与恢复的关键约束

- 重试只发生在**只读、可幂等**的外部查询；指数退避加抖动，单次工具调用设置总预算，避免每段路线多次重试使整个请求卡住。不能简单地对所有 `Exception` 重试。高德 API 返回 `status=0` 需要按 `infocode` 分类，不能一律当网络故障。
- “无搜索结果”和“上游故障后回退为空”必须有不同状态。服务可以继续返回安全估值，但事件要有 `source=estimated`、`fallback_reason`，并由质量门决定能否称为可执行。
- HTTP 客户端自己的隐式重试、Harness 重试、LangGraph 节点重试不能叠加成乘法。每个工具只有一个重试责任点；初期不启用图节点层重试。
- checkpoint 记录**图状态**，事件记录**执行经过**，会话版本记录**用户可见结果**。恢复前查看 `graph.get_state(config).next` 和运行状态；已完成图返回原结果，不能重新 `run(request, thread_id=同值)`；失败节点可重跑，故工具应是只读且缓存键包含标准化参数、数据来源和合理有效期。
- 特别关注并行的景点/酒店/天气节点、已有 `PlanningState.trace` 的列表累加器和 repair 的版本记录。恢复后不能把同一修复步骤追加两次，也不能把一次对话生成两个 `trip_plan_versions`。会话保存与 checkpoint 在两个 SQLite 文件中，无法天然跨库原子提交；用 `(run_id, final_version)` 唯一键和“已提交则返回”来消除重复写入。
- 当前依赖 `langgraph>=0.2,<1.0`，实施时按**仓库实际安装版本**验证恢复和持久化参数。新版官方文档描述的选项不能未经验证直接照搬到当前版本。

## 评测数据与发布门槛

现有 15 个场景保留作为回归层，新增至少 8 个来自用户实际反馈的冻结案例：圆明园内部区域重复、医院作景点、充电站作晚餐、午饭后过早晚餐、历史偏好但 balanced 太稀、高预算自然/美食/博物馆覆盖、酒店远离主路线、混合交通却步行超限。每例注明输入、期待行为、严重程度和允许降级条件；对于路线与报价这类会变化的数据，离线测试固定工具响应，不把实时价格或天气写成固定答案。

自动 grader 优先检查确定性事实：非法 POI 数、同一景区重复数、用餐可见性、各偏好有安排、营业时间、日步行上限、预算来源、每天活动/休息逻辑。体验项另设人工审阅小样本，LLM 审查只能补充信号，不能覆盖确定性失败。比较新旧版本时逐例列出新增失败、已修复失败、执行时间、工具失败率、重试次数、降级率和恢复重复调用数。P0 发布门槛：新增硬约束失败数为 0、上述真实故障案例通过、trace 缺失率为 0、敏感原文落库为 0；体验分数需结合人工样本再定阈值，避免简单数据集虚高。

## 不建议现在做的内容

- **通用 Harness Runtime 重写**：目前只有旅行规划一条主要图，抽独立调度器会和 LangGraph 重叠；先用 `RunContext + EventSink + ToolExecutor` 三个薄模块。
- **把所有工具塞进一个 LLM ToolRegistry**：会扩大模型权限；统一的是元数据和执行策略，Agent 仍领取各自工具子集。
- **直接接 OTLP/LangSmith**：先稳定自己的 event schema 和脱敏策略，再加 exporter。它们不能替代产品质量评测。
- **Docker / E2B sandbox**：当前工具是地图、酒店和 Embedding 的受控只读查询，没有模型执行任意代码的需求。

## 建议先做的第一个 PR

只交付阶段 0–1：一个 `harness_events` SQLite 表、`RunContext`、事件 schema、关键节点/工具/validator/repair 埋点，以及 8 个真实故障案例的评测规格。先通过一次真实北京行程验证 `run_id → 工具失败 → 回退 → 质量门 → 会话版本` 能串起来，再继续改重试和恢复。这样能从用户看到的坏结果倒查原因，也能在后续每项功能调整时判断究竟变好还是变差。

参考：[LangGraph Checkpointers 官方文档](https://docs.langchain.com/oss/python/langgraph/checkpointers)、[LangGraph Persistence 官方文档](https://docs.langchain.com/oss/python/langgraph/persistence)。
