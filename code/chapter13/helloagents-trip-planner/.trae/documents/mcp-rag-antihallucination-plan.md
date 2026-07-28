# MCP 工具层 + Hybrid RAG + 防幻觉协议 实现计划

## 一、总体概述

本计划在现有 HelloAgents Trip Planner 架构上新增三个核心能力：

1. **MCP 工具层**：用 FastMCP 标准化工具接口，解耦 Agent 与 AmapService
2. **Hybrid RAG**：Qdrant 本地向量库 + BM25 关键词 + RRF 融合检索
3. **防幻觉协议**：PlanningDraft/DailyIntent 约束 + POI ID 校验 + 引用可追溯

***

## 二、当前状态分析

### 2.1 现有架构关键发现

| 层级             | 文件                                           | 现状                                                                             | 问题                                      |
| -------------- | -------------------------------------------- | ------------------------------------------------------------------------------ | --------------------------------------- |
| 工具             | `tools/amap_tools.py`                        | HelloAgents `Tool` 子类，直接持有 `AmapService` 引用                                    | Agent 与 AmapService 紧耦合                 |
| 工具             | `tools/amap_tools.py`                        | 3 个工具（search\_attractions, query\_weather, search\_hotels），缺少 calculate\_route | 缺少路线计算工具                                |
| RAG            | `services/rag_service.py`                    | 内存向量检索，仅 cosine 相似度 + 简单关键词 fallback                                           | 无持久化、无 BM25、无 RRF 融合                    |
| RAG chunk      | `services/rag_service.py`                    | chunk 只有 title/source/city/tags/text/index                                     | 缺少 document\_id、chunk\_id、信息类型、可信等级等元数据 |
| EvidenceSource | `models/schemas.py:317`                      | 仅有 title/city/source/snippet/score                                             | 缺少检索方式、更新时间、可信等级                        |
| PlanningDraft  | `workflows/planning_state.py:34`             | 简单的 summary/source/suggested\_themes/hard\_constraints                         | 无 poi\_id 引用约束、无 DailyIntent            |
| Agent 工具绑定     | `agents/multi_agent_orchestrator.py:103-116` | 直接创建 Tool 实例传给 Agent                                                           | 无 MCP 抽象层                               |
| 依赖             | `requirements.txt`                           | `fastmcp>=2.0.0` 已声明但未使用                                                       | 直接可用                                    |

### 2.2 现有数据流

```
TripRequest → MultiAgentOrchestrator / LangGraphTripWorkflow
  ├─ AttractionSearchAgent → AttractionSearchTool → AmapService.search_poi()
  ├─ WeatherQueryAgent → WeatherQueryTool → AmapService.get_weather()
  ├─ HotelAgent → HotelSearchTool → AmapService.search_poi()
  ├─ RAG → TravelGuideRAG.search() → OpenAICompatibleEmbedder (内存)
  └─ PlannerAgent / MultiAgentTripPlanner → 确定性规划
```

### 2.3 目标架构

```
TripRequest → LangGraphTripWorkflow
  ├─ MCP Tool Server (FastMCP, 独立进程或内嵌)
  │   ├─ search_pois    → AmapService.search_poi()
  │   ├─ query_weather  → AmapService.get_weather()
  │   ├─ search_hotels  → AmapService.search_poi() + Hotel 转换
  │   └─ calculate_route → AmapService.route_between_pois()
  ├─ MCP Tool Adapter (HelloAgents Tool 封装 MCP Client)
  │   └─ 专家 Agent 不直接依赖 AmapService
  ├─ Hybrid RAG
  │   ├─ Qdrant Client Local Mode (持久化向量)
  │   ├─ BM25 (rank-bm25)
  │   └─ RRF 融合 → 去重 → Top 5
  └─ 防幻觉协议
      ├─ PlanningDraft / DailyIntent (poi_id 白名单)
      ├─ POI ID 校验器
      └─ RAG 引用校验器
```

***

## 三、详细实施步骤

### Phase 1: MCP 工具层（约 8 个文件）

#### Step 1.1: 创建 MCP Schema 定义

