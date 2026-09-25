"""Conservative venue classification; an unrecognized record is not a sight."""

_NON_VISIT_TYPES = (
    ("infrastructure", ("充电站", "充电桩", "换电站", "加油站", "汽车服务", "汽车维修", "charging station")),
    ("medical", ("医疗", "医院", "诊所", "卫生院", "hospital", "clinic")),
    ("hotel", ("住宿", "酒店", "宾馆", "旅馆", "hotel")),
    ("restaurant", ("餐饮", "餐厅", "餐馆", "restaurant", "food")),
    ("infrastructure", ("交通设施", "道路名", "地铁站", "公交站", "停车场", "售票处", "出入口", "附属设施")),
    ("other", ("公司企业", "商务住宅", "金融保险", "购物服务", "生活服务", "学校", "政府机构")),
)
_VISIT_TYPES = ("风景名胜", "公园", "博物馆", "美术馆", "纪念馆", "科技馆", "动物园", "植物园", "游乐园", "寺庙", "教堂", "旅游景点")
_INTERNAL_VISIT_TYPES = {"景点", "历史文化", "自然风光", "attraction", "historic", "culture", "museum", "park", "natural", "temple", "shopping", "amusement", "zoo"}


def venue_kind(name: str, provider_type: str = "") -> str:
    """Prefer specific provider types; use name heuristics only as a fallback.

    Generic internal labels such as '景点' do not override a hospital name.
    Names are hints, not proof of opening hours or public access.
    """
    name = (name or "").lower()
    provider_type = (provider_type or "").strip().lower()
    text = f"{name} {provider_type}"
    if any(word in text for word in ("建设中", "施工中", "暂不开放", "已关闭", "暂停营业")):
        return "unavailable"
    if any(word in text for word in ("充电站", "充电桩", "换电站", "加油站", "charging station")):
        return "infrastructure"
    for kind, terms in _NON_VISIT_TYPES:
        if any(term in provider_type for term in terms):
            # Shopping venues can be intentional visits; arbitrary shops cannot.
            if kind == "other" and "购物服务" in provider_type and any(term in provider_type for term in ("商场", "购物中心", "特色商业街")):
                return "attraction"
            return kind
    # Entrances and ticket offices are navigation stops, even inside a scenic area.
    if any(term in name for term in ("地铁站", "公交站", "停车场", "派出所", "文物科技保护中心", "售票处", "出入口")) or name.endswith(("入口", "出口")):
        return "infrastructure"
    if any(term in provider_type for term in _VISIT_TYPES):
        return "attraction"
    for kind, terms in _NON_VISIT_TYPES:
        if any(term in name for term in terms):
            return kind
    if any(term in name for term in ("饭店", "烤鸭", "涮肉", "火锅", "小吃", "咖啡")):
        return "restaurant"
    if provider_type in _INTERNAL_VISIT_TYPES:
        return "attraction"
    # Explicit aliases support name-only callers without substring promotion.
    from .place_name_service import is_known_attraction_name
    if is_known_attraction_name(name) or any(term in name for term in _VISIT_TYPES):
        return "attraction"
    return "unknown"
