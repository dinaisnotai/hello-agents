# 旅行规划助手

输入城市、日期、预算、节奏和必去景点，生成每日行程、交通路线和费用估算。支持保存行程、调整顺序和通过对话修改要求。

## 默认流程

```text
表单 / 对话修改后的要求
  → 攻略检索提供城市、天气和住宿线索
  → 景点、酒店、天气 Agent 选择查询方向
  → 地图工具验证候选和事实
  → 合并同一景区，优先考虑代表性景点
  → 按区域分天，计算游览和交通时间
  → 检查硬约束，执行有上限的事务式修复
  → LLM 审查体验问题，修复后重新验证
  → 返回行程及未满足的要求
```

默认 `WORKFLOW_MODE=langgraph`。Agent 用于查询策略、风险解释和体验审查；地图与确定性算法负责事实、路线、预算和硬约束。修复只有在目标问题改善且没有新增硬约束、重复、步行或交通退化时才提交，并受全流程轮数限制。每天的 2 / 3 / 4 个景点分别对应轻松 / 均衡 / 紧凑，是可选景点的上限，不是必须填满的指标。

代码先从四处看起：

- `backend/app/workflows/langgraph_trip_workflow.py`：编排检索、查询、规划、验证和修复。
- `backend/app/services/planner_service.py`：让首次规划与对话重规划使用同一入口。
- `backend/app/agents/trip_planner_agent.py`：获取候选、组装行程、计算和检查。
- `backend/app/services/spatial_planner.py`：选点、按区域分天、安排顺序。
- `backend/app/services/poi_identity_resolver.py`：景区及内部景点去重。

## 运行

```powershell
cd backend
pip install -r requirements.txt
# 首次运行时把 .env.example 复制为 .env，再填入自己的配置
python run.py
```

```powershell
cd frontend
npm install
npm run dev
```

| 配置 | 用途 |
| --- | --- |
| `WORKFLOW_MODE=langgraph` | 默认工作流；已有 `.env` 也需要设置此值 |
| `AMAP_API_KEY` | 查询真实地图数据；缺失时使用本地演示数据和估算 |
| `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL_ID` | 对话理解使用的模型配置 |
| `TRIP_SESSION_DB_PATH` | 可选；默认使用 `backend/data/trip_sessions.db` 保存会话 |
| `VITE_API_BASE_URL` | 前端访问后端的地址，默认 `http://127.0.0.1:8000`；Windows 上可避开 Docker 占用 IPv6 `localhost` 端口的问题 |

修改后端配置后需要重启。`GET /api/trip/health` 的 `mode` 应为 `langgraph`。

## 验证

```powershell
cd backend
python -m unittest discover -s tests -q
```

前端执行 `npm run build`。

测试使用离线数据，不能证明真实地图在所有城市、日期下都能产生好行程。无地图数据、估算营业时间、无法满足的必去点应查看结果提示。自由文本目前仅支持已有解析器覆盖的表达；重要限制优先填写专门的表单字段。

## 兼容模式

`WORKFLOW_MODE=simple` 可用于无 Agent、无检索的故障隔离，`legacy` 保留旧编排兼容性。`docs/` 中旧设计和阶段报告描述历史实现；本次职责重构见 [优化说明](docs/simplification-review.md)。