**文件**: `backend/app/mcp/__init__.py`（空文件）

**文件**: `backend/app/mcp/schemas.py`

定义 MCP 工具的输入/输出 Pydantic Schema：

```python
# ---- 统一响应格式 ----
class MCPToolResult(BaseModel):
    success: bool
    data: Any = None
    provider: str = "amap"
    fetched_at: datetime
    is_fallback: bool = False
    error_code: str | None = None
    error_message: str | None = None

# ---- 四个工具的输入/输出 ----
class SearchPOIsInput(BaseModel):
    city: str
    keywords: str
    category: str | None = None
    limit: int = 20

class SearchPOIsOutput(BaseModel):
    pois: list[POIInfo]

class QueryWeatherInput(BaseModel):
    city: str
    days: int = 7

class QueryWeatherOutput(BaseModel):
    weather: list[WeatherInfo]

class SearchHotelsInput(BaseModel):
    city: str
    keyword: str
    accommodation: str = "经济型酒店"
    limit: int = 10

class SearchHotelsOutput(BaseModel):
    hotels: list[Hotel]

class CalculateRouteInput(BaseModel):
    origin_name: str
    origin_address: str
    origin_lng: float
    origin_lat: float
    destination_name: str
    destination_address: str
    destination_lng: float
    destination_lat: float
    city: str
    route_type: str = "walking"  # walking/driving/transit

class CalculateRouteOutput(BaseModel):
    route: RouteInfo
```

#### Step 1.2: 创建 MCP Server

**文件**: `backend/app/mcp/server.py`

使用 FastMCP 创建 MCP Server，暴露四个工具：

```python
from fastmcp import FastMCP

mcp = FastMCP("trip-planner-tools")

@mcp.tool()
async def search_pois(input: SearchPOIsInput) -> MCPToolResult: ...

@mcp.tool()
async def query_weather(input: QueryWeatherInput) -> MCPToolResult: ...

@mcp.tool()
async def search_hotels(input: SearchHotelsInput) -> MCPToolResult: ...

@mcp.tool()
async def calculate_route(input: CalculateRouteInput) -> MCPToolResult: ...
```

每个工具内部调用 `AmapService`，统一包装为 `MCPToolResult`。

* 测试模式：使用 `mcp.run(transport="memory")` （内存 transport）

* 开发模式：使用 `mcp.run(transport="streamable-http")` 挂载到 FastAPI

#### Step 1.3: 创建 MCP Tool Adapter

**文件**: `backend/app/mcp/adapter.py`

将 MCP Client 工具包装为 HelloAgents `Tool` 子类，使专家 Agent 通过 MCP Client 调用工具而非直接依赖 AmapService：

```python
class MCPToolAdapter(Tool):
    """HelloAgents Tool that delegates to an MCP tool."""
    def __init__(self, mcp_client, tool_name: str, tool_description: str, input_schema, output_schema):
        ...
    def get_parameters(self) -> list[ToolParameter]: ...
    def run(self, parameters: dict) -> Any: ...
```

提供工厂函数，创建四个适配器实例：

* `create_search_pois_adapter(mcp_client) -> MCPToolAdapter`

* `create_query_weather_adapter(mcp_client) -> MCPToolAdapter`

* `create_search_hotels_adapter(mcp_client) -> MCPToolAdapter`

* `create_calculate_route_adapter(mcp_client) -> MCPToolAdapter`

#### Step 1.4: 修改 Agent 使用 MCP Adapter

**文件**: `backend/app/agents/multi_agent_orchestrator.py`

修改 `MultiAgentOrchestrator.__init__`，接受可选的 MCP Client，有 MCP Client 时使用 `MCPToolAdapter`，否则回退到现有的直接 Tool：

```python
def __init__(self, ..., mcp_client=None):
    if mcp_client:
        attraction_tool = create_search_pois_adapter(mcp_client)
        weather_tool = create_query_weather_adapter(mcp_client)
        hotel_tool = create_search_hotels_adapter(mcp_client)
    else:
        # 回退到现有直接 Tool
        attraction_tool = AttractionSearchTool(self.amap_service)
        ...
```

