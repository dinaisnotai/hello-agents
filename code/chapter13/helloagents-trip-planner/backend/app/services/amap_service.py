"""Timeout-bounded Amap Web API wrapper with local fallbacks."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..config import get_settings
from ..models.schemas import Location, POIInfo, RouteInfo, RouteStep, WeatherInfo
from .city_name_service import normalize_city_name
from .place_name_service import normalize_place_name, place_names_match

AMAP_API_BASE_URL = "https://restapi.amap.com/v3"
logger = logging.getLogger("uvicorn.error")


class TTLCache:
    def __init__(self, ttl_seconds: int = 1800):
        self.ttl_seconds = ttl_seconds
        self._data: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str) -> Any:
        item = self._data.get(key)
        if not item:
            return None
        expires_at, value = item
        if time.time() > expires_at:
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._data[key] = (time.time() + self.ttl_seconds, value)


class AmapService:
    """A resilient Amap service facade.

    The planner should depend on these typed methods, not raw LLM tool-call text.
    """

    def __init__(self):
        self.cache = TTLCache()
        self.settings = get_settings()

    def search_poi(self, keywords: str, city: str, citylimit: bool = True) -> List[POIInfo]:
        city = normalize_city_name(city)
        cache_key = f"poi:{city}:{keywords}:{citylimit}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        if not self.settings.amap_api_key:
            pois = self._fallback_pois(city, keywords)
            self.cache.set(cache_key, pois)
            return pois

        try:
            data = self._request(
                "/place/text",
                {
                    "keywords": keywords,
                    "city": city,
                    "citylimit": str(citylimit).lower(),
                    "extensions": "all",
                    "offset": 20,
                    "page": 1,
                },
            )
            pois = self._parse_pois(data, keywords, city)
        except Exception as exc:
            print(f"POI search fallback for {city}/{keywords}: {exc}")
            pois = self._fallback_pois(city, keywords)

        if not pois:
            pois = self._fallback_pois(city, keywords)
        self.cache.set(cache_key, pois)
        return pois

    def get_weather(self, city: str) -> List[WeatherInfo]:
        city = normalize_city_name(city)
        cache_key = f"weather:{city}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        if not self.settings.amap_api_key:
            self.cache.set(cache_key, [])
            return []

        try:
            data = self._request(
                "/weather/weatherInfo",
                {"city": city, "extensions": "all"},
            )
            weather = self._parse_weather(data)
        except Exception as exc:
            print(f"Weather fallback for {city}: {exc}")
            weather = []

        self.cache.set(cache_key, weather)
        return weather

    def plan_route(
        self,
        origin_address: str,
        destination_address: str,
        origin_city: Optional[str] = None,
        destination_city: Optional[str] = None,
        route_type: str = "walking",
    ) -> RouteInfo:
        cache_key = f"route:{route_type}:{origin_city}:{destination_city}:{origin_address}:{destination_address}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        if not self.settings.amap_api_key:
            return RouteInfo(route_type=route_type, description="Amap API key missing; route not requested.")

        try:
            origin = self.geocode(origin_address, origin_city)
            destination = self.geocode(destination_address, destination_city)
            if not origin or not destination:
                raise RuntimeError("Could not geocode route endpoints")
            route = self._plan_route_by_locations(
                origin,
                destination,
                route_type=route_type,
                origin_city=origin_city,
                destination_city=destination_city,
            )
        except Exception as exc:
            print(f"Route fallback for {origin_address}->{destination_address}: {exc}")
            route = RouteInfo(route_type=route_type, description="Route service unavailable; used estimated distance.")

        self.cache.set(cache_key, route)
        return route
    
    def route_between_pois(
        self,
        origin_name: str,
        origin_address: str,
        origin: Location,
        destination_name: str,
        destination_address: str,
        destination: Location,
        city: str,
        route_type: str = "walking",
    ) -> RouteInfo:
        """优先使用高德道路路线，失败时退回坐标估算。"""

        if self.settings.amap_api_key:
            try:
                route = self._plan_route_by_locations(
                    origin,
                    destination,
                    route_type=route_type,
                    origin_city=city,
                    destination_city=city,
                )

                if route.distance > 0 and route.duration > 0:
                    route.description = f"[amap] {route.description}"
                    return route
            except Exception as exc:
                print(
                    f"Amap route unavailable for "
                    f"{origin_name}->{destination_name}: {exc}"
                )

        estimated = self.estimate_route_between_locations(
            origin_name=origin_name,
            origin=origin,
            destination_name=destination_name,
            destination=destination,
            route_type=route_type,
        )
        estimated.description = f"[estimated] {estimated.description}"
        return estimated

    def estimate_route_between_locations(
        self,
        origin_name: str,
        origin: Location,
        destination_name: str,
        destination: Location,
        route_type: str = "walking",
    ) -> RouteInfo:
        distance = self._haversine_meters(origin.latitude, origin.longitude, destination.latitude, destination.longitude)
        speed_mps = 1.15 if route_type == "walking" else 6.0 if route_type == "driving" else 4.0
        duration = int(distance / speed_mps)
        return RouteInfo(
            distance=round(distance, 1),
            duration=duration,
            route_type=route_type,
            description=f"{origin_name} -> {destination_name}, estimated by coordinates",
        )

    def geocode(self, address: str, city: Optional[str] = None) -> Optional[Location]:
        try:
            if not self.settings.amap_api_key:
                return None
            args: Dict[str, Any] = {"address": address}
            if city:
                args["city"] = city
            return self._parse_location(self._request("/geocode/geo", args))
        except Exception as exc:
            print(f"Geocode fallback for {address}: {exc}")
            return None

    def get_poi_detail(self, poi_id: str) -> Dict[str, Any]:
        try:
            return self._request("/place/detail", {"id": poi_id, "extensions": "all"})
        except Exception as exc:
            return {"error": str(exc)}

    def _request(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Call Amap with a hard timeout and validate its application status."""

        timeout_seconds = max(0.1, float(self.settings.amap_timeout_seconds))
        timeout = httpx.Timeout(timeout_seconds, connect=min(3.0, timeout_seconds))
        response = httpx.get(
            f"{AMAP_API_BASE_URL}{path}",
            params={**params, "key": self.settings.amap_api_key},
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Amap returned a non-object response")
        if str(data.get("status", "1")) != "1":
            info = data.get("info") or "unknown Amap error"
            infocode = data.get("infocode") or "unknown"
            raise RuntimeError(f"Amap rejected the request: {info} ({infocode})")
        return data

    def _plan_route_by_locations(
        self,
        origin: Location,
        destination: Location,
        route_type: str,
        origin_city: Optional[str] = None,
        destination_city: Optional[str] = None,
    ) -> RouteInfo:
        endpoint_map = {
            "walking": "/direction/walking",
            "driving": "/direction/driving",
            "transit": "/direction/transit/integrated",
        }
        normalized_type = route_type if route_type in endpoint_map else "walking"
        params: Dict[str, Any] = {
            "origin": f"{origin.longitude},{origin.latitude}",
            "destination": f"{destination.longitude},{destination.latitude}",
        }
        if normalized_type == "driving":
            params["extensions"] = "base"
        elif normalized_type == "transit":
            params["city"] = origin_city or destination_city or ""
            params["cityd"] = destination_city or origin_city or ""

        data = self._request(endpoint_map[normalized_type], params)
        self._log_raw_route_response(normalized_type, params, data)
        return self._parse_route(data, normalized_type)

    def _log_raw_route_response(
        self,
        route_type: str,
        params: Dict[str, Any],
        data: Dict[str, Any],
    ) -> None:
        enabled = os.getenv("AMAP_LOG_RAW_RESPONSE", "false").strip().lower()
        if enabled in {"0", "false", "no", "off"}:
            return
        text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        try:
            max_chars = max(1000, int(os.getenv("AMAP_LOG_MAX_CHARS", "50000")))
        except ValueError:
            max_chars = 50000
        if len(text) > max_chars:
            omitted = len(text) - max_chars
            text = f"{text[:max_chars]}\n... [高德响应已截断，省略 {omitted} 个字符]"
        logger.info(
            "[amap-route] RAW RESPONSE route_type=%s origin=%s destination=%s\n%s",
            route_type,
            params.get("origin", ""),
            params.get("destination", ""),
            text,
        )

    def _parse_pois(self, raw: str, keywords: str, city: str) -> List[POIInfo]:
        data = self._extract_json(raw)
        candidates = []
        if isinstance(data, dict):
            candidates = data.get("pois") or data.get("data") or data.get("results") or []
        elif isinstance(data, list):
            candidates = data

        pois: List[POIInfo] = []
        for index, item in enumerate(candidates[:30]):
            if not isinstance(item, dict):
                continue
            location = self._location_from_value(item.get("location")) or self._location_from_lonlat(
                item.get("longitude"), item.get("latitude")
            )
            if not location:
                location = self._fallback_location(city, index)
            biz_ext = item.get("biz_ext") if isinstance(item.get("biz_ext"), dict) else {}
            address = item.get("address")
            if not isinstance(address, str):
                address = city
            tel = item.get("tel")
            if isinstance(tel, list):
                tel = ";".join(str(value) for value in tel if value)
            elif not isinstance(tel, str):
                tel = None
            pois.append(
                POIInfo(
                    id=str(item.get("id") or item.get("poi_id") or ""),
                    name=str(item.get("name") or f"{city}{keywords}{index + 1}"),
                    type=str(item.get("type") or item.get("category") or keywords or "景点"),
                    address=address or city,
                    location=location,
                    tel=tel,
                    rating=self._safe_float(item.get("rating") or biz_ext.get("rating")),
                    ticket_price=self._estimate_ticket_price(str(item.get("type") or keywords)),
                )
            )
        return pois

    def _parse_weather(self, raw: str) -> List[WeatherInfo]:
        data = self._extract_json(raw)
        forecasts = []
        if isinstance(data, dict):
            forecasts = data.get("forecasts") or data.get("casts") or data.get("data") or []
            if forecasts and isinstance(forecasts[0], dict) and "casts" in forecasts[0]:
                forecasts = forecasts[0].get("casts") or []
        elif isinstance(data, list):
            forecasts = data

        weather: List[WeatherInfo] = []
        for item in forecasts[:7]:
            if not isinstance(item, dict):
                continue
            weather.append(
                WeatherInfo(
                    date=str(item.get("date") or item.get("reporttime") or ""),
                    day_weather=str(item.get("dayweather") or item.get("day_weather") or item.get("weather") or ""),
                    night_weather=str(item.get("nightweather") or item.get("night_weather") or ""),
                    day_temp=item.get("daytemp") or item.get("day_temp") or item.get("temperature") or 0,
                    night_temp=item.get("nighttemp") or item.get("night_temp") or 0,
                    wind_direction=str(item.get("daywind") or item.get("wind_direction") or ""),
                    wind_power=str(item.get("daypower") or item.get("wind_power") or ""),
                )
            )
        return weather

    def _parse_route(self, raw: Any, route_type: str) -> RouteInfo:
        data = self._extract_json(raw)
        if not isinstance(data, dict):
            return RouteInfo(route_type=route_type, description=raw[:200])

        route = data.get("route") if isinstance(data.get("route"), dict) else data
        paths = route.get("paths") or route.get("transits") or []
        first = paths[0] if paths and isinstance(paths[0], dict) else route
        distance = self._safe_float(first.get("distance") or route.get("distance")) or 0
        duration = int(self._safe_float(first.get("duration") or route.get("duration")) or 0)
        if route_type == "transit":
            return self._parse_transit_route(first, distance, duration)
        description = first.get("instruction") or first.get("strategy") or "Amap route parsed successfully"
        return RouteInfo(distance=distance, duration=duration, route_type=route_type, description=str(description))

    def _parse_transit_route(
        self,
        transit: Dict[str, Any],
        distance: float,
        duration: int,
    ) -> RouteInfo:
        """Preserve Amap transit/walking legs instead of flattening them."""

        steps: List[RouteStep] = []
        walking_distance = self._safe_float(transit.get("walking_distance")) or 0
        walking_duration = 0

        for segment in transit.get("segments") or []:
            if not isinstance(segment, dict):
                continue

            walking = segment.get("walking")
            if isinstance(walking, dict):
                leg_distance = self._safe_float(walking.get("distance")) or 0
                leg_duration = int(self._safe_float(walking.get("duration")) or 0)
                walking_duration += leg_duration
                instructions = [
                    str(item.get("instruction"))
                    for item in walking.get("steps") or []
                    if isinstance(item, dict) and item.get("instruction")
                ]
                if leg_distance > 0 or leg_duration > 0:
                    steps.append(
                        RouteStep(
                            mode="walking",
                            distance_meters=leg_distance,
                            duration_minutes=max(1, round(leg_duration / 60)) if leg_duration else 0,
                            instruction="；".join(instructions),
                        )
                    )

            bus = segment.get("bus")
            buslines = bus.get("buslines") if isinstance(bus, dict) else []
            busline = buslines[0] if buslines and isinstance(buslines[0], dict) else None
            if busline:
                name = str(busline.get("name") or "公交/地铁")
                line_type = str(busline.get("type") or "")
                mode = "subway" if "地铁" in f"{name}{line_type}" else "bus"
                leg_duration = int(self._safe_float(busline.get("duration")) or 0)
                departure = busline.get("departure_stop") or {}
                arrival = busline.get("arrival_stop") or {}
                steps.append(
                    RouteStep(
                        mode=mode,
                        name=name,
                        origin=str(departure.get("name") or "") if isinstance(departure, dict) else "",
                        destination=str(arrival.get("name") or "") if isinstance(arrival, dict) else "",
                        distance_meters=self._safe_float(busline.get("distance")) or 0,
                        duration_minutes=max(1, round(leg_duration / 60)) if leg_duration else 0,
                        instruction=f"乘坐{name}",
                    )
                )

            railway = segment.get("railway")
            if isinstance(railway, dict) and railway:
                leg_duration = int(self._safe_float(railway.get("time") or railway.get("duration")) or 0)
                steps.append(
                    RouteStep(
                        mode="railway",
                        name=str(railway.get("name") or railway.get("trip") or "铁路"),
                        distance_meters=self._safe_float(railway.get("distance")) or 0,
                        duration_minutes=max(1, round(leg_duration / 60)) if leg_duration else 0,
                        instruction="乘坐铁路",
                    )
                )

        if walking_distance <= 0:
            walking_distance = sum(step.distance_meters for step in steps if step.mode == "walking")
        transit_duration = max(0, duration - walking_duration)
        transit_names = list(
            dict.fromkeys(step.name for step in steps if step.mode != "walking" and step.name)
        )
        detail = f"公共交通 {max(1, round(duration / 60))} 分钟"
        detail += (
            f"（步行 {round(walking_duration / 60)} 分钟，"
            f"公交/地铁 {round(transit_duration / 60)} 分钟）"
        )
        detail += f"；步行 {walking_distance / 1000:.1f} km"
        if transit_names:
            detail += f"；线路：{' → '.join(transit_names)}"

        return RouteInfo(
            distance=distance,
            duration=duration,
            route_type="transit",
            walking_distance=walking_distance,
            walking_duration=walking_duration,
            transit_duration=transit_duration,
            steps=steps,
            description=detail,
        )

    def _parse_location(self, raw: str) -> Optional[Location]:
        data = self._extract_json(raw)
        if isinstance(data, dict):
            geocodes = data.get("geocodes") or data.get("data") or []
            first = geocodes[0] if geocodes and isinstance(geocodes[0], dict) else data
            return self._location_from_value(first.get("location"))
        return None

    def _extract_json(self, raw: Any) -> Any:
        if isinstance(raw, (dict, list)):
            return raw
        if not isinstance(raw, str):
            return None
        raw = raw.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        match = re.search(r"(\{.*\}|\[.*\])", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return None
        return None

    def _fallback_pois(self, city: str, keywords: str) -> List[POIInfo]:
        names_by_city = {
            "北京": [
                ("故宫博物院", "博物馆;文物古迹"),
                ("天坛公园", "公园;文物古迹"),
                ("颐和园", "公园;文物古迹"),
                ("国家博物馆", "博物馆"),
                ("什刹海", "湖泊;历史街区"),
                ("南锣鼓巷", "历史街区;商业街"),
            ],
            "上海": [
                ("外滩", "历史建筑"),
                ("上海博物馆", "博物馆"),
                ("豫园", "园林;文物古迹"),
                ("陆家嘴", "城市观光"),
                ("武康路", "历史街区"),
                ("静安寺", "寺庙;宗教场所"),
            ],
            "杭州": [
                ("西湖", "湖泊;自然风光"),
                ("灵隐寺", "寺庙;宗教场所"),
                ("浙江省博物馆", "博物馆"),
                ("河坊街", "历史街区;商业街"),
                ("西溪湿地", "湿地;自然风光"),
                ("龙井村", "乡村;自然风光"),
            ],
            "成都": [
                ("武侯祠", "文物古迹"),
                ("宽窄巷子", "历史街区;商业街"),
                ("杜甫草堂", "园林;文物古迹"),
                ("金沙遗址博物馆", "博物馆;遗址"),
                ("锦里", "历史街区;商业街"),
                ("人民公园", "公园"),
            ],
        }
        entries = names_by_city.get(city)
        if not entries:
            return []
        ranked_entries = sorted(
            enumerate(entries),
            key=lambda item: not (
                place_names_match(keywords, item[1][0]) or keywords in item[1][1]
            ),
        )
        pois = []
        for original_index, (name, category) in ranked_entries:
            pois.append(
                POIInfo(
                    id=f"fallback-{city}-{normalize_place_name(name)}",
                    name=name,
                    type=category,
                    address=f"{city}市中心区域",
                    location=self._fallback_location(city, original_index),
                    rating=4.5 - (original_index % 3) * 0.1,
                    ticket_price=self._estimate_ticket_price(category),
                )
            )
        return pois

    def _fallback_location(self, city: str, index: int) -> Location:
        centers = {
            "北京": (116.397128, 39.916527),
            "上海": (121.473701, 31.230416),
            "杭州": (120.15507, 30.274084),
            "成都": (104.066541, 30.572269),
            "广州": (113.264385, 23.129112),
            "深圳": (114.057868, 22.543099),
        }
        lon, lat = centers.get(city, (116.397128, 39.916527))
        return Location(longitude=lon + index * 0.018, latitude=lat + index * 0.012)

    def _location_from_value(self, value: Any) -> Optional[Location]:
        if isinstance(value, str) and "," in value:
            lon, lat = value.split(",", 1)
            return self._location_from_lonlat(lon, lat)
        if isinstance(value, dict):
            return self._location_from_lonlat(value.get("longitude") or value.get("lng"), value.get("latitude") or value.get("lat"))
        return None

    def _location_from_lonlat(self, lon: Any, lat: Any) -> Optional[Location]:
        try:
            return Location(longitude=float(lon), latitude=float(lat))
        except (TypeError, ValueError):
            return None

    def _safe_float(self, value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _estimate_ticket_price(self, category: str) -> int:
        if any(word in category for word in ["博物馆", "公园", "街区", "购物", "美食"]):
            return 0
        if any(word in category for word in ["寺", "园", "景区", "自然"]):
            return 40
        return 60

    def _haversine_meters(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        radius = 6371000
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)
        a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
        return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


_amap_service = None


def get_amap_service() -> AmapService:
    global _amap_service

    if _amap_service is None:
        _amap_service = AmapService()

    return _amap_service
