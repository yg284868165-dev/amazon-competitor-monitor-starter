from __future__ import annotations

import json
from typing import Any

from app.config import load_competitors
from app.storage.database import Database

SEVERITY_LABELS = {"high": "高", "medium": "中", "low": "低"}
SOURCE_LABELS = {"product": "商品详情", "search": "关键词排名", "bestseller": "BSR榜单"}
FIELD_LABELS = {
    "current_price": "当前价格", "list_price": "划线价", "coupon_text": "优惠券",
    "deal_text": "促销活动", "business_price_text": "企业价",
    "rating_count": "评价数量", "rating_value": "星级",
    "main_bsr": "大类目BSR", "amazon_choice": "亚马逊精选标识",
    "best_seller": "畅销商品标识", "new_release": "新品标识",
    "high_return_rate": "高退货率标签", "listing_status": "商品页面状态",
    "main_category_name": "大类目", "subcategory_names": "小类目",
    "availability": "库存状态", "highlights_hash": "副标题（Highlights）",
    "featured_seller": "购物车卖家", "ships_from": "发货方", "offer_count": "跟卖数量",
    "variation_count": "变体数量", "title_hash": "标题", "image_hash": "主图",
    "about_items_hash": "五点描述", "product_images_hash": "产品图片集",
    "organic_rank": "自然排名", "ad_rank": "广告位排名", "rank": "BSR排名",
}


def _short(value: Any, limit: int = 42) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _transition_path(row: dict[str, Any]) -> list[Any]:
    transitions = row.get("_transitions") or [{
        "old_value": row.get("old_value"), "new_value": row.get("new_value"),
    }]
    values = [transitions[0].get("old_value"), *(item.get("new_value") for item in transitions)]
    result = []
    for value in values:
        if not result or str(result[-1]) != str(value):
            result.append(value)
    return result


def _path_text(values: list[Any]) -> str:
    return " → ".join(_short(value, 30) or "无" for value in values)


def _event_time(value: Any) -> str:
    text = str(value or "")
    return text[11:16] if len(text) >= 16 else ""


def format_rating_breakdown(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return ""
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("rating_breakdown"), dict):
        value = value["rating_breakdown"]
    parts = []
    for star in range(5, 0, -1):
        percentage = value.get(str(star), value.get(star))
        if percentage is None:
            continue
        try:
            number = float(percentage)
            display = f"{number:g}"
        except (TypeError, ValueError):
            display = str(percentage).strip().rstrip("%")
        parts.append(f"{star}星 {display}%")
    return "、".join(parts)


def format_product_identity(asin: str, internal_name: str = "", brand: str = "", title: str = "") -> str:
    asin = str(asin or "").strip().upper()
    internal_name, brand, title = _short(internal_name), _short(brand, 24), _short(title)
    if internal_name:
        parts = [internal_name]
        if brand and brand.casefold() not in internal_name.casefold():
            parts.append(brand)
    elif title:
        primary = title
        if brand and brand.casefold() not in title.casefold():
            primary = f"{brand} {title}"
        parts = [primary]
    elif brand:
        parts = [brand]
    else:
        parts = []
    if asin:
        parts.append(asin)
    return "｜".join(parts) or "未知商品"


def build_product_identities(db: Database, project_id: str) -> dict[str, str]:
    configured = {item.asin: item for item in load_competitors(project_id)}
    product_rows = db.fetchall("""
        SELECT p.asin,p.brand,p.title FROM product_snapshots p
        JOIN (SELECT asin,MAX(collected_at) latest FROM product_snapshots
              WHERE project_id=? AND success=1 GROUP BY asin) x
          ON x.asin=p.asin AND x.latest=p.collected_at
        WHERE p.project_id=? AND p.success=1
    """, (project_id, project_id))
    bestseller_rows = db.fetchall("""
        SELECT b.asin,b.brand,b.title FROM bestseller_snapshots b
        JOIN (SELECT asin,MAX(collected_at) latest FROM bestseller_snapshots
              WHERE project_id=? GROUP BY asin) x
          ON x.asin=b.asin AND x.latest=b.collected_at
        WHERE b.project_id=?
    """, (project_id, project_id))
    candidate_rows = db.fetchall("SELECT asin,brand,title FROM bsr_new_candidates WHERE project_id=?", (project_id,))
    observed = {row["asin"]: row for row in bestseller_rows}
    observed.update({row["asin"]: row for row in candidate_rows})
    observed.update({row["asin"]: row for row in product_rows})
    asins = set(configured) | set(observed)
    result = {}
    for asin in asins:
        item, row = configured.get(asin), observed.get(asin, {})
        result[asin] = format_product_identity(
            asin,
            item.internal_name if item else "",
            (item.brand if item else "") or str(row.get("brand") or ""),
            str(row.get("title") or ""),
        )
    return result