#### Step 1.5: FastAPI 集成

**文件**: `backend/app/api/main.py`

在 FastAPI 应用中挂载 MCP Server（Streamable HTTP 模式）：

```python
# 开发环境
mcp_server = create_mcp_server()
app.mount("/mcp", mcp_server.get_asgi_app())
```

#### Step 1.6: 契约测试

**文件**: `backend/tests/test_mcp_tools.py`

* 使用内存 MCP Transport 创建 MCP Client

* 对四个工具分别编写契约测试

* 验证：

  * 正常调用返回 success=True

  * 统一响应格式包含所有必需字段

  * 参数校验（空 city/keywords 返回 error）

  * fallback 场景（Amap API 不可用时 is\_fallback=True）

***

### Phase 2: Hybrid RAG（约 6 个文件）

#### Step 2.1: 添加依赖

**文件**: `backend/requirements.txt`

追加：

```
qdrant-client>=1.9.0,<2.0.0
rank-bm25>=0.2.2
```

#### Step 2.2: 增强 Chunk Schema

**文件**: `backend/app/rag/__init__.py`（空文件）

**文件**: `backend/app/rag/schemas.py`

```python
class GuideChunk(BaseModel):
    document_id: str          # 源文件名（如 "beijing.md"）
    chunk_id: str             # 唯一标识（如 "beijing-003"）
    city: str                 # 城市（如 "北京"）
    title: str                # 章节标题
    text: str                 # 完整原文
    info_type: str            # 信息类型：knowledge_card / fact / warning / strategy
    source: str               # 来源路径
    tags: list[str]           # 标签
    updated_at: datetime      # 更新时间
    valid_until: datetime | None  # 有效期
    credibility: int          # 可信等级 1-5
    embedding: list[float] | None  # 向量（可选）
```

#### Step 2.3: 创建 Qdrant 索引器

**文件**: `backend/app/rag/indexer.py`

```python
class QdrantIndexer:
    def __init__(self, path: str = "./data/qdrant", collection_name: str = "travel_guides"):
        # Qdrant Client Local Mode - 无需额外部署
        self.client = QdrantClient(path=path)
        self.collection_name = collection_name
    
    def ensure_collection(self, vector_size: int): ...
    
    def index_chunks(self, chunks: list[GuideChunk]) -> int:
        # 增量写入：按 chunk_id upsert
        ...
    
    def search_vector(self, query_vector: list[float], city: str, top_k: int = 10) -> list[ScoredChunk]:
        # 城市和元数据过滤 + 向量检索
        ...
```

#### Step 2.4: 创建 Hybrid Retriever

**文件**: `backend/app/rag/retriever.py`

实现固定检索流程：

```python
class HybridRetriever:
    def __init__(self, indexer: QdrantIndexer):
        self.indexer = indexer
        self.bm25_index: dict[str, BM25Okapi] = {}  # 按城市分 BM25 索引
    
    def search(self, city: str, query: str, top_k: int = 5) -> list[EvidenceSource]:
        """
        固定检索流程：
        1. 城市和元数据过滤
        2. 向量 Top 10
        3. BM25 Top 10
        4. RRF 融合
        5. 去重（按 chunk_id）
        6. 返回 Top 5
        
        Embedding 不可用时：自动跳过向量检索，仅用 BM25
        """
        ...
    
    def _rrf_fusion(
        self,
        vector_results: list[tuple[str, float]],
        bm25_results: list[tuple[str, float]],
        k: int = 60,
    ) -> list[tuple[str, float]]:
        """Reciprocal Rank Fusion"""
        ...
    
    def _deduplicate(self, results: list[tuple[str, float]]) -> list[tuple[str, float]]:
        """按 chunk_id 去重，保留最高分"""
        ...
```

**关键防幻觉行为**：

* 城市过滤是硬约束，绝不返回其他城市内容

* BM25 使用全部分词（中文分词 + 2-gram），确保零相关度内容不会仅因同城被返回

* 相关度为零的 chunk 在 BM25 阶段 score=0，RRF 融合后自然排在末尾

#### Step 2.5: 更新 EvidenceSource

