from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from app.config import load_competitors, load_keywords
from app.presentation import FIELD_LABELS, format_change_summary, format_product_identity
from app.storage.database import Database


ACTION_CATEGORIES = {
    "current_price": "price_promo", "list_price": "price_promo",
    "coupon_text": "price_promo", "deal_text": "price_promo",
    "business_price_text": "price_promo",
    "title_hash": "listing", "image_hash": "listing",
    "highlights_hash": "listing", "about_items_hash": "listing",
    "product_images_hash": "listing", "variation_count": "listing",
    "main_category_name": "listing", "subcategory_names": "listing",
    "availability": "operations", "featured_seller": "operations",
    "ships_from": "operations", "offer_count": "operations",
    "listing_status": "operations", "high_return_rate": "operations",
    "amazon_choice": "platform", "best_seller": "platform", "new_release": "platform",
}
CATEGORY_LABELS = {
    "price_promo": "价格与促销", "listing": "Listing内容",
    "operations": "销售状态", "platform": "平台标识",
}
DIRECT_ACTION_CATEGORIES = {"price_promo", "listing", "operations"}
LISTING_FIELDS = {
    "title_hash", "image_hash", "highlights_hash", "about_items_hash",
    "product_images_hash", "variation_count", "main_category_name", "subcategory_names",
}
IMAGE_FIELDS = {"image_hash", "product_images_hash"}
LIST_FIELDS = {"about_items_hash", "product_images_hash"}
IMPACT_DAYS = (1, 3, 7)
CONTENT_SUMMARIES = {
    "title_hash": "标题内容变化",
    "image_hash": "主图变化",
    "highlights_hash": "副标题（Highlights）变化",
    "about_items_hash": "五点描述变化",
    "product_images_hash": "产品图片集变化",
}


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _rank_value(value: Any) -> int | None:
    """Normalize legacy SQLite rank values without letting one bad cell break a file."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _safe_images(value: Any) -> list[str]:
    parsed = _json_value(value)
    values = parsed if isinstance(parsed, list) else [parsed]
    result = []
    for item in values:
        text = str(item or "").strip()
        if text.startswith(("https://", "http://")) and text not in result:
            result.append(text)
    return result


def _display_value(field: str, value: Any) -> Any:
    if field in IMAGE_FIELDS:
        return _safe_images(value)
    if field in LIST_FIELDS:
        parsed = _json_value(value)
        return parsed if isinstance(parsed, list) else ([str(parsed)] if parsed else [])
    if value is None:
        return ""
    return str(value)


def _display_type(field: str) -> str:
    if field in IMAGE_FIELDS:
        return "images"
    if field in LIST_FIELDS:
        return "list"
    if field in {"title_hash", "highlights_hash"}:
        return "text"
    return "value"


def _action_title(categories: set[str]) -> str:
    direct = categories & DIRECT_ACTION_CATEGORIES
    if len(direct) > 1:
        return "、".join(CATEGORY_LABELS[item] for item in ("price_promo", "listing", "operations") if item in direct) + "组合变化"
    if "price_promo" in categories:
        return "价格与促销调整"
    if "listing" in categories:
        return "Listing内容调整"
    if "operations" in categories:
        return "销售状态变化"
    return "Amazon平台标识变化"


def _event_summary(row: dict[str, Any]) -> str:
    return CONTENT_SUMMARIES.get(str(row.get("field_name"))) or format_change_summary(row)


def _category_rank_name(row: dict[str, Any]) -> str:
    parsed = _json_value(row.get("category_ranks_json"))
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        return str(parsed[0].get("category") or "大类目BSR")
    return "大类目BSR"


def _build_actions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in events:
        grouped.setdefault(str(row["run_id"]), []).append(row)
    actions = []
    for run_id, rows in grouped.items():
        rows.sort(key=lambda item: str(item["event_time"]))
        categories = {ACTION_CATEGORIES[str(item["field_name"])] for item in rows}
        items = []
        for row in rows:
            field = str(row["field_name"])
            items.append({
                "field": field,
                "label": FIELD_LABELS.get(field, field),
                "category": ACTION_CATEGORIES[field],
                "summary": _event_summary(row),
                "old": _display_value(field, row.get("old_value")),
                "new": _display_value(field, row.get("new_value")),
                "display_type": _display_type(field),
                "is_listing_change": field in LISTING_FIELDS,
            })
        actions.append({
            "run_id": run_id,
            "observed_at": rows[0]["event_time"],
            "title": _action_title(categories),
            "categories": [item for item in ("price_promo", "listing", "operations", "platform") if item in categories],
            "category_labels": [CATEGORY_LABELS[item] for item in ("price_promo", "listing", "operations", "platform") if item in categories],
            "items": items,
            "is_direct_action": bool(categories & DIRECT_ACTION_CATEGORIES),
        })
    return sorted(actions, key=lambda item: str(item["observed_at"]), reverse=True)


def _before_point(samples: list[dict[str, Any]], observed: datetime, run_id: str) -> dict[str, Any]:
    matching = [item for item in samples if item["time"] < observed and item.get("run_id") != run_id and item.get("success", True)]
    if not matching:
        return {"status": "no_data", "value": None, "observed_at": None}
    return _sample_point(matching[-1])


def _after_point(samples: list[dict[str, Any]], target: datetime, now: datetime) -> dict[str, Any]:
    if target > now:
        return {"status": "pending", "value": None, "observed_at": None}
    matching = [item for item in samples if item["time"] >= target and item.get("success", True)]
    if not matching:
        return {"status": "no_data", "value": None, "observed_at": None}
    return _sample_point(matching[0])


def _sample_point(sample: dict[str, Any]) -> dict[str, Any]:
    value = sample.get("value")
    return {
        "status": "ranked" if value is not None else sample.get("missing_status", "unranked"),
        "value": value,
        "observed_at": sample["time"].isoformat(timespec="seconds"),
    }


def _category_rank_samples(rows: list[dict[str, Any]]) -> list[tuple[str, str, list[dict[str, Any]]]]:
    category_types: dict[str, str] = {}
    for row in rows:
        parsed = _json_value(row.get("category_ranks_json"))
        if not isinstance(parsed, list):
            continue
        for index, item in enumerate(parsed):
            if isinstance(item, dict) and item.get("category"):
                category_types.setdefault(
                    str(item["category"]), "main_bsr" if index == 0 else "subcategory_bsr",
                )
    if not category_types and any(row.get("main_bsr") is not None for row in rows):
        category_types["大类目BSR"] = "main_bsr"

    result = []
    for category, metric in category_types.items():
        samples = []
        for row in rows:
            observed = _parse_time(row.get("collected_at"))
            if observed is None:
                continue
            parsed = _json_value(row.get("category_ranks_json"))
            values = {
                str(item.get("category")): item.get("rank")
                for item in parsed if isinstance(item, dict) and item.get("category")
            } if isinstance(parsed, list) else {}
            value = values.get(category)
            if metric == "main_bsr" and category == "大类目BSR" and value is None:
                value = row.get("main_bsr")
            value = _rank_value(value)
            samples.append({
                "time": observed, "run_id": row["run_id"], "value": value,
                "success": bool(row.get("success")), "missing_status": "not_observed",
            })
        result.append((metric, category, sorted(samples, key=lambda item: item["time"])))
    return result


def _search_rank_samples(
    outcomes: list[dict[str, Any]], ranks: dict[tuple[str, str], int], keyword: str,
) -> list[dict[str, Any]]:
    result = []
    for row in outcomes:
        if row["target"] != keyword:
            continue
        observed = _parse_time(row.get("finished_at"))
        if observed is None:
            continue
        result.append({
            "time": observed, "run_id": row["run_id"],
            "value": ranks.get((row["run_id"], keyword)), "success": bool(row["success"]),
        })
    return sorted(result, key=lambda item: item["time"])


def _build_impacts(
    actions: list[dict[str, Any]], products: list[dict[str, Any]],
    search_outcomes: list[dict[str, Any]], organic_ranks: dict[tuple[str, str], int],
    keywords: list[str], now: datetime,
) -> list[dict[str, Any]]:
    category_samples = _category_rank_samples(products)
    search_samples = {
        keyword: _search_rank_samples(search_outcomes, organic_ranks, keyword)
        for keyword in keywords
    }
    rows = []
    for action in actions:
        if not set(action["categories"]) & DIRECT_ACTION_CATEGORIES:
            continue
        observed = _parse_time(action["observed_at"])
        if observed is None:
            continue
        # The broad main-category rank is too noisy and not useful for this
        # action-level comparison. Keep only subcategory BSR and organic rank.
        metrics = [item for item in category_samples if item[0] != "main_bsr"]
        metrics.extend(("organic_rank", keyword, search_samples[keyword]) for keyword in keywords)
        for metric, context, samples in metrics:
            points = {
                "before": _before_point(samples, observed, action["run_id"]),
                **{f"day_{days}": _after_point(samples, observed + timedelta(days=days), now) for days in IMPACT_DAYS},
            }
            if all(point["status"] == "no_data" for point in points.values()):
                continue
            rows.append({
                "run_id": action["run_id"], "observed_at": action["observed_at"],
                "action": action["title"], "metric": metric,
                "metric_label": {
                    "main_bsr": "大类目BSR", "subcategory_bsr": "小类目BSR",
                    "organic_rank": "关键词自然位",
                }[metric],
                "context": context, "points": points,
            })
    return rows


def _background_summary(products: list[dict[str, Any]]) -> dict[str, Any]:
    def values(field: str, cast=float) -> list[Any]:
        result = []
        for row in products:
            value = row.get(field)
            if value is not None:
                try:
                    result.append(cast(value))
                except (TypeError, ValueError):
                    continue
        return result

    prices, bsr, ratings = values("current_price"), values("main_bsr", int), values("rating_count", int)
    return {
        "price": {"start": prices[0] if prices else None, "end": prices[-1] if prices else None, "min": min(prices) if prices else None, "max": max(prices) if prices else None},
        "main_bsr": {"start": bsr[0] if bsr else None, "end": bsr[-1] if bsr else None, "best": min(bsr) if bsr else None, "worst": max(bsr) if bsr else None},
        "rating_count": {"start": ratings[0] if ratings else None, "end": ratings[-1] if ratings else None, "change": ratings[-1] - ratings[0] if ratings else None},
    }


def build_asin_history(
    db: Database, project_id: str, asin: str, days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = (now or datetime.now()).replace(microsecond=0)
    if days not in {7, 14, 30}:
        raise ValueError("历史范围只能选择7天、14天或30天")
    normalized_asin = str(asin or "").strip().upper()
    competitors = {
        item.asin: item for item in load_competitors(project_id, include_disabled=True)
    }
    if normalized_asin not in competitors:
        raise ValueError("该ASIN不在所选产品项目的竞品列表中")
    cutoff = current_time - timedelta(days=days)
    cutoff_text, end_text = cutoff.isoformat(timespec="seconds"), current_time.isoformat(timespec="seconds")

    products = db.fetchall(
        """SELECT * FROM product_snapshots WHERE project_id=? AND asin=?
           AND datetime(collected_at)>=datetime(?) AND datetime(collected_at)<=datetime(?)
           AND (success=1 OR listing_status IN ('dog','removed')) ORDER BY collected_at""",
        (project_id, normalized_asin, cutoff_text, end_text),
    )
    latest_rows = db.fetchall(
        """SELECT * FROM product_snapshots WHERE project_id=? AND asin=?
           AND (success=1 OR listing_status IN ('dog','removed')) ORDER BY collected_at DESC LIMIT 1""",
        (project_id, normalized_asin),
    )
    latest = latest_rows[0] if latest_rows else {}
    event_fields = tuple(ACTION_CATEGORIES)
    placeholders = ",".join("?" for _ in event_fields)
    events = db.fetchall(
        f"""SELECT * FROM change_events WHERE project_id=? AND asin=? AND confirmed=1
            AND datetime(event_time)>=datetime(?) AND datetime(event_time)<=datetime(?)
            AND field_name IN ({placeholders}) ORDER BY event_time""",
        (project_id, normalized_asin, cutoff_text, end_text, *event_fields),
    )
    actions = _build_actions(events)

    keyword_names = [item.keyword for item in load_keywords(project_id=project_id)]
    search_outcomes = db.fetchall(
        """SELECT run_id,target,success,finished_at FROM collection_task_outcomes
           WHERE project_id=? AND task_type='search'
           AND datetime(finished_at)>=datetime(?) AND datetime(finished_at)<=datetime(?)
           ORDER BY finished_at""",
        (project_id, cutoff_text, end_text),
    )
    search_rows = db.fetchall(
        """SELECT run_id,keyword,organic_rank,ad_rank,is_sponsored,collected_at
           FROM search_snapshots WHERE project_id=? AND asin=?
           AND datetime(collected_at)>=datetime(?) AND datetime(collected_at)<=datetime(?)""",
        (project_id, normalized_asin, cutoff_text, end_text),
    )
    organic_ranks: dict[tuple[str, str], int] = {}
    ad_ranks: dict[tuple[str, str], int] = {}
    for row in search_rows:
        for field, target in (("organic_rank", organic_ranks), ("ad_rank", ad_ranks)):
            value = _rank_value(row.get(field))
            if value is None:
                continue
            key = (row["run_id"], row["keyword"])
            if key not in target or value < target[key]:
                target[key] = value

    category_counts = Counter(category for action in actions for category in set(action["categories"]))
    direct_actions = [item for item in actions if item["is_direct_action"]]
    competitor = competitors[normalized_asin]
    identity = format_product_identity(
        normalized_asin, competitor.internal_name, competitor.brand or str(latest.get("brand") or ""),
        str(latest.get("title") or ""),
    )
    listing_changes = [
        {"run_id": action["run_id"], "observed_at": action["observed_at"], "title": action["title"], "item": item}
        for action in actions for item in action["items"] if item["is_listing_change"]
    ]
    product_intraday = [{
        "collected_at": row["collected_at"], "price": row.get("current_price"),
        "coupon": row.get("coupon_text"), "deal": row.get("deal_text"),
        "business_price": row.get("business_price_text"),
        "availability": row.get("availability"), "offer_count": row.get("offer_count"),
        "featured_seller": row.get("featured_seller"), "ships_from": row.get("ships_from"),
        "listing_status": row.get("listing_status") or ("active" if row.get("success") else None),
    } for row in reversed(products)]
    ad_intraday = []
    for row in reversed(search_outcomes):
        keyword = str(row["target"])
        key = (row["run_id"], keyword)
        if not row["success"]:
            status, rank = "collection_failed", None
        elif key in ad_ranks:
            status, rank = "observed", ad_ranks[key]
        else:
            status, rank = "not_observed", None
        ad_intraday.append({
            "collected_at": row["finished_at"], "keyword": keyword,
            "status": status, "ad_rank": rank,
        })

    impacts = _build_impacts(
        actions, products, search_outcomes, organic_ranks, keyword_names, current_time,
    )
    impacts_by_run: dict[str, list[dict[str, Any]]] = {}
    for impact in impacts:
        impacts_by_run.setdefault(str(impact["run_id"]), []).append(impact)
    for action in actions:
        action["impacts"] = impacts_by_run.get(str(action["run_id"]), [])

    return {
        "project_id": project_id, "asin": normalized_asin, "identity": identity,
        "amazon_url": f"https://www.amazon.com/dp/{normalized_asin}", "days": days,
        "range": {
            "from": cutoff_text, "to": end_text, "first_sample": products[0]["collected_at"] if products else None,
            "last_sample": products[-1]["collected_at"] if products else None, "sample_count": len(products),
        },
        "current": {
            "title": latest.get("title"), "brand": latest.get("brand"),
            "price": latest.get("current_price"), "main_bsr": latest.get("main_bsr"),
            "main_category": _category_rank_name(latest) if latest else "",
            "rating_value": latest.get("rating_value"), "rating_count": latest.get("rating_count"),
            "listing_status": latest.get("listing_status") or ("active" if latest.get("success") else None),
            "collected_at": latest.get("collected_at"),
        },
        "summary": {
            "important_nodes": len(direct_actions), "all_nodes": len(actions),
            "price_promo": category_counts["price_promo"], "listing": category_counts["listing"],
            "operations": category_counts["operations"], "platform": category_counts["platform"],
        },
        "actions": actions, "listing_changes": listing_changes, "impacts": impacts,
        "intraday": {"product": product_intraday, "ads": ad_intraday},
        "background": _background_summary([row for row in products if row.get("success")]),
    }
