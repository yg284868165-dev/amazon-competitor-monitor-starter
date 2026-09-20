from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any

from app.config import load_competitors
from app.paths import CONFIG_DIR
from app.storage.database import Database

RULES_PATH = CONFIG_DIR / "new_product_rules.json"
MOMENTUM_DAYS = 7
MOMENTUM_MIN_OBSERVED_DAYS = 4
MOMENTUM_MIN_RANK_GAIN = 5
MOMENTUM_MIN_GAIN_RATIO = 0.10
MOMENTUM_MIN_UP_RATIO = 0.60


def _include_term_matches(keywords: list[str], content: str) -> list[tuple[str, str]]:
    """Broad-match include phrases: any meaningful term makes the rule match."""
    matches: list[tuple[str, str]] = []
    seen_terms: set[str] = set()
    for keyword in keywords:
        normalized = str(keyword or "").strip().casefold()
        if not normalized:
            continue
        terms = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", normalized)
        for term in terms or [normalized]:
            if term in content:
                if term not in seen_terms:
                    matches.append((normalized, term))
                    seen_terms.add(term)
                break
    return matches


def load_rules() -> dict[str, dict[str, Any]]:
    if not RULES_PATH.exists():
        return {}
    with RULES_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_rules(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError("潜力竞品筛选规则格式错误")
    existing = load_rules()
    cleaned = {}
    for project_id, rule in value.items():
        include = [str(item).strip().casefold() for item in rule.get("include_keywords", []) if str(item).strip()]
        exclude = [str(item).strip().casefold() for item in rule.get("exclude_keywords", []) if str(item).strip()]
        # Keep the former age setting in the file for backwards compatibility.
        # It is now informational only and no longer gates radar recommendations.
        max_age = int(rule.get("max_age_days") or existing.get(str(project_id), {}).get("max_age_days") or 90)
        if not 1 <= max_age <= 365:
            raise ValueError("兼容字段 max_age_days 必须在 1–365 之间")
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
    """Refresh listing ages without excluding older products from the radar."""
    parameters: tuple[Any, ...] = (project_id,) if project_id else ()
    where = "WHERE project_id=? AND date_first_available IS NOT NULL" if project_id else "WHERE date_first_available IS NOT NULL"
    rows = db.fetchall(f"SELECT * FROM bsr_new_candidates {where}", parameters)
    changed = 0
    current_date = today or date.today()
    with db.connect() as connection:
        for row in rows:
            try:
                age_days = (current_date - date.fromisoformat(str(row["date_first_available"]))).days
            except ValueError:
                continue
            if row.get("age_days") != age_days:
                connection.execute(
                    "UPDATE bsr_new_candidates SET age_days=? WHERE project_id=? AND asin=?",
                    (age_days, row["project_id"], row["asin"]),
                )
                changed += 1
    return changed


def discover_candidates(db: Database, run_id: str, project_id: str) -> list[dict[str, Any]]:
    mark_expired_candidates(db, project_id)
    refresh_automatic_classifications(db, project_id)
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
                "relevance_reason": "当前位于BSR监控范围，等待同类判断",
            })
            discovered.append(values)
        db.upsert_candidate(values)
    # Leaving the observed range ends the current momentum episode. A later
    # genuinely new rise may then trigger one fresh recommendation.
    with db.connect() as connection:
        connection.execute(
            """UPDATE bsr_new_candidates SET momentum_active=0
               WHERE project_id=? AND momentum_active=1
               AND datetime(last_seen_at)<datetime('now','-2 days')""",
            (project_id,),
        )
    sync_candidate_alerts(db, run_id, project_id, set(current_by_asin))
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
        included = bool(_include_term_matches(include_words, title))
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
    include_keywords = rule.get("include_keywords", [])
    included = _include_term_matches(include_keywords, content)
    available = product.get("date_first_available")
    age_days = None
    if available:
        age_days = ((today or date.today()) - date.fromisoformat(str(available))).days
    if excluded:
        status, reason = "not_same", f"命中排除关键词：{', '.join(excluded)}"
    elif not include_keywords:
        status, reason = "pending", "尚未配置同类关键词，等待设置筛选规则"
    elif not content.strip():
        status, reason = "pending", "尚未获取到可用于匹配的商品文案"
    elif not included:
        status, reason = "not_same", "未宽泛命中任何同类关键词，自动判定为非同类产品"
    elif age_days is not None and age_days < 0:
        status, reason = "pending", "上架日期晚于当前日期，等待人工确认"
    else:
        labels = ", ".join(f"{term}（规则：{keyword}）" for keyword, term in included)
        age_note = f"；上架 {age_days} 天" if age_days is not None else "；上架日期未获取（不影响趋势筛选）"
        status, reason = "same", f"宽泛命中同类词：{labels}{age_note}"
    return {"relevance_status": status, "classification_source": "auto", "relevance_reason": reason, "age_days": age_days}