**文件**: `backend/app/models/schemas.py`

在现有 `EvidenceSource` 上追加字段：

```python
class EvidenceSource(BaseModel):
    title: str
    city: str = ""
    source: str = ""           # 保留原有
    chunk_id: str = ""         # 新增：追溯到具体切片
    snippet: str
    score: float = 0
    retrieval_method: str = "" # 新增："vector" / "bm25" / "hybrid"
    updated_at: datetime | None = None  # 新增
    credibility: int = 1       # 新增：可信等级 1-5
```

#### Step 2.6: 离线索引 CLI 命令

**文件**: `backend/app/rag/cli.py`

```python
# 用法：python -m app.rag.cli index --data-dir ./data/travel_guides
# 用法：python -m app.rag.cli index --data-dir ./data/travel_guides --incremental

def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    
    index_parser = subparsers.add_parser("index")
    index_parser.add_argument("--data-dir", required=True)
    index_parser.add_argument("--incremental", action="store_true")
    index_parser.add_argument("--qdrant-path", default="./data/qdrant")
    ...
```

索引流程：

1. 扫描 `data_dir` 下所有 `.md` 文件
2. 按 `##` 标题分片
3. 解析元数据（document\_id、chunk\_id、城市、信息类型、标签、可信等级）
4. 调用 Embedding 服务生成向量
5. 增量写入 Qdrant（按 chunk\_id upsert，不重复写入已有内容）
6. 构建和持久化 BM25 索引

#### Step 2.7: 重构 RAG Service

**文件**: `backend/app/services/rag_service.py`

重构 `TravelGuideRAG` 以使用新的 Hybrid RAG：

```python
class TravelGuideRAG:
    def __init__(self, ...):
        self.retriever = HybridRetriever(...)
    
    def search(self, city: str, query: str, top_k: int = 5) -> list[EvidenceSource]:
        # 委托给 HybridRetriever
        return self.retriever.search(city, query, top_k)
    
    # 保留 reindex 方法供离线命令调用
    def reindex(self, data_dir: Path, incremental: bool = True): ...
```

保持 `get_travel_guide_rag()` 单例不变，保证向后兼容。

***

### Phase 3: 防幻觉协议（约 5 个文件）

#### Step 3.1: 新增 Schema

**文件**: `backend/app/workflows/planning_state.py`

追加：

```python
class DailyIntent(BaseModel):
    """LLM 生成的单日行程意图，poi_id 只能来自专家节点返回"""
    day_index: int
    date: str
    theme: str = ""
    poi_ids: list[str] = Field(default_factory=list)  # 必须可追溯
    excluded_types: list[str] = Field(default_factory=list)
    notes: str = ""


class PlanningDraft(BaseModel):
    """扩充版规划草案，增强引用约束"""
    summary: str = ""
    source: Literal["context_only", "llm", "fallback"] = "context_only"
    suggested_themes: list[str] = Field(default_factory=list)
    hard_constraints: list[str] = Field(default_factory=list)
    daily_intents: list[DailyIntent] = Field(default_factory=list)  # 新增
    rag_references: list[dict] = Field(default_factory=list)  # 新增：{source, chunk_id}
    llm_generated_fields: set[str] = Field(default_factory=set)  # 记录 LLM 生成了哪些字段
```

#### Step 3.2: POI ID 校验器

**文件**: `backend/app/constraints/validators/poi_id_validator.py`

```python
class POIIDValidator:
    """校验 LLM 草案中的 poi_id 合法性"""
    
    def __init__(self, specialist_poi_ids: set[str], excluded_types: set[str], city: str):
        self.valid_ids = specialist_poi_ids
        self.excluded_types = excluded_types
        self.city = city
    
    def validate_draft(self, draft: PlanningDraft) -> tuple[PlanningDraft, list[str]]:
        """
        校验规则：
        1. 未知 ID → 剔除并记录警告
        2. 重复 ID（同一天内） → 去重
        3. 被排除类型 → 剔除
        4. 城市不匹配 → 剔除
        
        返回：(清洗后的 draft, 警告列表)
        """
        ...
    
    def validate_poi_belongs_to_city(self, poi_id: str, city: str) -> bool:
        """检查 POI 是否属于目标城市"""
        ...
```

