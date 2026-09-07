from __future__ import annotations

import json
from datetime import datetime
from typing import Any
import re

from app.storage.database import Database
from app.candidates import discover_candidates
from app.config import load_competitors


PRODUCT_FIELDS = {
    "current_price": "价格", "list_price": "划线价", "coupon_text": "Coupon",
    "deal_text": "Deal", "business_price_text": "企业价",
    "rating_count": "Rating数量", "rating_value": "星级",
    "main_bsr": "大类BSR", "amazon_choice": "Amazon's Choice",
    "best_seller": "Best Seller", "new_release": "New Release",
    "high_return_rate": "高退货率标签",
    "availability": "库存状态", "highlights_hash": "Highlights",
    "featured_seller": "Featured Offer卖家", "ships_from": "发货方", "offer_count": "跟卖数量",
    "variation_count": "变体数量", "title_hash": "标题", "image_hash": "主图",
    "about_items_hash": "About this item", "product_images_hash": "产品图片集",
}

LISTING_STATUS_LABELS = {
    "active": "正常在售",
    "dog": "页面变狗",
    "removed": "商品下架",
}

HASH_VALUE_FIELDS = {
    "title_hash": "title",
    "image_hash": "main_image_url",
    "highlights_hash": "highlights_text",
    "about_items_hash": "about_items_json",
    "product_images_hash": "product_images_json",
}


def _normalize_image_url(value: str | None) -> str | None:
    if not value:
        return value
    return re.sub(r"\._[^/]+_(?=\.(?:jpe?g|png|webp)$)", "", value, flags=re.I)


def _availability_state(value: str | None) -> str | None:
    text = " ".join(str(value or "").lower().split())
    if not text:
        return None
    if any(word in text for word in ("currently unavailable", "temporarily out of stock", "out of stock", "unavailable")):
        return "断货"
    if any(word in text for word in ("only ", "left in stock", "order soon")):
        return "低库存"
    if any(word in text for word in ("in stock", "available to ship", "usually ships")):
        return "有货"
    return None


def _listing_status(row: dict[str, Any]) -> str | None:
    value = str(row.get("listing_status") or "").strip()
    if value:
        return value
    return "active" if row.get("success") else None


