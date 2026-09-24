"""Read-only LiteAPI hotel content and date/occupancy-specific offers."""
from datetime import datetime, timezone
import logging
import math
import httpx

from ..config import settings
from ..models.schemas import Hotel
from .trip_cost_service import stay_nights
from .accommodation_selector import AccommodationSelector

logger = logging.getLogger("uvicorn.error")


class HotelQuoteService:
    def __init__(self, client=None):
        self.client = client

    def search(self, request, amap):
        if settings.hotel_api_provider != "liteapi" or not settings.liteapi_api_key:
            return [], "酒店报价服务未启用，当前为规划估价"
        if stay_nights(request) == 0:
            return [], "一日游不查询过夜报价"
        if request.room_count > request.party_size:
            return [], "房间数超过人数，需确认每间入住人数后查询报价"
        if self.client is not None:
            return self._search(request, amap, self.client)
        try:
            with httpx.Client(base_url=settings.liteapi_base_url.rstrip("/") + "/",
                              headers={"X-API-Key": settings.liteapi_api_key.strip()}, timeout=18) as client:
                return self._search(request, amap, client)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.warning("Hotel quote provider returned HTTP %s", status)
            return [], f"酒店报价接口返回 HTTP {status}，当前使用估价；请核对服务配置"
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            logger.warning("Hotel quote provider unavailable or returned invalid data")
            return [], "酒店报价暂不可用，当前为规划估价"

    def _search(self, request, amap, client):
        city = {"北京": "Beijing", "上海": "Shanghai", "杭州": "Hangzhou", "成都": "Chengdu"}.get(request.city, request.city)
        tier = AccommodationSelector._tier(request.accommodation)
        params = {"countryCode": "CN", "cityName": city, "limit": 10}
        if tier == "luxury":
            params["starRating"] = "5.0"
        elif tier == "comfortable":
            params["starRating"] = "4.0"
        response = client.get("data/hotels", params=params)
        response.raise_for_status()
        hotels = response.json().get("data", [])
        metadata = {str(item["id"]): item for item in hotels if item.get("id") and item.get("name")}
        if not metadata:
            return [], "未获取符合地区和档次的酒店，当前为地图估价"
        occupancies = [{"adults": request.party_size // request.room_count + int(index < request.party_size % request.room_count)}
                       for index in range(request.room_count)]
        response = client.post("hotels/rates", json={"hotelIds": list(metadata), "checkin": request.start_date,
                               "checkout": request.end_date, "currency": "CNY", "guestNationality": "CN",
                               "occupancies": occupancies, "maxRatesPerHotel": 1, "timeout": 6})
        response.raise_for_status()
        payload = response.json()
        sandbox = settings.liteapi_environment != "production" or bool(payload.get("sandbox"))
        result = []
        for item in payload.get("data", []):
            meta = metadata.get(str(item.get("hotelId")))
            if not meta:
                continue
            offers = []
            for offer in item.get("roomTypes", []):
                money = offer.get("offerRetailRate", {})
                if money.get("currency") != "CNY":
                    continue
                total = float(money.get("amount", 0))
                valid = math.isfinite(total) and total > 0 and bool(offer.get("rates"))
                for rate in offer.get("rates", []):
                    for fee in rate.get("retailRate", {}).get("taxesAndFees", []):
                        if fee.get("included") is False:
                            if fee.get("currency") != "CNY" or fee.get("amount") is None:
                                valid = False
                            else:
                                total += float(fee["amount"])
                if valid and math.isfinite(total):
                    offers.append(total)
            if not offers:
                continue
            total = min(offers)
            stars = float(meta.get("stars") or 0)
            if tier == "luxury" and stars < 5:
                continue
            location = None
            if getattr(getattr(amap, "settings", None), "amap_api_key", ""):
                # Routing uses AMap coordinates, not raw supplier WGS84 coordinates.
                location = amap.geocode(f"{meta['name']} {meta.get('address', '')}", request.city)
            result.append(Hotel(name=meta["name"], address=meta.get("address", ""), location=location,
                                type="豪华酒店" if stars >= 5 else "舒适型酒店" if stars >= 4 else "经济型酒店" if stars else "等级未知",
                                star_rating=stars or None, tier_source="liteapi",
                                estimated_cost=math.ceil(total / stay_nights(request) / request.room_count),
                                price_source="sandbox_quote" if sandbox else "provider_quote",
                                price_note=("沙箱测试报价，不可据此预订" if sandbox else "供应商查询报价，预订前需复核") + "；按成人分配房间，含返回的未含税费，餐食及取消条件请向供应商确认",
                                provider_hotel_id=str(item["hotelId"]), quoted_total=total,
                                quote_checkin=request.start_date, quote_checkout=request.end_date,
                                quote_rooms=request.room_count, quote_party_size=request.party_size,
                                quote_timestamp=datetime.now(timezone.utc).isoformat()))
        return result, "" if result else "酒店无可用人民币报价，当前为规划估价"