def refresh_automatic_classifications(
    db: Database, project_id: str | None = None, today: date | None = None,
) -> list[dict[str, Any]]:
    """Reapply current rules to stored automatic candidates and return changed rows."""
    parameters: tuple[Any, ...] = (project_id,) if project_id else ()
    where = "WHERE classification_source='auto'"
    if project_id:
        where += " AND project_id=?"
    rows = db.fetchall(f"SELECT * FROM bsr_new_candidates {where}", parameters)
    changed: list[dict[str, Any]] = []
    with db.connect() as connection:
        for row in rows:
            result = classify_candidate(row["project_id"], {
                "title": row.get("title"),
                "highlights_text": "",
                "about_items_json": [],
                "date_first_available": row.get("date_first_available"),
            }, today)
            comparable = (
                result["relevance_status"], result["classification_source"],
                result["relevance_reason"], result["age_days"],
            )
            existing = (
                row.get("relevance_status"), row.get("classification_source"),
                row.get("relevance_reason"), row.get("age_days"),
            )
            if comparable == existing:
                continue
            connection.execute(
                """UPDATE bsr_new_candidates
                   SET relevance_status=?,classification_source=?,relevance_reason=?,age_days=?
                   WHERE project_id=? AND asin=?""",
                (*comparable, row["project_id"], row["asin"]),
            )
            changed.append({**row, **result})
    return changed


def set_candidate_date(
    db: Database, project_id: str, asin: str, value: str, today: date | None = None,
) -> dict[str, Any]:
    rows = db.fetchall(
        "SELECT * FROM bsr_new_candidates WHERE project_id=? AND asin=?",
        (project_id, asin.upper()),
    )
    if not rows:
        raise ValueError("潜力竞品候选不存在")
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
    if row.get("classification_source") == "manual" and row.get("relevance_status") in {"same", "not_same"}:
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


def _rounded_median(values: list[int]) -> int:
    return int(round(float(median(values))))


def _momentum_for_series(daily: list[tuple[str, int]]) -> dict[str, Any]:
    start_values = [rank for _, rank in daily[:2]]
    end_values = [rank for _, rank in daily[-2:]]
    start_rank, end_rank = _rounded_median(start_values), _rounded_median(end_values)
    rank_gain = start_rank - end_rank
    gain_ratio = rank_gain / start_rank if start_rank else 0.0
    movements = [previous - current for (_, previous), (_, current) in zip(daily, daily[1:]) if previous != current]
    up_moves = sum(1 for movement in movements if movement > 0)
    up_ratio = up_moves / len(movements) if movements else 0.0
    enough_days = len(daily) >= MOMENTUM_MIN_OBSERVED_DAYS
    meaningful_gain = rank_gain >= MOMENTUM_MIN_RANK_GAIN or gain_ratio >= MOMENTUM_MIN_GAIN_RATIO
    recommended = enough_days and rank_gain > 0 and meaningful_gain and up_ratio >= MOMENTUM_MIN_UP_RATIO
    if not enough_days:
        reason = f"仅有 {len(daily)} 个观测日，至少需要 {MOMENTUM_MIN_OBSERVED_DAYS} 天"
    elif not meaningful_gain or rank_gain <= 0:
        reason = f"近7日从第 {start_rank} 名到第 {end_rank} 名，上升幅度未达阈值"
    elif up_ratio < MOMENTUM_MIN_UP_RATIO:
        reason = f"近7日从第 {start_rank} 名到第 {end_rank} 名，但上升方向仅占 {up_moves}/{len(movements)} 个可比区间"
    else:
        reason = f"近7日从第 {start_rank} 名提升到第 {end_rank} 名，{up_moves}/{len(movements)} 个可比区间向上"
    high_confidence = recommended and len(daily) >= 5 and up_ratio >= 0.75 and (
        rank_gain >= 15 or gain_ratio >= 0.25
    )
    return {
        "observed_days": len(daily), "start_rank": start_rank, "end_rank": end_rank,
        "rank_gain": rank_gain, "up_moves": up_moves, "comparable_moves": len(movements),
        "up_ratio": up_ratio, "recommended": recommended,
        "confidence": "high" if high_confidence else ("medium" if recommended else "none"),
        "reason": reason,
        "daily_path": [{"date": day, "rank": rank} for day, rank in daily],
    }