def _category_names(row: dict[str, Any]) -> tuple[str | None, tuple[str, ...]]:
    try:
        values = json.loads(row.get("category_ranks_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        values = []
    names = []
    for item in values if isinstance(values, list) else []:
        name = " ".join(str(item.get("category") or "").split()) if isinstance(item, dict) else ""
        if name and name not in names:
            names.append(name)
    return (names[0] if names else None, tuple(sorted(names[1:])))


def _confirmed_category_values(
    row: dict[str, Any], previous_rows: list[dict[str, Any]], index: int,
    missing_confirmations: int,
) -> tuple[Any, Any] | None:
    current_value = _category_names(row)[index]
    previous_value = _category_names(previous_rows[0])[index]
    if current_value == previous_value and current_value:
        return None
    if current_value:
        return previous_value, current_value
    recent = [row, *previous_rows]
    if len(recent) <= missing_confirmations:
        return None
    first_values = [_category_names(item)[index] for item in recent[:missing_confirmations]]
    baseline = _category_names(recent[missing_confirmations])[index]
    if any(first_values) or not baseline:
        return None
    return baseline, current_value


def _category_display(value: Any) -> str | None:
    if isinstance(value, tuple):
        return "；".join(value) or None
    return str(value) if value else None


def _event(db: Database, run_id: str, project_id: str, event_type: str, severity: str, source_type: str, message: str, **values: Any) -> None:
    db.insert("change_events", {
        "run_id": run_id, "project_id": project_id, "event_type": event_type, "severity": severity,
        "source_type": source_type, "event_time": datetime.now().isoformat(timespec="seconds"),
        "message": message, **values,
    })


def _rating_event_details(row: dict[str, Any]) -> dict[str, Any] | None:
    value = row.get("rating_breakdown_json")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(value, dict) or not value:
        return None
    return {"rating_breakdown": value}


def compare_products(db: Database, run_id: str, project_id: str, settings: dict) -> None:
    current = db.fetchall(
        """SELECT * FROM product_snapshots WHERE run_id=? AND project_id=?
           AND (success=1 OR listing_status IN ('dog','removed'))""",
        (run_id, project_id),
    )
    price_threshold = float(settings["changes"].get("price_percent_alert", 5))
    bsr_threshold = int(settings["changes"].get("bsr_position_alert", 20))
    missing_confirmations = max(2, int(settings["changes"].get("missing_confirmations", 2)))
    for row in current:
        previous_rows = db.fetchall(
            """SELECT * FROM product_snapshots WHERE project_id=? AND asin=? AND run_id<>?
               AND (success=1 OR listing_status IN ('dog','removed'))
               ORDER BY collected_at DESC LIMIT ?""",
            (project_id, row["asin"], run_id, missing_confirmations + 8),
        )
        if not previous_rows:
            continue
        new_status = _listing_status(row)
        old_status = _listing_status(previous_rows[0])
        if new_status and old_status and new_status != old_status:
            _event(
                db, run_id, project_id, "listing_status_changed", "high", "product",
                f"{row['asin']} 的商品页面状态发生变化",
                asin=row["asin"], field_name="listing_status",
                old_value=LISTING_STATUS_LABELS.get(old_status, old_status),
                new_value=LISTING_STATUS_LABELS.get(new_status, new_status),
            )
        if new_status != "active":
            continue
        active_previous = [item for item in previous_rows if _listing_status(item) == "active" and item.get("success")]
        if not active_previous:
            continue
        previous = active_previous[0]

        for index, field, label in (
            (0, "main_category_name", "大类目"),
            (1, "subcategory_names", "小类目"),
        ):
            category_values = _confirmed_category_values(
                row, active_previous, index, missing_confirmations,
            )
            if not category_values:
                continue
            old_category, new_category = category_values
            _event(
                db, run_id, project_id, "category_changed", "medium", "product",
                f"{row['asin']} 的{label}发生变化",
                asin=row["asin"], field_name=field,
                old_value=_category_display(old_category),
                new_value=_category_display(new_category),
            )

        main_image_changed = (
            previous.get("image_hash") != row.get("image_hash")
            and row.get("image_hash") is not None
            and _normalize_image_url(previous.get("main_image_url"))
            != _normalize_image_url(row.get("main_image_url"))
        )
        for field, label in PRODUCT_FIELDS.items():
            old, new = previous.get(field), row.get(field)
            if new is None and field in {"offer_count", "ships_from"}:
                recent = [row, *previous_rows]
                if (
                    len(recent) <= missing_confirmations
                    or any(item.get(field) is not None for item in recent[:missing_confirmations])
                    or recent[missing_confirmations].get(field) is None
                ):
                    continue
                old = recent[missing_confirmations][field]
                new = 0 if field == "offer_count" else None
            if old == new:
                continue
            if field == "high_return_rate" and old is None:
                # The field was introduced after earlier snapshots; the first
                # observation establishes a baseline rather than a false alert.
                continue
            if new is None and field not in {"coupon_text", "deal_text", "business_price_text"}:
                if field != "ships_from":
                    continue
            if field == "availability":
                old_state, new_state = _availability_state(str(old) if old is not None else None), _availability_state(str(new) if new is not None else None)
                if not old_state or not new_state or old_state == new_state:
                    continue
                old, new = old_state, new_state
            if field in {"highlights_hash", "about_items_hash", "product_images_hash"} and old is None:
                # The field was introduced after earlier snapshots; do not report
                # initial backfill as a competitor change.
                continue
            if field == "image_hash" and _normalize_image_url(previous.get("main_image_url")) == _normalize_image_url(row.get("main_image_url")):
                continue
            if field == "product_images_hash" and main_image_changed:
                # A main-image replacement also changes the full image-set hash.
                # Report it once as "主图变化" instead of creating two alerts.
                continue
            change_value = None
            severity = "low"
            if field == "current_price" and old:
                change_value = float(new) - float(old)
                percent = abs(change_value / float(old) * 100)
                severity = "high" if percent >= price_threshold else "medium"
            elif field == "main_bsr" and old:
                change_value = int(old) - int(new)
                severity = "medium" if abs(change_value) >= bsr_threshold else "low"
            elif field == "offer_count" and old is not None:
                change_value = int(new or 0) - int(old)
                severity = "medium"
            elif field in {"coupon_text", "deal_text", "business_price_text", "amazon_choice", "best_seller", "availability", "featured_seller", "ships_from", "high_return_rate"}:
                severity = "high" if field in {"deal_text", "business_price_text", "availability", "high_return_rate"} else "medium"
            elif field in {"highlights_hash", "about_items_hash", "image_hash", "product_images_hash"}:
                severity = "medium"
            display_field = HASH_VALUE_FIELDS.get(field)
            display_old = previous.get(display_field) if display_field else old
            display_new = row.get(display_field) if display_field else new
            _event(
                db, run_id, project_id, "field_changed", severity, "product",
                f"{row['asin']} 的{label}发生变化", asin=row["asin"], field_name=field,
                old_value=str(display_old) if display_old is not None else None,
                new_value=str(display_new) if display_new is not None else None,
                change_value=change_value,
                details_json=_rating_event_details(row) if field == "rating_value" else None,
            )


def compare_searches(db: Database, run_id: str, project_id: str, settings: dict) -> None:
    threshold = int(settings["changes"].get("keyword_position_alert", 10))
    current = db.fetchall("SELECT * FROM search_snapshots WHERE run_id=? AND project_id=?", (run_id, project_id))
    target_asins = {item.asin for item in load_competitors(project_id)}
    keys = {(row["keyword"], row["asin"], row["is_sponsored"]) for row in current if row["asin"] in target_asins}
    for keyword, asin, sponsored in keys:
        rank_field = "ad_rank" if sponsored else "organic_rank"
        current_rank = min(row[rank_field] for row in current if row["keyword"] == keyword and row["asin"] == asin and row["is_sponsored"] == sponsored and row[rank_field] is not None)
        previous = db.fetchall(
            f"SELECT {rank_field} AS rank FROM search_snapshots WHERE project_id=? AND keyword=? AND asin=? AND is_sponsored=? AND run_id<>? AND {rank_field} IS NOT NULL ORDER BY collected_at DESC LIMIT 1",
            (project_id, keyword, asin, sponsored, run_id),
        )
        if not previous or previous[0]["rank"] == current_rank:
            continue
        old_rank = int(previous[0]["rank"])
        delta = old_rank - current_rank
        severity = "medium" if abs(delta) >= threshold else "low"
        position_type = "广告位" if sponsored else "自然位"
        _event(db, run_id, project_id, "rank_changed", severity, "search", f"{asin} 在 {keyword} 的{position_type}由 {old_rank} 变为 {current_rank}", asin=asin, keyword=keyword, field_name=rank_field, old_value=str(old_rank), new_value=str(current_rank), change_value=delta)


def compare_bestsellers(db: Database, run_id: str, project_id: str) -> list[dict]:
    # Every current, unmonitored top-100 ASIN becomes a candidate. Alerts are
    # created only after product relevance and Date First Available are checked.
    return discover_candidates(db, run_id, project_id)


def compare_run(db: Database, run_id: str, project_id: str, settings: dict) -> None:
    compare_products(db, run_id, project_id, settings)
    compare_searches(db, run_id, project_id, settings)
    compare_bestsellers(db, run_id, project_id)