#### Step 3.3: LLM 生成字段剥离器

**文件**: `backend/app/constraints/validators/llm_content_filter.py`

```python
class LLMContentFilter:
    """从 LLM 输出中剥离不可靠字段"""
    
    FORBIDDEN_FIELDS = {
        "距离", "路线时长", "步行时间", "驾车时间", "交通时长",
        "票价", "门票价格", "酒店价格", "住宿费用",
        "具体里程", "公里数",
    }
    
    def filter_draft(self, draft: PlanningDraft) -> PlanningDraft:
        """
        LLM 不允许生成：
        - 距离
        - 路线时长
        - 票价
        - 酒店价格
        
        这些字段必须来自 AmapService 工具调用结果。
        """
        ...
    
    def filter_plan_output(self, plan: TripPlan) -> TripPlan:
        """对最终计划也执行同样的过滤"""
        ...
```

#### Step 3.4: RAG 引用校验器

**文件**: `backend/app/constraints/validators/rag_citation_validator.py`

```python
class RAGCitationValidator:
    """确保 Planner 输出的结论只能引用真实 RAG 检索结果"""
    
    def __init__(self, valid_sources: list[EvidenceSource]):
        self.valid_chunk_ids = {s.chunk_id for s in valid_sources}
        self.valid_source_paths = {s.source for s in valid_sources}
    
    def validate_references(self, draft: PlanningDraft) -> list[str]:
        """
        校验 draft.rag_references 中的每个引用：
        - source 必须在 valid_source_paths 中
        - chunk_id 必须在 valid_chunk_ids 中
        - 无法映射的引用 → 记录警告
        
        返回警告列表
        """
        ...
    
    def filter_factual_claims(self, claims: list[dict]) -> list[dict]:
        """
        事实型声明（非主观建议）必须能映射到真实 chunk：
        - 无有效来源的 → 丢弃
        - 来源不可信的（credibility < 3）→ 标记警告
        """
        ...
```

#### Step 3.5: 集成到 LangGraph 工作流

**文件**: `backend/app/workflows/langgraph_trip_workflow.py`

修改工作流：

1. **`build_draft`** **节点改造**：

   * 生成 `PlanningDraft`（含 `daily_intents`）

   * 如果 LLM 生成了 `daily_intents`，其中的 `poi_ids` 必须来自专家节点返回

2. **新增** **`validate_draft`** **节点**（在 `build_draft` 之后）：

```python
graph.add_node("validate_draft", self.validate_draft)
graph.add_edge("build_draft", "validate_draft")
graph.add_edge("validate_draft", "deterministic_planning")
```

```
validate_draft 节点逻辑：
├─ POIIDValidator.validate_draft()
│   ├─ 剔除未知 ID
│   ├─ 去重
│   ├─ 剔除被排除类型
│   └─ 剔除城市不匹配
├─ LLMContentFilter.filter_draft()
│   └─ 剥离去距离/时长/票价/价格等字段
├─ RAGCitationValidator.validate_references()
│   └─ 校验所有 rag_references 可追溯
└─ 无有效来源的事实型声明 → 记录 Reviewer 警告并丢弃
```

1. **`deterministic_planning`** **节点**：

   * 只使用清洗后的 `poi_ids`（已经过滤了虚假 ID）

   * 距离、时长等数据必须来自 `AmapService` 的真实调用结果

2. **`finalize`** **节点**：

   * 最终验证所有引用都能映射到真实知识切片

   * `evidence_sources` 中每个 source 必须对应到 RAG 的 `chunk_id`

#### Step 3.6: 修改 Planner Prompt

**文件**: `backend/app/agents/prompts.py`

在 `PLANNER_PROMPT` 中追加防幻觉指令：

```
- 你只能引用专家节点（AttractionSearch/Weather/Hotel）返回结果中的 poi_id
- 禁止编造景点 ID、名称、距离、票价、酒店价格
- 如果某个 POI 的 ID 不在有效列表中，不得在计划中引用
- 建议类内容需要标注来源（source/chunk_id）
- 禁止生成步行距离、驾车时间、公交路线等数据，这些由工具提供
```