def build_opportunity_radar(
    db: Database, project_id: str | None = None, today: date | None = None,
) -> list[dict[str, Any]]:
    """Rank unmonitored same-type products by recent small-category BSR momentum."""
    current_date = today or date.today()
    cutoff = (current_date - timedelta(days=MOMENTUM_DAYS - 1)).isoformat()
    parameters: tuple[Any, ...] = (project_id,) if project_id else ()
    candidate_where = "WHERE project_id=?" if project_id else ""
    candidates = db.fetchall(
        f"SELECT * FROM bsr_new_candidates {candidate_where}", parameters,
    )
    project_ids = {str(row["project_id"]) for row in candidates}
    monitored = {
        pid: {item.asin for item in load_competitors(pid, include_disabled=True)}
        for pid in project_ids
    }
    snapshot_where = "AND project_id=?" if project_id else ""
    snapshots = db.fetchall(
        f"""SELECT project_id,category_name,asin,rank,substr(collected_at,1,10) day,collected_at
            FROM bestseller_snapshots
            WHERE date(collected_at)>=date(?) AND date(collected_at)<=date(?) {snapshot_where}
            ORDER BY collected_at""",
        (cutoff, current_date.isoformat(), *parameters),
    )
    grouped: dict[tuple[str, str, str, str], list[int]] = {}
    latest: dict[tuple[str, str, str], tuple[str, int]] = {}
    for row in snapshots:
        key = (str(row["project_id"]), str(row["asin"]), str(row["category_name"]), str(row["day"]))
        try:
            grouped.setdefault(key, []).append(int(row["rank"]))
            latest[(key[0], key[1], key[2])] = (str(row["collected_at"]), int(row["rank"]))
        except (TypeError, ValueError):
            continue
    series: dict[tuple[str, str, str], list[tuple[str, int]]] = {}
    for (pid, asin, category, day), ranks in grouped.items():
        series.setdefault((pid, asin, category), []).append((day, _rounded_median(ranks)))
    trends: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for (pid, asin, category), daily in series.items():
        daily.sort()
        trend = _momentum_for_series(daily)
        trend.update({"category_name": category, "current_rank": latest[(pid, asin, category)][1]})
        trends.setdefault((pid, asin), []).append(trend)

    result = []
    for row in candidates:
        pid, asin = str(row["project_id"]), str(row["asin"])
        if asin in monitored.get(pid, set()):
            continue
        choices = trends.get((pid, asin), [])
        best = max(
            choices,
            key=lambda item: (
                int(item["recommended"]), item["confidence"] == "high",
                item["rank_gain"], -item["current_rank"], item["observed_days"],
            ),
            default=None,
        )
        enriched = dict(row)
        if best:
            enriched.update(best)
        else:
            enriched.update({
                "category_name": row.get("category_name"), "current_rank": row.get("current_rank"),
                "observed_days": 0, "start_rank": None, "end_rank": None, "rank_gain": 0,
                "up_moves": 0, "comparable_moves": 0, "up_ratio": 0.0,
                "recommended": False, "confidence": "none",
                "reason": "近7日暂无足够的榜单观测数据", "daily_path": [],
            })
        enriched["recommended"] = bool(
            enriched["recommended"] and enriched.get("relevance_status") == "same"
        )
        if best and enriched.get("relevance_status") != "same":
            enriched["reason"] = f"趋势数据已计算；{row.get('relevance_reason') or '尚未确认是否同类'}"
        result.append(enriched)
    confidence_order = {"high": 0, "medium": 1, "none": 2}
    return sorted(result, key=lambda item: (
        not item["recommended"], confidence_order.get(str(item["confidence"]), 3),
        -int(item.get("rank_gain") or 0), int(item.get("current_rank") or 9999),
    ))


