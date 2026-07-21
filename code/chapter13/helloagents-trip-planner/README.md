# 约束感知型多智能体旅行规划系统

这是基于 HelloAgents 教程项目二次改造的实习简历版 AI Agent 项目。系统不只生成一段行程文本，而是把 POI 获取、路线评估、预算估算、约束校验、规划复核拆成多个角色，并输出可解释、可评测的旅行计划。

## 核心能力

- 多角色规划：`POICollector`、`RouteEvaluator`、`BudgetEstimator`、`ConstraintChecker`、`PlannerReviewer`
- 约束输入：预算上限、行程节奏、必去景点、避开类型、饮食限制、每日最大步行距离、住宿区域
- 结构化输出：每日行程、路线段、预算明细、约束报告、风险提示、RAG 攻略证据
- 轻量 RAG：检索 `backend/app/data/travel_guides/*.md`，为规划提供城市攻略、路线建议和避坑信息
- 可评测：`backend/scripts/evaluate_planner.py` 生成约束满足率、平均耗时、预算和距离统计
- 可离线演示：未配置高德 API Key 时自动使用本地 POI 和路线估算降级数据

## 架构

```text
Vue3 Form
  -> FastAPI /api/trip/plan
  -> POICollector         获取候选景点/酒店
  -> TravelGuideRAG       检索本地城市攻略证据
  -> RouteEvaluator       估算景点间路线距离和时长
  -> BudgetEstimator      汇总门票/酒店/餐饮/交通预算
  -> ConstraintChecker    检查预算、步行、必去点、饮食限制
  -> PlannerReviewer      生成风险提示和复核建议
  -> Vue3 Result          展示约束得分、路线、预算、证据来源
```

## 运行

后端：

```bash
cd backend
pip install -r requirements.txt
python run.py
```

前端：

```bash
cd frontend
npm install
npm run dev
```

环境变量：

- `AMAP_API_KEY`：可选。缺失时使用本地降级数据。
- `LLM_API_KEY` / `OPENAI_API_KEY`：当前主流程不强依赖 LLM，后续可用于 PlannerReviewer 生成自然语言解释。
- `VITE_API_BASE_URL`：默认 `http://localhost:8000`。

## 评测

```bash
cd backend
python scripts/evaluate_planner.py
```

脚本会读取 `eval_cases.jsonl`，输出 `eval_report.md`。建议继续扩展到 100 条 case，覆盖预算紧张、亲子游、老人游、雨天、必去景点、饮食限制等场景。

## 简历写法草稿

- 设计多角色 Agent 编排流程，将 POI 获取、路线评估、预算估算、约束校验、规划复核拆分为独立模块，降低单提示词生成不可控问题。
- 实现预算、步行距离、必去景点、饮食限制等约束报告，并支持用户编辑景点顺序后重新计算路线、预算和约束得分。
- 构建本地城市攻略 RAG 检索，为行程推荐提供路线建议和风险提示证据，减少纯 LLM 编造。
- 编写离线评测脚本，统计约束满足率、平均耗时、预算误差、路线距离等指标，为简历中的量化结果提供真实来源。

## 下一步可增强

- 接入真实高德路线 API 并解析公交/步行方案
- 扩展 100+ 条评测 case 和 badcase 复盘
- 用 LLM 只做 Reviewer 文案生成，不参与事实获取
- 增加 Docker Compose 和 CI 检查
