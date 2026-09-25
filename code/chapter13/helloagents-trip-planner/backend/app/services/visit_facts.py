"""Small, dated official facts. Unknown prices must not be inferred from category."""
from datetime import date
from .place_name_service import normalize_place_name

VERIFIED_ON = date(2026, 9, 25)
VALID_UNTIL = date(2026, 12, 24)
PALACE_SOURCE = "https://www.dpm.org.cn/subject_booking/"


def apply_visit_facts(attraction, city, visit_date):
    day = date.fromisoformat(visit_date)
    name = normalize_place_name(attraction.name)
    # Only these dated facts are authoritative here; generic positive values remain estimates.
    if city == "北京" and VERIFIED_ON <= day <= VALID_UNTIL:
        if name == "故宫博物院":
            high = 4 <= day.month <= 10
            attraction.ticket_price = 60 if high else 40
            attraction.ticket_price_status = "official_reference"
            attraction.ticket_price_source = PALACE_SOURCE
            attraction.ticket_price_note = "成人大门票参考价；不含专馆，优惠资格另核；2026-09-25核验，出行前复核"
            attraction.opening_time = "08:30"
            attraction.closing_time = "17:00" if high else "16:30"
            attraction.latest_entry_time = "16:00" if high else "15:30"
            attraction.hours_source = "official_reference"
            attraction.access_note = "午门入，神武门或东华门出；上下车点及临时管制需核实"
            return
        if name == "中国国家博物馆":
            attraction.ticket_price = 0
            attraction.ticket_price_status = "confirmed_free"
            attraction.ticket_price_source = "https://pcticket.chnmuseum.cn/museum-en/"
            attraction.ticket_price_note = "常规入馆免费但需预约；特展另核；2026-09-25核验，出行前复核"
            return
    if attraction.ticket_price_status in {"official_reference", "confirmed_free"}:
        attraction.ticket_price = 0
        attraction.ticket_price_status = "unknown"
        attraction.ticket_price_note = "参考资料超出核验有效期，需重新核实"
        if attraction.hours_source == "official_reference":
            attraction.opening_time = None
            attraction.closing_time = None
            attraction.latest_entry_time = None
            attraction.hours_source = "unknown"
    elif attraction.ticket_price > 0:
        attraction.ticket_price_status = "estimate"
        attraction.ticket_price_note = "规划估价，尚未核实实际票种和价格"


def resolve_access_points(attraction, city, amap):
    if city != "北京" or normalize_place_name(attraction.name) != "故宫博物院":
        return
    if not getattr(getattr(amap, "settings", None), "amap_api_key", ""):
        return
    from .spatial_planner import haversine_meters
    for field, gate in (("entrance_location", "午门"), ("exit_location", "神武门")):
        if getattr(attraction, field) is not None:
            continue
        try:
            matches = [p for p in amap.search_poi(f"故宫 {gate}", city)
                       if p.id and not p.id.startswith("fallback-")
                       and p.name in {gate, f"故宫博物院-{gate}", f"故宫-{gate}"}
                       and haversine_meters(p.location, attraction.location) < 2000]
            if matches:
                setattr(attraction, field, matches[0].location)
        except Exception:
            pass
