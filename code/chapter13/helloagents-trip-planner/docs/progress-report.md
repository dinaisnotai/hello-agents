# Travel Planning Agent 项目进度报告

更新时间：2026-07-30

## 1. 项目目标

这是一个 Constraint-aware、Tool-using Travel Planning Agent。目标不是生成一段普通旅游攻略，而是展示 Agent 工程中可验证的规划闭环：结构化需求抽取、工具调用、确定性规划、约束校验、可观测性、离线评测、有限修复，以及 Agent harness 的工程化边界。

系统把 LLM 放在意图理解、专家信息与软体验评审的位置；路线、预算、约束和关键修复由确定性组件负责，避免把不可验证的自然语言输出直接当作行程事实。

## 2. 当前生产 Pipeline

默认生产入口使用 LangGraph。实际状态图定义在 `app/workflows/langgraph_trip_workflow.py`：

```text
User Request
  -> parse_intent
  -> constraint_extractor
  -> [attraction | weather | hotel | rag] specialists（并行）
  -> build_draft
  -> deterministic_planning
       -> candidate normalization / metadata enrichment
       -> POI identity resolution
       -> accommodation selection
       -> attraction scoring
       -> portfolio-aware spatial itinerary planning
       -> route evaluation + legacy/general constraint validation
       -> deterministic reviewer
       -> quality gate + bounded repair
  -> validate_constraints
  -> bounded_repair（条件分支，受上限约束）
  -> soft_review
  -> finalize
  -> TripPlan / observability trace / best-effort response
```

`MultiAgentTripPlanner` 也是 deterministic 和 legacy 路径的核心。它负责候选覆盖、路线聚类、预算估算、硬约束校验与最终 `TripPlan` 构建；LangGraph 负责节点编排、checkpoint 与恢复。

## 3. 已完成能力

### Observability / tracing

- 原始问题：候选、路线、约束失败和 repair 的因果关系不可追踪。
- 实现：每次规划生成结构化 `PlanningRunTrace`，附加到 `TripPlan.observability_trace` 并输出单行 JSON；记录需求、候选评分、筛选原因、日程、约束、quality gate、portfolio 指标和 repair 历史。
- 关键文件：`app/models/observability.py`、`app/services/planning_observability.py`。
- 当前效果：可以从一次运行中查看候选、最终选点、拒绝原因和质量门决策；部分历史 trace 的阶段粒度仍需继续校准。

### Evaluation Harness

- 原始问题：仅靠单元测试无法判断行程是否满足步行、预算、偏好、节奏与多样性。
- 实现：数据驱动 evaluator 独立检查 walking、end time、budget、must-visit、avoid hiking、transport、pace、diversity、preference、family 与 portfolio 指标。
- 关键文件：`app/evaluation/harness.py`、`app/evaluation/schemas.py`、`evals/travel_scenarios.json`、`scripts/run_evaluation.py`。
- 当前效果：支持 deterministic 与 LangGraph 离线回归；自动通过不等价于真实用户体验通过。

### POIIdentityResolver

- 原始问题：别名、父子 POI 和跨日重复会产生多次“游览同一实体”。
- 实现：按 provider id、parent id、名称和坐标生成 canonical `visit_key`；父景区默认作为展示实体，用户明确指定子景点时才保留子 POI。
- 关键文件：`app/services/poi_identity_resolver.py`。
- 当前效果：可合并父子与别名，跨日去重；真实供应商 parent 数据不完整时仍需持续验证代表实体质量。

### AccommodationSelector

- 原始问题：住宿仅按搜索顺序使用，预算、档位与景点可达性未共同考虑，且非住宿 POI 曾被伪装成“推荐酒店”。
- 实现：在真实住宿候选中结合档位、预算、核心景点可达性选择；无可验证住宿 POI 时输出“待确认酒店”，不伪造路线锚点。
- 关键文件：`app/services/accommodation_selector.py`、`app/agents/trip_planner_agent.py`。
- 当前效果：降低住宿成本和交通锚点错误；真实酒店检索质量仍依赖地图数据。

### Core landmark coverage 与 portfolio metrics

- 原始问题：偏好提升容易使小众同类 POI 挤掉城市代表性景点。
- 实现：候选标注 core / major / complementary / niche；空间选择加入 core 覆盖、major 配额、niche ratio 与类别饱和控制；trace/evaluation 输出 portfolio metrics。
- 关键文件：`app/services/poi_metadata_service.py`、`app/services/spatial_planner.py`、`app/services/planning_observability.py`。
- 当前效果：首次访问场景更倾向保留城市代表性；偏好校准与最终组合一致性仍是当前重点。

### Quality Gate、Experience Evaluator 与 Repair Controller

- 原始问题：软体验问题、硬约束问题和 LLM reviewer 结果混杂，可能导致重复 repair 或无可用 degraded 响应。
- 实现：结构化 `ExperienceEvaluation` 区分 blocking / non-blocking issue；严格 RepairStrategy、issue fingerprint、repair history、mutation scope、事务式回滚和有限 repair loop。
- 关键文件：`app/models/quality.py`、`app/services/itinerary_quality.py`、`app/services/repair_controller.py`、`app/agents/planner_agent.py`。
- 当前效果：硬约束保持不放宽；软问题修复失败时保留 warning 和 best-effort `TripPlan`，不会返回空响应。