def format_change_summary(row: dict[str, Any]) -> str:
    old, new = row.get("old_value"), row.get("new_value")
    field = FIELD_LABELS.get(row.get("field_name"), row.get("field_name") or "变化")
    if row.get("event_type") == "entered_bestseller":
        return f"新进入 {row.get('category_name') or 'BSR前100'}，目前第{new}名"
    if row.get("event_type") == "new_competitor_found":
        return str(row.get("message") or f"发现未监控的同类新竞品，目前第{new}名")
    if row.get("field_name") in {"organic_rank", "ad_rank", "main_bsr", "rank"}:
        try:
            old_rank, new_rank = int(float(old)), int(float(new))
            direction = "提升" if new_rank < old_rank else "下降"
            context = f"“{row['keyword']}”" if row.get("keyword") else ""
            return f"{context}{field}从{old_rank}名{direction}到{new_rank}名"
        except (TypeError, ValueError):
            pass
    if row.get("field_name") == "rating_value":
        summary = f"星级：{_short(old, 12) or '无'} → {_short(new, 12) or '无'}"
        breakdown = format_rating_breakdown(row.get("details_json"))
        return f"{summary}；当前各星级评价占比：{breakdown}" if breakdown else summary
    path = _transition_path(row)
    if row.get("field_name") == "current_price" and len(path) >= 2:
        try:
            prices = [float(value) for value in path if value is not None]
            start, end, lowest = prices[0], prices[-1], min(prices)
            if start == end and lowest < start:
                low_time = ""
                for item in row.get("_transitions") or []:
                    try:
                        if float(item.get("new_value")) == lowest:
                            low_time = _event_time(item.get("event_time"))
                            break
                    except (TypeError, ValueError):
                        continue
                suffix = f"，最低价首次出现在{low_time}" if low_time else ""
                return f"日内短时降价：${start:g} → 最低${lowest:g} → ${end:g}{suffix}"
            detail = f"当前价格：${start:g} → ${end:g}"
            if lowest < min(start, end):
                detail += f"（日内最低${lowest:g}）"
            return detail
        except (TypeError, ValueError):
            pass
    if row.get("field_name") == "offer_count" and len(path) >= 2:
        counts = [int(float(value)) for value in path if value not in {None, ""}]
        if counts:
            maximum, final = max(counts), counts[-1]
            transitions = row.get("_transitions") or []
            appeared = next((item for item in transitions if item.get("new_value") not in {None, "", 0, "0"}), None)
            disappeared = next((item for item in transitions if item.get("new_value") in {0, "0"}), None)
            if path[0] in {None, "", 0, "0"} and final == 0 and appeared and disappeared:
                appear_time, disappear_time = _event_time(appeared.get("event_time")), _event_time(disappeared.get("event_time"))
                timing = f"{appear_time}发现" if appear_time else "发现"
                gone = f"，{disappear_time}确认消失" if disappear_time else "，随后确认消失"
                return f"跟卖动态：{timing}{maximum}个跟卖{gone}"
            if final == 0:
                return f"跟卖已消失（此前最多{maximum}个）"
            if path[0] in {None, "", 0, "0"}:
                return f"发现{final}个跟卖"
            if str(path[0]) == str(path[-1]) and len(path) > 2:
                return f"跟卖数量日内波动：{_path_text(path)}"
    if row.get("field_name") == "availability":
        if "断货" in path and path[-1] == "有货":
            return "日内曾断货，随后恢复有货"
        if path[-1] == "断货":
            return f"发生断货（原状态：{_short(path[0]) or '未知'}）"
        if path[-1] == "有货" and path[0] in {"断货", "低库存"}:
            return f"恢复有货（原状态：{path[0]}）"
        if path[-1] == "低库存":
            return f"进入低库存（原状态：{_short(path[0]) or '未知'}）"
    if row.get("field_name") == "high_return_rate":
        states = [str(value) for value in path]
        if len(states) > 2 and states[-1] in {"0", "0.0"} and any(value in {"1", "1.0"} for value in states[1:-1]):
            return "日内曾出现高退货率标签，随后消失"
        if states[-1] in {"1", "1.0"}:
            return "出现高退货率标签"
        return "高退货率标签已消失"
    if row.get("field_name") == "listing_status":
        states = [str(value) for value in path]
        if len(states) > 2 and states[-1] == "正常在售":
            if "页面变狗" in states[1:-1]:
                return "日内曾变狗，随后恢复正常"
            if "商品下架" in states[1:-1]:
                return "日内曾下架，随后恢复正常"
        if states[-1] == "页面变狗":
            return "商品页面变狗"
        if states[-1] == "商品下架":
            return "商品已下架"
        if states[-1] == "正常在售" and states[0] == "商品下架":
            return "商品已重新上架"
        if states[-1] == "正常在售":
            return "商品页面已恢复正常"
    if row.get("field_name") in {"main_category_name", "subcategory_names"}:
        if len(path) > 2:
            return f"{field}日内变化：{_path_text(path)}"
        if not old:
            return f"新增{field}：{_short(new, 80)}"
        if not new:
            return f"{field}已不再显示（原：{_short(old, 80)}）"
        return f"{field}：{_short(old, 80)} → {_short(new, 80)}"
    if row.get("field_name") in {"coupon_text", "deal_text", "business_price_text", "featured_seller", "ships_from"} and len(path) > 2:
        return f"{field}日内变化：{_path_text(path)}"
    if row.get("field_name") == "coupon_text" and old and not new:
        return f"优惠券活动已取消（原：{_short(old, 55)}）"
    if row.get("field_name") == "deal_text" and old and not new:
        return f"促销活动已取消（原：{_short(old, 55)}）"
    if row.get("field_name") == "business_price_text" and old and not new:
        return f"企业价已取消（原：{_short(old, 55)}）"
    if row.get("field_name") == "business_price_text" and not old and new:
        return f"出现企业价：{_short(new, 70)}"
    if row.get("field_name") == "image_hash":
        return "主图变化"
    return f"{field}：{_short(old, 55) or '无'} → {_short(new, 55) or '无'}"
