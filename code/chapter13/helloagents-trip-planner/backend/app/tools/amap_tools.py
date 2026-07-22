"""Narrow HelloAgents tool adapters around the typed Amap service."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from hello_agents.tools.base import Tool, ToolParameter

try:
    # hello-agents 0.2.9 exists in both legacy and protocol-enabled builds.
    from hello_agents.tools.base import ToolResponse
except ImportError:  # pragma: no cover - exercised by the runtime environment
    ToolResponse = None

from ..models.schemas import Hotel
from ..services.amap_service import AmapService


class AttractionSearchTool(Tool):
    """Allow the attraction agent to search POIs, but nothing else."""

    def __init__(self, amap_service: AmapService):
        super().__init__(
            name="search_attractions",
            description="根据关键词搜索指定城市的景点 POI",
        )
        self.amap_service = amap_service

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="city", type="string", description="目的地城市"),
            ToolParameter(name="keyword", type="string", description="景点搜索关键词"),
        ]

    def run(self, parameters: Dict[str, Any]) -> Any:
        city = str(parameters.get("city") or "").strip()
        keyword = str(parameters.get("keyword") or "").strip()
        if not city or not keyword:
            return _error("INVALID_ARGUMENT", "city 和 keyword 不能为空")

        pois = self.amap_service.search_poi(keywords=keyword, city=city)
        return _success(
            f"找到 {len(pois)} 个景点候选",
            {"pois": [poi.model_dump(mode="json") for poi in pois]},
        )


class WeatherQueryTool(Tool):
    """Allow the weather agent to query a city's forecast."""

    def __init__(self, amap_service: AmapService):
        super().__init__(
            name="query_weather",
            description="查询指定城市未来几天的天气预报",
        )
        self.amap_service = amap_service

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="city", type="string", description="目的地城市"),
        ]

    def run(self, parameters: Dict[str, Any]) -> Any:
        city = str(parameters.get("city") or "").strip()
        if not city:
            return _error("INVALID_ARGUMENT", "city 不能为空")

        weather = self.amap_service.get_weather(city)
        return _success(
            f"获得 {len(weather)} 天天气预报",
            {"weather": [item.model_dump(mode="json") for item in weather]},
        )


class HotelSearchTool(Tool):
    """Allow the hotel agent to search and normalize hotel candidates."""

    def __init__(self, amap_service: AmapService):
        super().__init__(
            name="search_hotels",
            description="根据区域和住宿档次搜索指定城市的酒店 POI",
        )
        self.amap_service = amap_service

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="city", type="string", description="目的地城市"),
            ToolParameter(name="keyword", type="string", description="酒店搜索关键词"),
            ToolParameter(
                name="accommodation",
                type="string",
                description="用户选择的住宿档次",
            ),
        ]

    def run(self, parameters: Dict[str, Any]) -> Any:
        city = str(parameters.get("city") or "").strip()
        keyword = str(parameters.get("keyword") or "").strip()
        accommodation = str(parameters.get("accommodation") or "经济型酒店").strip()
        if not city or not keyword:
            return _error("INVALID_ARGUMENT", "city 和 keyword 不能为空")

        nightly_cost = _nightly_cost(accommodation)
        pois = self.amap_service.search_poi(keywords=keyword, city=city)
        hotels = [
            Hotel(
                name=poi.name,
                address=poi.address,
                location=poi.location,
                price_range=f"{nightly_cost}-{nightly_cost + 200}元/晚",
                rating=str(poi.rating or 4.5),
                distance="待路线规划确认",
                type=accommodation,
                estimated_cost=nightly_cost,
            )
            for poi in pois
        ]
        return _success(
            f"找到 {len(hotels)} 个酒店候选",
            {"hotels": [hotel.model_dump(mode="json") for hotel in hotels]},
        )


def _nightly_cost(accommodation: str) -> int:
    if "豪华" in accommodation:
        return 1000
    if "舒适" in accommodation:
        return 650
    if "民宿" in accommodation:
        return 450
    return 350


def _success(text: str, data: Dict[str, Any]) -> Any:
    """Return the response shape expected by either 0.2.9 tool protocol."""

    if ToolResponse is not None:
        return ToolResponse.success(text=text, data=data)
    return json.dumps(
        {"success": True, "message": text, "data": data},
        ensure_ascii=False,
    )


def _error(code: str, message: str) -> Any:
    if ToolResponse is not None:
        return ToolResponse.error(code=code, message=message)
    return json.dumps(
        {"success": False, "error_code": code, "message": message},
        ensure_ascii=False,
    )
