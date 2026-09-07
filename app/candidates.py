from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Any

from app.config import load_competitors
from app.paths import CONFIG_DIR
from app.storage.database import Database

RULES_PATH = CONFIG_DIR / "new_product_rules.json"


def load_rules() -> dict[str, dict[str, Any]]:
    if not RULES_PATH.exists():
        return {}
    with RULES_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_rules(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError("新品筛选规则格式错误")
    cleaned = {}
    for project_id, rule in value.items():
        include = [str(item).strip().casefold() for item in rule.get("include_keywords", []) if str(item).strip()]
        exclude = [str(item).strip().casefold() for item in rule.get("exclude_keywords", []) if str(item).strip()]
        max_age = int(rule.get("max_age_days") or 90)
        if not 1 <= max_age <= 365:
            raise ValueError("新品最大上架天数必须在 1–365 之间")
        cleaned[str(project_id)] = {
            "include_keywords": list(dict.fromkeys(include)),
            "exclude_keywords": list(dict.fromkeys(exclude)),
            "max_age_days": max_age,
        }
    temp = RULES_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, RULES_PATH)
    return cleaned


def mark_expired_candidates(db: Database, project_id: str | None = None, today: date | None = None) -> int:
    rules = load_rules()
    parameters: tuple[Any, ...] = (project_id,) if project_id else ()
    where = "WHERE project_id=? AND date_first_available IS NOT NULL" if project_id else "WHERE date_first_available IS NOT NULL"
    rows = db.fetchall(f"SELECT * FROM bsr_new_candidates {where}", parameters)
    expired = 0
    current_date = today or date.today()
    with db.connect() as connection:
        for row in rows:
            try:
                age_days = (current_date - date.fromisoformat(str(row["date_first_available"]))).days
            except ValueError:
                continue
            max_age_days = int(rules.get(row["project_id"], {}).get("max_age_days", 90))
            if age_days < max_age_days:
                continue
            reason = f"上架已 {age_days} 天，达到或超过新品期限"
            if (
                row["relevance_status"] != "too_old"
                or row.get("age_days") != age_days
                or row.get("relevance_reason") != reason
            ):
                connection.execute(
                    """UPDATE bsr_new_candidates
                       SET age_days=?,relevance_status='too_old',classification_source='auto',relevance_reason=?
                       WHERE project_id=? AND asin=?""",
                    (age_days, reason, row["project_id"], row["asin"]),
                )
                expired += 1
    return expired


def discover_candidates(db: Database, run_id: str, project_id: str) -> list[dict[str, Any]]:
    mark_expired_candidates(db, project_id)
    current = db.fetchall(
        "SELECT * FROM bestseller_snapshots WHERE run_id=? AND project_id=? ORDER BY rank",
        (run_id, project_id),
    )
    monitored_asins = {
        item.asin for item in load_competitors(project_id, include_disabled=True)
    }

    # The candidate table stores one row per ASIN. If the same ASIN appears in
    # multiple configured categories, retain its best current category rank.
    current_by_asin: dict[str, dict[str, Any]] = {}
    for row in current:
        asin = str(row.get("asin") or "").upper()
        if not asin or asin in monitored_asins:
            continue
        previous = current_by_asin.get(asin)
        if previous is None or int(row.get("rank") or 9999) < int(previous.get("rank") or 9999):
            current_by_asin[asin] = row

    discovered: list[dict[str, Any]] = []
    for asin, row in current_by_asin.items():
        existing_rows = db.fetchall(
            "SELECT * FROM bsr_new_candidates WHERE project_id=? AND asin=?",
            (project_id, asin),
        )
        existing = existing_rows[0] if existing_rows else None
        values = {
            "project_id": project_id,
            "asin": asin,
            "first_seen_at": existing["first_seen_at"] if existing else row["collected_at"],
            "last_seen_at": row["collected_at"],
            "category_name": row["category_name"],
            "current_rank": row["rank"],
            "title": row.get("title") or (existing or {}).get("title"),
            "brand": row.get("brand") or (existing or {}).get("brand"),
            "date_source": (existing or {}).get("date_source"),
            "detail_url": f"https://www.amazon.com/dp/{asin}",
        }
        if existing:
            values.update({
                key: existing.get(key) for key in (
                    "date_first_available", "age_days", "relevance_status", "classification_source",
                    "relevance_reason", "last_checked_at", "alerted_at",
                )
            })
        else:
            values.update({
                "relevance_status": "pending",
                "classification_source": "auto",
                "relevance_reason": "当前位于BSR前100，等待详情页筛选",
            })
            discovered.append(values)
        db.upsert_candidate(values)
        # This also repairs old qualified-but-not-alerted candidates. The
        # alerted_at guard keeps the notification one-time only.
        if existing and existing.get("relevance_status") == "same" and not existing.get("alerted_at"):
            create_candidate_alert(db, run_id, project_id, asin, monitored_asins)
    return discovered


