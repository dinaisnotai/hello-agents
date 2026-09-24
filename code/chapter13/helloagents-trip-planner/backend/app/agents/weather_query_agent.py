"""LLM-backed weather specialist with deterministic date completion."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from threading import Lock
from typing import Optional

from hello_agents import HelloAgentsLLM, SimpleAgent, ToolRegistry

from ..models.agent_outputs import WeatherQueryResult
from ..models.schemas import TripRequest, WeatherInfo
from ..tools.amap_tools import WeatherQueryTool
from .agent_utils import (
    parse_agent_result,
    run_stateless_agent,
    specialist_max_tool_iterations,
)
from .prompts import WEATHER_QUERY_PROMPT


class WeatherQueryAgent:
    def __init__(
        self,
        llm: Optional[HelloAgentsLLM],
        weather_tool: WeatherQueryTool,
    ):
        self.weather_tool = weather_tool
        self._run_lock = Lock()
        self.agent: Optional[SimpleAgent] = None
        if llm is not None:
            registry = ToolRegistry()
            registry.register_tool(weather_tool)
            self.agent = SimpleAgent(
                name="WeatherQueryAgent",
                llm=llm,
                system_prompt=WEATHER_QUERY_PROMPT,
                tool_registry=registry,
                enable_tool_calling=True,
            )

    def run(self, request: TripRequest, *, evidence=()) -> WeatherQueryResult:
        observed = self._complete_dates(request, self.weather_tool.amap_service.get_weather(request.city))
        agent_input = {
            "city": request.city,
            "start_date": request.start_date,
            "end_date": request.end_date,
            "travel_days": request.travel_days,
            "verified_weather": [item.model_dump(mode="json") for item in observed],
            "travelers": request.travelers,
            "guide_evidence": [item.model_dump(mode="json") for item in evidence[:4]],
        }
        if self.agent is not None:
            try:
                with self._run_lock:
                    raw_result = run_stateless_agent(
                        self.agent,
                        json.dumps(agent_input, ensure_ascii=False),
                        max_tool_iterations=specialist_max_tool_iterations(),
                    )
                result = parse_agent_result(raw_result, WeatherQueryResult)
                result.weather = observed
                # Unknown dates must not inherit a model-generated forecast.
                verified_risk_text = []
                for text in result.risk_summary:
                    if any(
                        item.day_weather != "未知"
                        and item.date in text
                        and any(
                            condition and condition != "未知" and condition in text
                            for condition in (item.day_weather, item.night_weather)
                        )
                        for item in observed
                    ):
                        verified_risk_text.append(text)
                result.risk_summary = list(dict.fromkeys([
                    *self._summarize_risks(observed),
                    *verified_risk_text,
                ]))
                result.used_fallback = False
                return result
            except Exception as exc:
                return self._fallback(request, f"天气 Agent 调用失败：{exc}")

        return self._fallback(request, "LLM 未启用，使用确定性天气查询")

    def _fallback(self, request: TripRequest, warning: str) -> WeatherQueryResult:
        weather = self.weather_tool.amap_service.get_weather(request.city)
        completed = self._complete_dates(request, weather)
        risks = self._summarize_risks(completed)
        return WeatherQueryResult(
            weather=completed,
            risk_summary=risks,
            warnings=[warning],
            used_fallback=True,
        )

    def _complete_dates(
        self,
        request: TripRequest,
        weather: list[WeatherInfo],
    ) -> list[WeatherInfo]:
        by_date = {item.date: item for item in weather}
        start_date = datetime.strptime(request.start_date, "%Y-%m-%d")
        completed = []
        for index in range(request.travel_days):
            date = (start_date + timedelta(days=index)).strftime("%Y-%m-%d")
            completed.append(
                by_date.get(date)
                or WeatherInfo(
                    date=date,
                    day_weather="未知",
                    night_weather="未知",
                )
            )
        return completed

    def _summarize_risks(self, weather: list[WeatherInfo]) -> list[str]:
        risks = []
        for item in weather:
            condition = f"{item.day_weather}{item.night_weather}"
            if any(word in condition for word in ("雨", "雪", "雷", "台风")):
                risks.append(f"{item.date}可能有恶劣天气，建议准备室内备选行程")
            if isinstance(item.day_temp, int) and item.day_temp >= 35:
                risks.append(f"{item.date}可能高温，建议减少正午户外活动")
        return risks
