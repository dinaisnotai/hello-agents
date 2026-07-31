# Travel Planning Agent 工程报告：Repair Skills 与 Context Governance

## 1. 背景与目标

本项目是一个以确定性规划为核心的旅行规划 Agent。目标不是让 LLM 直接生成或修改行程，而是在工具调用、候选排序、路线规划、约束验证和质量修复之间建立可观测、可验证的工程闭环。

本轮完成两个能力：

1. Evaluation-Driven Repair Skills：将现有 repair strategy 结构化为可追踪、可统计的技能执行事件。
2. Role-Based Context Governance：为不同 LLM 角色提供最小、可解释、受 token 预算约束的上下文视图。

不变原则：LLM 不直接修改正式 `TripPlan`；repair 必须经过 `PlanMutationSandbox`；`CommitGate` 仍是唯一正式提交来源；Validator 未被放宽。

## 2. 现有调用链

生产路径中的主要链路如下：

```text
User Request
  -> Requirement / Constraint Extraction
  -> Attraction, Weather, Hotel, RAG specialists
  -> Deterministic candidate normalization and planning
  -> Route / budget / walking recalculation
  -> Constraint validation
  -> Experience Evaluator (optional LLM)
  -> RepairController
  -> PlanMutationSandbox
  -> CommitGate
  -> Finalize
```

LangGraph 路径在 `bounded_repair` 节点中复用 sandbox；deterministic quality repair 在 `MultiAgentTripPlanner.run_quality_loop()` 中复用同一提交语义。

## 3. Repair Skill 设计

### 3.1 静态定义与 registry

`RepairSkillDefinition` 是静态能力描述，不包含可执行代码。每个定义包括稳定 `skill_id`、版本、关联 `RepairStrategy`、支持的 issue type、适用 scope、所需上下文字段和启用状态。

`RepairSkillRegistry` 覆盖当前所有 `RepairStrategy`。真实修改逻辑仍保留在 `RepairController` 的 handler 内，registry 只负责能力查找与适用性校验，不提供动态插件或在线自进化能力。

示例：

| Strategy | Skill ID |
| --- | --- |
| `ADD_NEARBY_COMPLEMENTARY_POI` | `repair.add_nearby_complementary_poi.v1` |
| `ADD_WEATHER_BACKUP` | `repair.add_weather_backup.v1` |
| `REMOVE_DUPLICATE` | `repair.remove_duplicate.v1` |
| `REDUCE_COST` | `repair.reduce_cost.v1` |
| `RUN_CONSTRAINT_REPAIR` | `repair.run_constraint_repair.v1` |

### 3.2 执行事件

`RepairSkillExecution` 关联已有 `RepairAttempt`，但不保存完整行程或 prompt。它记录：

- execution / attempt / run ID；
- skill ID、版本、strategy、issue fingerprint、severity、scope；
- selected、mutation_succeeded、committed、quality_improved；
- CommitGate decision、rollback reason、error；
- repair 前后质量、hard violation、warning 与耗时；
- deterministic 或 LangGraph pipeline mode，offline 或 live evaluation mode。

这几个状态刻意分离。例如 handler 可以成功修改 candidate，但 candidate 仍可能因新增 hard violation 而 rollback；mutation success 不等于 commit。

### 3.3 存储与报告

执行事件写入本地 JSONL artifact：

```text
backend/data/repair_skill_executions.jsonl
```

该文件已被 Git 忽略，避免把本地运行历史、可再生成统计和用户运行数据提交到仓库。

离线报告命令：

```powershell
python scripts/report_repair_skills.py
python scripts/report_repair_skills.py --json
python scripts/report_repair_skills.py --issue-type underfilled_day
```

报告按 skill 聚合 selection、mutation success、commit、rollback、failure、quality delta、hard/warning delta、耗时、rollback reason、issue type、scope 与 pipeline mode。

## 4. Context Governance 设计

### 4.1 三类角色

`ContextRole` 包含：

- `EXPERIENCE_EVALUATOR`
- `REPAIR_STRATEGIST`
- `FINAL_EXPLAINER`

`ContextPolicy` 为每个角色定义 required / optional / forbidden sections、token 预算、候选/RAG/repair history 上限和优先级。