### Regression tests 与用户提示清洗

- 原始问题：真实案例暴露了天气 repair、低价值补点、父子 POI、酒店候选和内部告警泄漏等问题。
- 实现：增加 repair、portfolio、identity、accommodation、candidate acceptance、warning curation 回归测试；用户 warning 仅保留与最终主行程相关的可执行中文信息，内部诊断留在 trace。
- 关键文件：`tests/test_quality_gate_repair.py`、`tests/test_portfolio_policy.py`、`tests/test_candidate_acceptance_policy.py`、`app/services/user_warning_service.py`。
- 当前效果：可避免部分已删除 POI、英文 evaluator 原文和“已合并”诊断直接展示给用户。

## 4. Evaluation-driven 改进过程

初始离线评测为 4/7，通过 trace 定位到上海美食候选元数据、亲子组合多样性和紧预算住宿成本三个问题；局部修改后达到 7/7。随后扩展 portfolio、quality gate 与 repair 场景，目前离线数据集为 15 个案例。

改进模式是：先用 trace 定位问题所在层（candidate generation、ranking、spatial planning、budget、state/repair），再做局部确定性修改并运行 evaluator 与单元测试，而不是只调 prompt 或改变测试期望。

当前自动 evaluation 已通过，但这只证明覆盖到的场景满足既定 oracle。真实地图数据、真实偏好表达和人工“城市代表性/体验层次”判断仍可能暴露质量问题，不能把通过率等同于产品质量。

## 5. 真实案例中发现的问题

- 空白日、单日利用率不足与路线可行性之间仍需要更精细的平衡。
- repair 处理软 warning 时可能影响主计划，因此已加入 mutation scope 和 rollback，但真实案例仍需持续校准。
- 高分 core/major 候选可能在后续补点或 repair 阶段被低分 niche POI 替代；当前已开始引入 CandidateAcceptancePolicy，覆盖完整性仍需证明。
- 普通偏好可能演化为小众同类景点堆叠，形成 personalization overfitting。
- 主 ranking、spatial planner、repair 和 portfolio evaluator 的选择标准仍可能存在不一致。
- evaluator 指标与人工体验存在 gap：例如自动指标可通过，但行程的代表性、节奏、酒店可信度或解释质量仍可能不理想。

## 6. 当前工程判断

当前系统已经具备：可观测、可评估、可定位、有边界修复、并有回归测试的 Agent planning harness。

但尚未达到：对所有真实输入稳定生成高质量路线、完全自动的 itinerary quality optimization，或商业级旅行规划质量。当前更准确的定位是“可研究、可验证、可迭代的约束规划 Agent 原型”，而不是完成态的推荐产品。

## 7. 下一步计划

1. 统一并完整验证 CandidateAcceptancePolicy，覆盖初始选择、补点、替换、repair、replan/resume。
2. 修复 quota、selection role、rejection reason 与最终 portfolio state 的不一致。
3. 校准 preference personalization，限制普通偏好下的 niche overuse。
4. 扩展真实人工验收集，并加入人工接受率、人工覆盖度等指标。
5. 用真实案例稳定 Quality Gate 与 Repair Loop 的触发、接受和回滚策略。
6. 在核心规划质量稳定后，再考虑 MCP 集成。
7. 引入 Hybrid RAG，使高质量本地知识与工具结果共同参与决策。
8. 增加 hallucination / grounding guard，区分 provider fact、RAG evidence 与模型建议。

## 8. 当前验证结果

以下为本工作区最近一次实际运行结果：

- 单元测试：`139` 项通过，命令：`python -m unittest discover -s tests`。
- deterministic evaluation：`15/15` 通过，报告：[backend/eval_report.json](../backend/eval_report.json)。
- LangGraph evaluation：`15/15` 通过，报告：[backend/eval_report_langgraph.json](../backend/eval_report_langgraph.json)。
- `python -m compileall -q app scripts tests`：通过。
- `git diff --check`：通过（未暂存差异检查）。
- `git diff --cached --check`：当前暂存集发现 1 处 trailing whitespace，位于既有的 `docs/engineering-design-report.md` 第 3 行；提交前应单独修正或确认该文档的暂存状态。

注意：`portfolio_eval_report.json` 当前为单独实验运行产物，显示 `0/1`，不应与上述主评测结论混用，也不建议纳入提交。

## 9. 面试叙事摘要

这个项目的重点不是把旅行规划交给一次 LLM 调用，而是把开放式 Agent 行为拆成可验证的工程链路。我没有通过盲目调 prompt 或修改评分权重解决失败，而是先建立 structured observability 和独立 evaluation harness，再用真实 failure 区分候选召回、排序、空间规划、约束、状态管理和 repair 的问题。系统已具备 bounded repair、degraded best-effort、POI identity 与回归测试，但我也明确保留了开放式规划质量评估、个性化过拟合和真实地图数据差异等局限。这体现的是一个可诊断、可迭代的 Agent harness，而非宣称已经达到商业级路线优化。
