from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta
from statistics import median
from typing import Any

from app.config import load_competitors, load_keywords, load_projects
from app.presentation import build_product_identities
from app.storage.database import Database

PERIODS = (7, 14, 28)
METRIC_LABELS = {
    "main_bsr": "大类目BSR", "category_bsr": "小类目BSR",
    "organic_rank": "自然排名",
}
UNRANKED = 1_000_000


def _daily_medians(rows: list[dict[str, Any]], value_field: str) -> list[tuple[str, int]]:
    values: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        value = row.get(value_field)
        if value is not None:
            values[str(row["day"])].append(int(value))
    return [(day, round(median(day_values))) for day, day_values in sorted(values.items())]


def _threshold_met(metric: str, change: int, reference_rank: float) -> bool:
    movement = abs(change)
    if metric == "main_bsr":
        return movement >= max(20, round(reference_rank * 0.1))
    if metric == "category_bsr":
        # The small-category rule is deliberately OR, not the stricter maximum.
        return movement >= 5 or movement >= reference_rank * 0.1
    if metric == "organic_rank":
        return movement >= 5
    return movement >= 2


def _period_result(
    metric: str, samples: list[tuple[str, int]], period: int, week_end: date,
) -> dict[str, Any]:
    weeks = period // 7
    minimum_days = 3 if metric == "ad_rank" else 5
    weekly_samples: list[list[tuple[str, int]]] = []
    for index in range(weeks):
        start = week_end - timedelta(days=(weeks - index) * 7 - 1)
        end = start + timedelta(days=6)
        matching = [sample for sample in samples if start.isoformat() <= sample[0] <= end.isoformat()]
        weekly_samples.append(matching)
    valid_days = sum(len(values) for values in weekly_samples)
    if any(len(values) < minimum_days for values in weekly_samples):
        return {"period": period, "status": "insufficient", "valid_days": valid_days}

    all_values = [value for values in weekly_samples for _, value in values]
    reference = median(all_values)
    if weeks == 1:
        values = weekly_samples[0]
        start_rank = round(median(value for _, value in values[:2]))
        end_rank = round(median(value for _, value in values[-2:]))
        moves = [old - new for (_, old), (_, new) in zip(values, values[1:]) if old != new]
        change = start_rank - end_rank
        consistent = sum(1 for move in moves if (move > 0) == (change > 0)) if change else 0
        direction_ok = bool(moves) and consistent / len(moves) >= 0.6
    else:
        representatives = [round(median(value for _, value in values)) for values in weekly_samples]
        start_rank, end_rank = representatives[0], representatives[-1]
        change = start_rank - end_rank
        if weeks == 2:
            direction_ok = change != 0
        else:
            moves = [old - new for old, new in zip(representatives, representatives[1:]) if old != new]
            consistent = sum(1 for move in moves if (move > 0) == (change > 0)) if change else 0
            direction_ok = change != 0 and consistent >= 2

    result = {
        "period": period, "start_rank": start_rank, "end_rank": end_rank,
        "direction": "提升" if change > 0 else "下降" if change < 0 else "持平",
        "change_positions": abs(change), "valid_days": valid_days,
    }
    result["status"] = "trend" if direction_ok and _threshold_met(metric, change, reference) else "stable"
    return result


def _qualify(metric: str, samples: list[tuple[str, int]], period: int) -> dict[str, Any] | None:
    """Compatibility helper for callers that already provide a complete window."""
    if not samples:
        return None
    result = _period_result(metric, samples, period, date.fromisoformat(samples[-1][0]))
    if result["status"] != "trend":
        return None
    return {key: value for key, value in result.items() if key != "status"}