### 4.2 分层规则

L0 永远保留：用户请求摘要、hard constraints、must-visit、预算、步行限制、当前计划版本、核心行程摘要和已有 hard violations。

L1 按角色注入：

- Experience Evaluator：每日行程、步行/交通、天气、有限候选、有限 repair history、有限 RAG evidence。
- Repair Strategist：当前 issue、局部候选、历史 attempts、适用 skill 与聚合 metrics。
- Final Explainer：最终行程、warnings、天气备选、必要 evidence；不包含候选拒绝明细和完整 repair trace。

L2 默认禁止：完整候选池、地图 API 原始响应、完整路线矩阵、全量 score breakdown、checkpoint 原始状态和调试日志。

### 4.3 Token 裁剪

项目没有可靠 provider tokenizer 时，使用封装的稳定字符估算器。裁剪顺序是 repair history、RAG evidence、候选、详细天气等 L1 内容；L0 不会被删除。

如果 L0 自身超出 policy budget，context 允许超预算，并记录 `required_context_over_budget`，而不是丢弃 hard constraints。

### 4.4 已接入节点

`PlannerAgent.review_plan()` 的 Experience Evaluator 已使用 `RoleContextBuilder` 生成 prompt payload，并将 `ContextTrace` 写入 `TripPlan` 和 planning observability trace。

当前仓库没有独立的 Repair Strategist LLM 节点：repair strategy 是 Experience Evaluator 的结构化输出，随后由确定性 registry、RepairController 和 CommitGate 验证。`REPAIR_STRATEGIST` context 已准备好可用 skill/metrics 摘要，供未来拆分该节点时复用。

当前 Final Explainer 是确定性 finalize，而不是 LLM 调用。系统记录它的治理上下文视图和 trace，但没有为了形式上的“角色接入”增加未经验证的 LLM 输出路径。

## 5. 可观测性与安全边界

新增 `ContextTrace` 记录 role、policy version、计划版本、输入 token 估算、included/omitted/truncated sections、artifact reference 和 L0 超预算标记。

原有 `PlanningRunTrace` 继续保留候选、路线、约束、portfolio 和 quality-gate 信息，并新增 context traces。Repair Skill execution 不写入完整计划快照，也不记录 API key、完整 prompt 或第三方原始响应。

## 6. 关键文件

- `backend/app/models/repair_skills.py`
- `backend/app/services/repair_skill_registry.py`
- `backend/app/services/repair_skill_metrics.py`
- `backend/app/services/plan_mutation_sandbox.py`
- `backend/app/models/context_governance.py`
- `backend/app/services/context_governance.py`
- `backend/app/agents/planner_agent.py`
- `backend/app/services/planning_observability.py`
- `backend/scripts/report_repair_skills.py`

## 7. 验证结果

本轮验证结果：

| 检查 | 结果 |
| --- | --- |
| 全量单元测试 | 151 passed |
| Deterministic evaluation | 15/15 passed |
| LangGraph evaluation | 15/15 passed |
| Governance disabled evaluation | 15/15 passed |
| Governance enabled evaluation | 15/15 passed |
| `python -m compileall app -q` | passed |
| `git diff --check` | passed |
| Repair Skill report JSON/text smoke test | passed |

离线 evaluation 未调用 LLM，因此 enabled/disabled 对比证明了规划质量与硬约束结果不退化，但不代表真实 provider token 已下降。真实 token usage 字段已在 `ContextTrace` 预留；目前记录稳定的估算 token。

## 8. 已知限制与下一步

1. Repair Skill metrics 是离线辅助信号，不能覆盖当前请求的 applicability check、Validator 或 CommitGate。
2. JSONL 是第一版本地 artifact store；多进程生产部署可替换为 SQLite 或集中式事件存储。
3. 当前 LLM provider adapter 只返回文本，不能获取真实 input token usage；后续可在 adapter 支持 usage 时回填 trace。
4. 如未来新增独立 Repair Strategist 或 Final Explainer LLM，必须复用 `RoleContextBuilder`，保持 structured output contract 不变，并继续禁止 LLM 直接 commit 或修改正式计划。