***

### Phase 4: 验收测试（约 3 个文件）

#### Step 4.1: MCP 契约测试

**文件**: `backend/tests/test_mcp_tools.py`

```python
class TestMCPContract:
    """MCP 工具可独立调用并通过契约测试"""
    
    def test_search_pois_contract(self): ...
    def test_query_weather_contract(self): ...
    def test_search_hotels_contract(self): ...
    def test_calculate_route_contract(self): ...
    def test_unified_response_format(self): ...
    def test_invalid_params_return_error(self): ...
    def test_fallback_when_amap_unavailable(self): ...
```

#### Step 4.2: Hybrid RAG 测试

**文件**: `backend/tests/test_hybrid_rag.py`

```python
class TestHybridRAG:
    """Embedding 故障时 BM25 仍返回正确城市证据"""
    
    def test_bm25_fallback_when_embedding_fails(self): ...
    def test_rrf_fusion_produces_valid_scores(self): ...
    def test_city_filter_never_returns_other_city(self): ...
    def test_zero_relevance_not_returned_as_evidence(self): ...
    def test_deduplication_by_chunk_id(self): ...
    def test_incremental_indexing(self): ...
```

#### Step 4.3: 防幻觉协议测试

**文件**: `backend/tests/test_anti_hallucination.py`

```python
class TestAntiHallucination:
    """LLM 构造虚假 POI ID 时无法进入最终计划"""
    
    def test_unknown_poi_id_is_stripped(self): ...
    def test_duplicate_poi_ids_are_deduplicated(self): ...
    def test_excluded_type_poi_is_removed(self): ...
    def test_city_mismatch_poi_is_removed(self): ...
    def test_llm_generated_distance_is_filtered(self): ...
    def test_llm_generated_price_is_filtered(self): ...
    def test_rag_citation_must_map_to_real_chunk(self): ...
    def test_factual_claim_without_source_is_dropped(self): ...
    def test_final_references_all_map_to_real_chunks(self): ...
```

***

## 四、文件变更清单

### 新增文件（13 个）

| 文件                                                             | 用途                            |
| -------------------------------------------------------------- | ----------------------------- |
| `backend/app/mcp/__init__.py`                                  | MCP 包                         |
| `backend/app/mcp/schemas.py`                                   | MCP 工具 Pydantic Schema        |
| `backend/app/mcp/server.py`                                    | FastMCP Server 实现             |
| `backend/app/mcp/adapter.py`                                   | HelloAgents MCP Tool Adapter  |
| `backend/app/rag/__init__.py`                                  | RAG 包                         |
| `backend/app/rag/schemas.py`                                   | 增强 GuideChunk Schema          |
| `backend/app/rag/indexer.py`                                   | Qdrant 索引器                    |
| `backend/app/rag/retriever.py`                                 | Hybrid Retriever（向量+BM25+RRF） |
| `backend/app/rag/cli.py`                                       | 离线索引 CLI 命令                   |
| `backend/app/constraints/validators/poi_id_validator.py`       | POI ID 校验器                    |
| `backend/app/constraints/validators/llm_content_filter.py`     | LLM 生成字段过滤器                   |
| `backend/app/constraints/validators/rag_citation_validator.py` | RAG 引用校验器                     |
| `backend/tests/test_anti_hallucination.py`                     | 防幻觉协议测试                       |

### 修改文件（11 个）