def _category_rows(value: str | None) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        rows = json.loads(value)
        return rows if isinstance(rows, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _rank_display(value: int | None, pages: int | None = None) -> str:
    if value is None:
        return "数据不足"
    if value >= UNRANKED:
        return f"未进入前{pages or 3}页"
    return f"{value}名"


def _trend_summary(row: dict[str, Any], period: int, result: dict[str, Any]) -> str:
    if result["status"] == "insufficient":
        return f"{period}日数据不足"
    start = _rank_display(result["start_rank"], row.get("pages"))
    end = _rank_display(result["end_rank"], row.get("pages"))
    if result["status"] == "stable":
        return f"{period}日无明显趋势（{start}→{end}）"
    return f"{period}日从{start}{result['direction']}到{end}"


def analyze_project_weekly_trends(
    db: Database, project_id: str, week_end: date,
) -> list[dict[str, Any]]:
    if week_end.weekday() != 6:
        raise ValueError("自然周趋势截止日期必须是周日")
    competitors = load_competitors(project_id)
    target_asins = {item.asin for item in competitors}
    if not target_asins:
        return []
    identities = build_product_identities(db, project_id)
    earliest = (week_end - timedelta(days=27)).isoformat()
    latest = week_end.isoformat()
    placeholders = ",".join("?" for _ in target_asins)
    asin_params = tuple(sorted(target_asins))
    products = db.fetchall(
        f"""SELECT substr(collected_at,1,10) day,asin,main_bsr,category_ranks_json,collected_at
            FROM product_snapshots WHERE project_id=? AND success=1
            AND substr(collected_at,1,10) BETWEEN ? AND ? AND asin IN ({placeholders})
            ORDER BY collected_at""",
        (project_id, earliest, latest, *asin_params),
    )

    series: dict[tuple[str, str, str], dict[str, Any]] = {}
    for asin in target_asins:
        matching = [row for row in products if row["asin"] == asin]
        main_samples = _daily_medians(matching, "main_bsr")
        main_category = ""
        category_values: dict[tuple[str, str], list[int]] = defaultdict(list)
        for row in matching:
            ranks = _category_rows(row.get("category_ranks_json"))
            if ranks and ranks[0].get("category"):
                main_category = str(ranks[0]["category"]).strip()
            for item in ranks[1:]:
                category = str(item.get("category") or "").strip()
                rank = item.get("rank")
                if category and rank is not None:
                    category_values[(str(row["day"]), category)].append(int(rank))
        series[("main_bsr", asin, main_category)] = {
            "samples": main_samples, "category_name": main_category,
        }
        categories = sorted({category for _, category in category_values})
        for category in categories:
            samples = [
                (day, round(median(values)))
                for (day, name), values in sorted(category_values.items()) if name == category
            ]
            series[("category_bsr", asin, category)] = {
                "samples": samples, "category_name": category,
            }

    keywords = load_keywords(project_id=project_id)
    outcomes = db.fetchall(
        """SELECT run_id,target keyword,substr(finished_at,1,10) day
           FROM collection_task_outcomes WHERE project_id=? AND task_type='search' AND success=1
           AND substr(finished_at,1,10) BETWEEN ? AND ? ORDER BY finished_at""",
        (project_id, earliest, latest),
    )
    searches = db.fetchall(
        f"""SELECT run_id,keyword,asin,organic_rank,ad_rank
            FROM search_snapshots WHERE project_id=?
            AND substr(collected_at,1,10) BETWEEN ? AND ? AND asin IN ({placeholders})""",
        (project_id, earliest, latest, *asin_params),
    )
    observed: dict[tuple[str, str, str], dict[str, int | None]] = {}
    for row in searches:
        key = (row["run_id"], row["keyword"], row["asin"])
        values = observed.setdefault(key, {"organic_rank": None, "ad_rank": None})
        for field in ("organic_rank", "ad_rank"):
            value = row.get(field)
            if value is not None and (values[field] is None or int(value) < int(values[field])):
                values[field] = int(value)
    for keyword in keywords:
        keyword_outcomes = [row for row in outcomes if row["keyword"] == keyword.keyword]
        for asin in target_asins:
            organic_by_day: dict[str, list[int]] = defaultdict(list)
            for outcome in keyword_outcomes:
                values = observed.get((outcome["run_id"], keyword.keyword, asin), {})
                organic_by_day[outcome["day"]].append(int(values.get("organic_rank") or UNRANKED))
            organic_samples = [(day, round(median(values))) for day, values in sorted(organic_by_day.items())]
            series[("organic_rank", asin, keyword.keyword)] = {
                "samples": organic_samples, "keyword": keyword.keyword, "pages": keyword.pages,
            }

    results = []
    for (metric, asin, context), metadata in series.items():
        period_results = {
            period: _period_result(metric, metadata["samples"], period, week_end)
            for period in PERIODS
        }
        if not any(value["status"] == "trend" for value in period_results.values()):
            continue
        row = {
            "project_id": project_id, "metric": metric, "metric_label": METRIC_LABELS[metric],
            "asin": asin, "product": identities.get(asin, asin), "context": context,
            "keyword": metadata.get("keyword", ""), "category_name": metadata.get("category_name", ""),
            "pages": metadata.get("pages"), "periods": period_results,
        }
        row["summaries"] = [_trend_summary(row, period, period_results[period]) for period in PERIODS]
        results.append(row)
    return results


def analyze_all_weekly_trends(db: Database, week_end: date) -> list[dict[str, Any]]:
    project_ids = {item.project_id for item in load_projects()}
    project_ids.update(row["project_id"] for row in db.fetchall("SELECT DISTINCT project_id FROM collection_runs"))
    result = []
    for project_id in sorted(project_ids):
        result.extend(analyze_project_weekly_trends(db, project_id, week_end))
    return result


def analyze_project_trends(
    db: Database, project_id: str, end_date: date, periods: tuple[int, ...] = PERIODS,
) -> list[dict[str, Any]]:
    """Flatten the last completed natural-week analysis for the Excel trend sheet."""
    week_end = end_date - timedelta(days=(end_date.weekday() + 1) % 7)
    result = []
    for row in analyze_project_weekly_trends(db, project_id, week_end):
        for period in periods:
            value = row["periods"].get(period)
            if not value or value["status"] != "trend":
                continue
            result.append({
                **row, **value, "period": period,
                "summary": f"{row['metric_label']}：{_trend_summary(row, period, value)}",
            })
    return result
