"""Classify provider records before applying landmark metadata."""

def venue_kind(name: str, provider_type: str = "") -> str:
    text = f"{name} {provider_type}".lower()
    if any(word in text for word in ("建设中", "施工中", "暂不开放", "已关闭", "暂停营业")):
        return "unavailable"
    if any(word in text for word in ("医院", "医疗", "诊所", "卫生院", "hospital", "clinic")):
        return "medical"
    if any(word in text for word in ("餐饮", "餐厅", "餐馆", "饭店", "烤鸭", "涮肉", "火锅", "小吃", "咖啡", "restaurant", "food")):
        return "restaurant"
    if any(word in text for word in ("交通设施", "道路名", "地铁站", "公交站", "停车场", "派出所", "文物科技保护中心", "售票处", "出入口")):
        return "infrastructure"
    if any(word in text for word in ("住宿服务", "酒店", "宾馆", "旅馆", "hotel")):
        return "hotel"
    return "attraction"