def candidate_backlog(db: Database, project_id: str, discovered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    monitored_asins = {
        item.asin for item in load_competitors(project_id, include_disabled=True)
    }
    newly_discovered_asins = {row["asin"] for row in discovered}
    by_asin = {row["asin"]: row for row in discovered if row["asin"] not in monitored_asins}
    rows = db.fetchall(
        """SELECT * FROM bsr_new_candidates WHERE project_id=? AND relevance_status='pending'
           AND (last_checked_at IS NULL OR (date_first_available IS NULL AND datetime(last_checked_at)<=datetime('now','-7 days')))
           ORDER BY CASE WHEN current_rank IS NULL THEN 1 ELSE 0 END, current_rank, first_seen_at DESC""", (project_id,),
    )
    for row in rows:
        if row["asin"] not in monitored_asins:
            by_asin.setdefault(row["asin"], row)
    rule = load_rules().get(project_id, {})
    include_words = rule.get("include_keywords", [])
    exclude_words = rule.get("exclude_keywords", [])

    def queue_priority(row: dict[str, Any]) -> tuple[int, int, int, str]:
        title = str(row.get("title") or "").casefold()
        excluded = any(word in title for word in exclude_words)
        included = any(word in title for word in include_words)
        match_priority = 0 if included and not excluded else (2 if excluded else 1)
        new_priority = 0 if row["asin"] in newly_discovered_asins else 1
        return match_priority, new_priority, int(row.get("current_rank") or 9999), str(row.get("first_seen_at") or "")

    return sorted(by_asin.values(), key=queue_priority)[:25]


def classify_candidate(project_id: str, product: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    rule = load_rules().get(project_id, {"include_keywords": [], "exclude_keywords": [], "max_age_days": 90})
    content = " ".join([
        str(product.get("title") or ""), str(product.get("highlights_text") or ""),
        " ".join(product.get("about_items_json") or []),
    ]).casefold()
    excluded = [word for word in rule.get("exclude_keywords", []) if word in content]
    included = [word for word in rule.get("include_keywords", []) if word in content]
    available = product.get("date_first_available")
    age_days = None
    if available:
        age_days = ((today or date.today()) - date.fromisoformat(str(available))).days
    max_age_days = int(rule.get("max_age_days", 90))
    if age_days is not None and age_days >= max_age_days:
        status, reason = "too_old", f"上架已 {age_days} 天，达到或超过新品期限"
    elif excluded:
        status, reason = "not_same", f"命中排除关键词：{', '.join(excluded)}"
    elif not included:
        status, reason = "pending", "未命中同类关键词，等待人工确认"
    elif age_days is None:
        status, reason = "pending", f"命中同类关键词 {', '.join(included)}，但未获取到上架日期"
    elif age_days < 0:
        status, reason = "pending", "上架日期晚于当前日期，等待人工确认"
    else:
        status, reason = "same", f"命中同类关键词：{', '.join(included)}；上架 {age_days} 天"
    return {"relevance_status": status, "classification_source": "auto", "relevance_reason": reason, "age_days": age_days}


def set_candidate_date(
    db: Database, project_id: str, asin: str, value: str, today: date | None = None,
) -> dict[str, Any]:
    rows = db.fetchall(
        "SELECT * FROM bsr_new_candidates WHERE project_id=? AND asin=?",
        (project_id, asin.upper()),
    )
    if not rows:
        raise ValueError("新品候选不存在")
    row = rows[0]
    current_date = today or date.today()
    date_text = str(value or "").strip()
    if date_text:
        try:
            available_date = date.fromisoformat(date_text)
        except ValueError:
            raise ValueError("上架日期格式无效，请使用 YYYY-MM-DD") from None
        if available_date > current_date:
            raise ValueError("上架日期不能晚于今天")
        date_text = available_date.isoformat()

    result = classify_candidate(project_id, {
        "title": row.get("title"),
        "highlights_text": "",
        "about_items_json": [],
        "date_first_available": date_text or None,
    }, current_date)
    if (
        result["relevance_status"] != "too_old"
        and row.get("classification_source") == "manual"
        and row.get("relevance_status") in {"same", "not_same"}
    ):
        result["relevance_status"] = row["relevance_status"]
        result["classification_source"] = "manual"
        label = "同类竞品" if row["relevance_status"] == "same" else "非同类产品"
        suffix = f"；人工填写上架日期，上架 {result['age_days']} 天" if date_text else "；上架日期待补充"
        result["relevance_reason"] = f"人工确认为{label}{suffix}"

    with db.connect() as connection:
        connection.execute(
            """UPDATE bsr_new_candidates
               SET date_first_available=?,date_source=?,age_days=?,relevance_status=?,
                   classification_source=?,relevance_reason=?
               WHERE project_id=? AND asin=?""",
            (
                date_text or None, "manual" if date_text else None,
                result["age_days"], result["relevance_status"],
                result["classification_source"], result["relevance_reason"],
                project_id, asin.upper(),
            ),
        )
    return db.fetchall(
        "SELECT * FROM bsr_new_candidates WHERE project_id=? AND asin=?",
        (project_id, asin.upper()),
    )[0]


def create_candidate_alert(
    db: Database, run_id: str, project_id: str, asin: str,
    monitored_asins: set[str] | None = None,
) -> bool:
    monitored_asins = monitored_asins if monitored_asins is not None else {
        item.asin for item in load_competitors(project_id, include_disabled=True)
    }
    if asin.upper() in monitored_asins:
        return False
    rows = db.fetchall("SELECT * FROM bsr_new_candidates WHERE project_id=? AND asin=?", (project_id, asin))
    if not rows:
        return False
    row = rows[0]
    if row["alerted_at"] or row["relevance_status"] != "same" or row["age_days"] is None:
        return False
    rules = load_rules().get(project_id, {"max_age_days": 90})
    if int(row["age_days"]) >= int(rules.get("max_age_days", 90)):
        return False
    now = datetime.now().isoformat(timespec="seconds")
    db.insert("change_events", {
        "run_id": run_id, "project_id": project_id, "event_type": "new_competitor_found",
        "severity": "high" if int(row["current_rank"] or 999) <= 20 else "medium",
        "source_type": "bestseller", "asin": asin, "category_name": row["category_name"],
        "field_name": "rank", "new_value": str(row["current_rank"]), "event_time": now,
        "message": f"发现未监控的同类新竞品：上架 {row['age_days']} 天，"
                   f"位于 {row['category_name']} 第 {row['current_rank']} 名",
    })
    with db.connect() as connection:
        connection.execute("UPDATE bsr_new_candidates SET alerted_at=? WHERE project_id=? AND asin=?", (now, project_id, asin))
    return True


def set_manual_status(db: Database, project_id: str, asin: str, status: str) -> dict[str, Any]:
    if status not in {"same", "not_same", "pending"}:
        raise ValueError("无效的人工确认状态")
    mark_expired_candidates(db, project_id)
    rows = db.fetchall("SELECT * FROM bsr_new_candidates WHERE project_id=? AND asin=?", (project_id, asin))
    if not rows:
        raise ValueError("新品候选不存在")
    if rows[0]["relevance_status"] == "too_old":
        raise ValueError("该商品已超过新品上架期限，无需人工确认")
    reason = {"same": "人工确认为同类竞品", "not_same": "人工确认为非同类产品", "pending": "等待人工确认"}[status]
    with db.connect() as connection:
        connection.execute(
            "UPDATE bsr_new_candidates SET relevance_status=?,classification_source='manual',relevance_reason=? WHERE project_id=? AND asin=?",
            (status, reason, project_id, asin),
        )
    if status == "same":
        latest = db.fetchall("SELECT run_id FROM collection_runs WHERE project_id=? ORDER BY started_at DESC LIMIT 1", (project_id,))
        if latest:
            create_candidate_alert(db, latest[0]["run_id"], project_id, asin)
    return db.fetchall("SELECT * FROM bsr_new_candidates WHERE project_id=? AND asin=?", (project_id, asin))[0]
