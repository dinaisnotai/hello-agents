# 约束感知型多智能体旅行规划系统

这是基于 HelloAgents 教程项目二次改造的实习简历版 AI Agent 项目。系统由景点、天气、酒店和规划四个 Agent 组成；精确路线与时间由确定性算法计算，LLM 负责搜索决策、信息整合和软性审查，输出可解释、可评测的旅行计划。

## 核心能力

- 四 Agent 协作：`AttractionSearchAgent`、`WeatherQueryAgent`、`HotelAgent`、`PlannerAgent`
- 约束输入：预算上限、行程节奏、必去景点、避开类型、饮食限制、每日最大步行距离、住宿区域
- 结构化输出：每日行程、路线段、预算明细、约束报告、风险提示、RAG 攻略证据
- 轻量 RAG：检索 `backend/app/data/travel_guides/*.md`，为规划提供城市攻略、路线建议和避坑信息
- 可评测：`backend/scripts/evaluate_planner.py` 生成约束满足率、平均耗时、预算和距离统计
- 可离线演示：未配置高德 API Key 时自动使用本地 POI 和路线估算降级数据

## 架构

```text
Vue3 Form
  -> FastAPI /api/trip/plan
  -> AttractionSearchAgent  理解偏好并调用景点 POI 工具
  -> WeatherQueryAgent      查询天气并生成天气风险
  -> HotelAgent             理解住宿需求并调用酒店 POI 工具
  -> TravelGuideRAG         检索本地城市攻略证据
  -> PlannerAgent           整合专家输出并执行软性审查
  -> SpatialPlanner         保留高优先级景点，按距离分组并生成最近邻顺序
  -> RouteEvaluator       估算景点间路线距离和时长
  -> BudgetEstimator      汇总门票/酒店/餐饮/交通预算
  -> ConstraintChecker    检查预算、步行、必去点、饮食限制
  -> PlannerReviewer      生成风险提示和复核建议
  -> Vue3 Result          展示约束得分、路线、预算、证据来源
```

## 空间规划策略

行程规划明确区分“候选集合”和“可执行行程”：`POICollector` 负责召回、过滤、去重和评分，`SpatialItineraryPlanner` 再根据行程容量保留必去与高分景点。入选景点使用 Haversine 距离进行“最远点选种子 + 最近簇分配”，让每天集中在一个地理片区；每天内部从酒店附近出发，使用最近邻策略生成初始访问顺序。所有相同分值和距离都使用景点名称、POI ID、坐标作为稳定的平局规则，因此相同候选输入会生成相同结果。

节奏中的景点数量是上限而不是必须填满的目标：`relaxed` 最多 2 个景点、每日 420 分钟；`balanced` 最多 3 个、每日 540 分钟；`packed` 最多 4 个、每日 660 分钟。规划器按活动类型估算游览时长，并将酒店往返、景点间交通、游览和餐饮缓冲合并计算；例如长城类活动基础游览时长为 240 分钟，如果加入其他景点后超过当日时间预算，就只安排长城。必去景点数量超过容量时仍以必去约束优先，并在约束报告中提示超时风险。

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
- `LLM_API_KEY` / `OPENAI_API_KEY`：可选。配置后四个 Agent 使用 LLM；缺失或调用失败时自动降级到确定性流程。
- `LLM_TIMEOUT`：单次 LLM 网络调用超时秒数，默认 `60`。三个专家 Agent 会并行执行，PlannerAgent 在其后执行。
- `LLM_MAX_TOOL_ITERATIONS`：专家 Agent 的工具调用轮数，默认 `1`，即最多一次工具决策和一次最终回答；不建议提高。
- `AGENT_LOG_OUTPUTS`：是否在后端控制台打印每个 Agent 的结构化输出，默认 `true`。
- `AGENT_LOG_MAX_CHARS`：单个 Agent 输出日志的最大字符数，默认 `50000`，超出时会明确标记截断。
- `AMAP_LOG_RAW_RESPONSE`：是否打印高德路线接口的原始响应，默认 `false`；仅在排查路线解析时临时开启。
- `AMAP_LOG_MAX_CHARS`：单次高德原始响应日志的最大字符数，默认 `50000`。
- `VITE_API_TIMEOUT_MS`：前端规划请求的总等待时间，默认 `300000`（5 分钟）。
- `VITE_API_BASE_URL`：默认 `http://localhost:8000`。
- `TRIP_SESSION_DB_PATH`：可选的 SQLite 会话数据库路径；默认使用 `backend/data/trip_sessions.db`。

## 多轮行程修改

## 配置 RAG Embedding（火山方舟）

在 `backend` 目录复制 `.env.example` 为 `.env`，然后填入你自己在火山方舟控制台创建的 API Key：

```dotenv
EMBEDDING_API_KEY=你的方舟_API_Key
EMBEDDING_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
EMBEDDING_MODEL=doubao-embedding-text-240515
```

获取方式：登录[火山方舟 API Key 控制台](https://console.volcengine.com/ark/region:ark+cn-beijing/apikey)创建 Key；在模型/推理接入点页面开通文本向量模型，并把模型 ID 或该模型的 Endpoint ID 填到 `EMBEDDING_MODEL`。本项目会请求 `POST /embeddings`，所以 `EMBEDDING_BASE_URL` 必须保留到 `/api/v3`，不要在末尾再加 `/embeddings`。改完后重启后端即可。

若 Key、模型或网络暂时不可用，RAG 会在日志中记录失败原因，并自动改用本地攻略 Markdown 的关键词检索；同一城市仍会返回参考片段，不会再是 0 条。

生成计划后，系统会自动创建持久化会话。结果页可以通过自然语言修改预算、行程节奏和必去景点；每次修改或手动重算都会保存一个新的计划版本。刷新页面时，前端使用会话 ID 从后端恢复最新请求、计划、消息和版本记录。

当前会话接口：

- `POST /api/trip/sessions`：创建会话并生成计划。
- `GET /api/trip/sessions`：查询会话列表。
- `GET /api/trip/sessions/{session_id}`：恢复会话当前状态。
- `POST /api/trip/sessions/{session_id}/messages`：通过对话修改行程。
- `POST /api/trip/sessions/{session_id}/replan`：保存手动调整后的重规划版本。

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

详细计划见 [`docs/commercial-planner-roadmap.md`](docs/commercial-planner-roadmap.md)。

- 接入真实高德路线 API 并解析公交/步行方案
- 扩展 100+ 条评测 case 和 badcase 复盘
- 用 LLM 只做 Reviewer 文案生成，不参与事实获取
- 增加 Docker Compose 和 CI 检查