def _record_momentum_alert(
    db: Database, run_id: str, project_id: str, opportunity: dict[str, Any], now: str,
) -> None:
    asin = str(opportunity["asin"])
    path = " → ".join(
        f"{item['date'][5:]} 第{item['rank']}名" for item in opportunity["daily_path"]
    )
    db.insert("change_events", {
        "run_id": run_id, "project_id": project_id, "event_type": "competitor_momentum_found",
        "severity": "high" if opportunity["confidence"] == "high" else "medium",
        "source_type": "bestseller", "asin": asin, "category_name": opportunity["category_name"],
        "field_name": "rank", "old_value": str(opportunity["start_rank"]),
        "new_value": str(opportunity["end_rank"]), "change_value": -opportunity["rank_gain"],
        "event_time": now,
        "message": f"发现未监控的潜力竞品：{opportunity['reason']}；日中位名次路径：{path}",
        "details_json": {"daily_path": opportunity["daily_path"], "confidence": opportunity["confidence"]},
    })
    with db.connect() as connection:
        connection.execute(
            """UPDATE bsr_new_candidates
               SET momentum_active=1,momentum_alerted_at=?,momentum_reason=?
               WHERE project_id=? AND asin=?""",
            (now, opportunity["reason"], project_id, asin),
        )


def sync_candidate_alerts(
    db: Database, run_id: str, project_id: str, asins: set[str] | None = None,
) -> list[str]:
    """Update one project's momentum episodes using a single radar calculation."""
    selected = {str(asin).upper() for asin in asins} if asins is not None else None
    candidates = db.fetchall(
        "SELECT * FROM bsr_new_candidates WHERE project_id=?", (project_id,),
    )
    opportunities = {
        str(item["asin"]): item for item in build_opportunity_radar(db, project_id)
        if item["recommended"] and (selected is None or str(item["asin"]) in selected)
    }
    now = datetime.now().isoformat(timespec="seconds")
    alerted: list[str] = []
    for row in candidates:
        asin = str(row["asin"])
        if selected is not None and asin not in selected:
            continue
        opportunity = opportunities.get(asin)
        if opportunity is None:
            if row.get("momentum_active"):
                with db.connect() as connection:
                    connection.execute(
                        "UPDATE bsr_new_candidates SET momentum_active=0 WHERE project_id=? AND asin=?",
                        (project_id, asin),
                    )
            continue
        if row.get("momentum_active"):
            continue
        _record_momentum_alert(db, run_id, project_id, opportunity, now)
        alerted.append(asin)
    return alerted


def create_candidate_alert(
    db: Database, run_id: str, project_id: str, asin: str,
    monitored_asins: set[str] | None = None,
) -> bool:
    normalized = asin.upper()
    monitored_asins = monitored_asins if monitored_asins is not None else {
        item.asin for item in load_competitors(project_id, include_disabled=True)
    }
    if normalized in monitored_asins:
        return False
    return normalized in sync_candidate_alerts(db, run_id, project_id, {normalized})


def set_manual_status(db: Database, project_id: str, asin: str, status: str) -> dict[str, Any]:
    if status not in {"same", "not_same", "pending"}:
        raise ValueError("无效的人工确认状态")
    mark_expired_candidates(db, project_id)
    rows = db.fetchall("SELECT * FROM bsr_new_candidates WHERE project_id=? AND asin=?", (project_id, asin))
    if not rows:
        raise ValueError("潜力竞品候选不存在")
    reason = {"same": "人工确认为同类竞品", "not_same": "人工确认为非同类产品", "pending": "等待人工确认"}[status]
    with db.connect() as connection:
        connection.execute(
            "UPDATE bsr_new_candidates SET relevance_status=?,classification_source='manual',relevance_reason=? WHERE project_id=? AND asin=?",
            (status, reason, project_id, asin),
        )
    if status == "same":
        latest = db.fetchall(
            "SELECT run_id FROM collection_runs WHERE project_id=? AND status<>'running' ORDER BY started_at DESC LIMIT 1",
            (project_id,),
        )
        if latest:
            create_candidate_alert(db, latest[0]["run_id"], project_id, asin)
    return db.fetchall("SELECT * FROM bsr_new_candidates WHERE project_id=? AND asin=?", (project_id, asin))[0]