| 文件                                                 | 修改内容                                                                  |
| -------------------------------------------------- | --------------------------------------------------------------------- |
| `backend/requirements.txt`                         | 添加 qdrant-client, rank-bm25                                           |
| `backend/app/models/schemas.py`                    | EvidenceSource 增加 retrieval\_method/updated\_at/credibility/chunk\_id |
| `backend/app/workflows/planning_state.py`          | 新增 DailyIntent，扩充 PlanningDraft                                       |
| `backend/app/workflows/langgraph_trip_workflow.py` | 新增 validate\_draft 节点，改造 build\_draft，集成防幻觉校验                         |
| `backend/app/services/rag_service.py`              | 重构为使用 Hybrid RAG，保留接口兼容                                               |
| `backend/app/agents/multi_agent_orchestrator.py`   | 支持 MCP Client 注入，使用 MCPToolAdapter                                    |
| `backend/app/agents/prompts.py`                    | 追加防幻觉 Prompt 指令                                                       |
| `backend/app/api/main.py`                          | 挂载 MCP Server（Streamable HTTP）                                        |
| `backend/tests/test_rag_service.py`                | 适配新的 RAG 接口                                                           |
| `backend/tests/test_langgraph_trip_workflow.py`    | 适配新的 validate\_draft 节点                                               |
| `backend/.env.example`                             | 添加 QDRANT\_PATH, MCP\_TRANSPORT 等配置项                                  |

### 保留不变的文件

| 文件                                              | 原因                             |
| ----------------------------------------------- | ------------------------------ |
| `backend/app/tools/amap_tools.py`               | 保留作为 fallback，当 MCP 不可用时退回直接调用 |
| `backend/app/services/amap_service.py`          | 核心服务不变，MCP 工具和旧 Tool 都依赖它      |
| `backend/app/agents/attraction_search_agent.py` | Agent 逻辑不变，只是 Tool 来源可切换       |
| `backend/app/agents/hotel_agent.py`             | 同上                             |
| `backend/app/agents/weather_query_agent.py`     | 同上                             |
| `backend/app/agents/trip_planner_agent.py`      | 核心规划逻辑不变                       |
| `backend/app/services/spatial_planner.py`       | 空间规划不变                         |
| 其余所有文件                                          | 无影响                            |

***

## 五、关键设计决策

1. **MCP 与现有 Tool 并存**：在 `MultiAgentOrchestrator` 中通过依赖注入切换。有 MCP Client 时用 Adapter，否则用原有 Tool。这确保平滑迁移，不破坏现有功能。

2. **Qdrant Local Mode**：使用磁盘持久化（`qdrant-client` 的 local mode），不要求额外部署服务。向量和 BM25 索引均在本地文件系统。

3. **RRF 融合 k=60**：这是 RRF 的标准参数，向量检索和 BM25 的排名在融合时有合理的平衡。

4. **Embedding 降级链**：Embedding 可用 → 向量+BM25+RRF；Embedding 不可用 → 仅 BM25。绝不返回空结果或错误城市内容。

5. **防幻觉在 Graph 层面拦截**：在 `validate_draft` 节点统一执行 ID 校验、内容过滤、引用校验，确保虚假信息无法进入后续的 `deterministic_planning`。

6. **PlanningDraft 可审计**：所有被剔除的 ID、被过滤的字段、被丢弃的声明都记录在 trace/warnings 中，方便调试和审计。

***

## 六、验证步骤

### MCP 工具层

```bash
# 1. 运行 MCP 契约测试
pytest backend/tests/test_mcp_tools.py -v

# 2. 验证 memory transport
python -c "from app.mcp.server import mcp; mcp.run(transport='memory')"

# 3. 启动开发环境验证 Streamable HTTP
uvicorn app.api.main:app --reload
curl http://localhost:8000/mcp/...
```

### Hybrid RAG

```bash
# 4. 运行离线索引
python -m app.rag.cli index --data-dir backend/app/data/travel_guides

# 5. 运行 RAG 测试（含 embedding 故障场景）
pytest backend/tests/test_hybrid_rag.py -v

# 6. 验证 BM25 fallback
# 设置无效 EMBEDDING_API_KEY 后查询，确认仍返回正确城市结果
```

### 防幻觉协议

```bash
# 7. 运行防幻觉测试
pytest backend/tests/test_anti_hallucination.py -v

# 8. 端到端验证：构造虚假 POI ID 的 LLM 输出
# 确认 validate_draft 节点拦截，虚假 ID 不进入最终计划
```

### 集成测试

```bash
# 9. 全量回归测试
pytest backend/tests/ -v

# 10. 端到端旅行规划测试
pytest backend/tests/test_langgraph_trip_workflow.py -v
```

