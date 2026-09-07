from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from app.config import load_competitors, load_keywords, load_projects
from app.paths import CONFIG_DIR
from app.presentation import build_product_identities
from app.storage.database import Database


CHINA_TZ = ZoneInfo("Asia/Shanghai")
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
STATUS_LABELS = {
    "observed": "第{rank}位",
    "not_observed": "未观察到",
    "failed": "采集失败",
}


def _aware_china(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=CHINA_TZ)


@lru_cache(maxsize=1)
def _schedule_times() -> tuple[time, ...]:
    path = CONFIG_DIR / "schedule.json"
    try:
        values = json.loads(path.read_text(encoding="utf-8")).get("times") or []
        result = [time.fromisoformat(str(value)) for value in values]
    except (OSError, ValueError, json.JSONDecodeError):
        result = []
    return tuple(result or [time(2), time(8), time(14), time(20)])


def _slot_for(started_at: str) -> tuple[str, int]:
    current = _aware_china(started_at)
    candidates: list[datetime] = []
    for offset in (-1, 0, 1):
        candidate_date = current.date() + timedelta(days=offset)
        for value in _schedule_times():
            candidates.append(datetime.combine(candidate_date, value, CHINA_TZ))
    closest = min(candidates, key=lambda value: abs((value - current).total_seconds()))
    if abs((closest - current).total_seconds()) > 2 * 60 * 60:
        return "其他", 99
    pacific = closest.astimezone(PACIFIC_TZ)
    return pacific.strftime("%H:%M"), pacific.hour


def collect_ad_observations(
    db: Database, start_date: date, end_date: date,
) -> list[dict[str, Any]]:
    """Return every observed, absent or failed ad sample for monitored ASINs.

    ``start_date`` and ``end_date`` are interpreted in America/Los_Angeles so
    daypart evidence follows the marketplace rather than the computer clock.
    """
    if start_date > end_date:
        return []
    projects = {item.project_id: item.project_name for item in load_projects()}
    competitors = {
        project_id: [item.asin for item in load_competitors(project_id)]
        for project_id in projects
    }
    keywords = {
        project_id: {item.keyword for item in load_keywords(project_id=project_id)}
        for project_id in projects
    }
    identities = {
        project_id: build_product_identities(db, project_id)
        for project_id in projects
    }
    broad_start = (start_date - timedelta(days=2)).isoformat()
    broad_end = (end_date + timedelta(days=2)).isoformat()
    runs = db.fetchall(
        """SELECT run_id,project_id,task_type,started_at,finished_at,status
           FROM collection_runs WHERE substr(started_at,1,10) BETWEEN ? AND ?""",
        (broad_start, broad_end),
    )
    runs_by_id = {row["run_id"]: row for row in runs}
    if not runs_by_id:
        return []
    placeholders = ",".join("?" for _ in runs_by_id)
    run_ids = tuple(runs_by_id)
    outcomes = db.fetchall(
        f"""SELECT run_id,project_id,target keyword,success,finished_at
            FROM collection_task_outcomes WHERE task_type='search'
            AND run_id IN ({placeholders})""",
        run_ids,
    )
    batch_times = db.fetchall(
        f"""SELECT run_id,project_id,keyword,MIN(collected_at) collected_at
            FROM search_snapshots WHERE run_id IN ({placeholders})
            GROUP BY run_id,project_id,keyword""",
        run_ids,
    )
    batch_time_by_key = {
        (row["run_id"], row["project_id"], row["keyword"]): row["collected_at"]
        for row in batch_times
    }
    ranks = db.fetchall(
        f"""SELECT run_id,project_id,keyword,asin,MIN(ad_rank) ad_rank
            FROM search_snapshots WHERE ad_rank IS NOT NULL
            AND run_id IN ({placeholders})
            GROUP BY run_id,project_id,keyword,asin""",
        run_ids,
    )
    rank_by_key = {
        (row["run_id"], row["project_id"], row["keyword"], row["asin"]): int(row["ad_rank"])
        for row in ranks
    }

    batches: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in outcomes:
        key = (row["run_id"], row["project_id"], row["keyword"])
        batches[key] = {**row, "success": bool(row["success"])}
    # Older runs may predate per-target outcomes. A parsed search page still
    # proves that the keyword batch produced usable data.
    for key, collected_at in batch_time_by_key.items():
        if key not in batches:
            batches[key] = {
                "run_id": key[0], "project_id": key[1], "keyword": key[2],
                "success": True, "finished_at": collected_at,
            }

    observations: list[dict[str, Any]] = []
    for (run_id, project_id, keyword), batch in batches.items():
        if project_id not in projects or keyword not in keywords.get(project_id, set()):
            continue
        run = runs_by_id.get(run_id)
        if not run:
            continue
        observed_at = (
            batch_time_by_key.get((run_id, project_id, keyword))
            or batch.get("finished_at") or run.get("finished_at") or run["started_at"]
        )
        china_dt = _aware_china(str(observed_at))
        pacific_dt = china_dt.astimezone(PACIFIC_TZ)
        if not start_date <= pacific_dt.date() <= end_date:
            continue
        slot_label, slot_order = _slot_for(str(run["started_at"]))
        for asin in competitors.get(project_id, []):
            rank = rank_by_key.get((run_id, project_id, keyword, asin))
            status = "observed" if rank is not None else (
                "not_observed" if batch["success"] else "failed"
            )
            observations.append({
                "run_id": run_id, "run_type": run["task_type"],
                "project_id": project_id, "project_name": projects[project_id],
                "asin": asin, "product": identities.get(project_id, {}).get(asin, asin),
                "keyword": keyword, "status": status, "ad_rank": rank,
                "china_datetime": china_dt.isoformat(timespec="minutes"),
                "pacific_datetime": pacific_dt.isoformat(timespec="minutes"),
                "pacific_date": pacific_dt.date().isoformat(),
                "slot": slot_label, "slot_order": slot_order,
            })
    return sorted(observations, key=lambda row: (
        row["project_id"], row["asin"], row["keyword"], row["pacific_datetime"], row["run_id"],
    ))


def _display_observation(row: dict[str, Any]) -> str:
    label = STATUS_LABELS[row["status"]]
    if row["status"] == "observed":
        label = label.format(rank=row["ad_rank"])
    local_time = datetime.fromisoformat(row["pacific_datetime"]).strftime("%H:%M")
    return f"{local_time} {label}"


def build_daily_ad_paths(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        grouped[(row["project_id"], row["asin"], row["keyword"], row["pacific_date"])].append(row)
    result = []
    for (project_id, asin, keyword, pacific_date), rows in grouped.items():
        rows.sort(key=lambda row: row["pacific_datetime"])
        ranks = [int(row["ad_rank"]) for row in rows if row["ad_rank"] is not None]
        result.append({
            "project_id": project_id, "project_name": rows[0]["project_name"],
            "product": rows[0]["product"], "asin": asin, "keyword": keyword,
            "pacific_date": pacific_date,
            "path": " → ".join(_display_observation(row) for row in rows),
            "best_rank": min(ranks) if ranks else None,
            "worst_rank": max(ranks) if ranks else None,
            "observed_count": len(ranks),
            "successful_count": sum(row["status"] != "failed" for row in rows),
            "sample_count": len(rows),
        })
    return sorted(result, key=lambda row: (
        row["project_id"], row["asin"], row["keyword"], row["pacific_date"],
    ))


def _final_slot_samples(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["slot"] != "其他":
            grouped[(row["pacific_date"], row["slot"])].append(row)
    result = {}
    for key, values in grouped.items():
        values.sort(key=lambda row: row["pacific_datetime"])
        successful = [row for row in values if row["status"] != "failed"]
        result[key] = (successful or values)[-1]
    return result


def _slot_statistics(samples: dict[tuple[str, str], dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    slots = sorted({slot for _, slot in samples}, key=lambda value: int(value[:2]))
    for slot in slots:
        values = [row for (_, name), row in samples.items() if name == slot and row["status"] != "failed"]
        ranks = [int(row["ad_rank"]) for row in values if row["ad_rank"] is not None]
        result[slot] = {
            "successful": len(values), "observed": len(ranks),
            "observed_rate": len(ranks) / len(values) if values else 0,
            "median_rank": round(median(ranks)) if ranks else None,
        }
    return result


def _facts(stats: dict[str, dict[str, Any]]) -> str:
    parts = []
    for slot, value in stats.items():
        if not value["successful"]:
            continue
        rank = f"，中位第{value['median_rank']}位" if value["median_rank"] is not None else ""
        parts.append(
            f"美西{slot[:2]}时{value['observed']}/{value['successful']}次观察到广告{rank}"
        )
    return "；".join(parts)


def _pair_pattern(
    samples: dict[tuple[str, str], dict[str, Any]], stats: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    eligible = [slot for slot, row in stats.items() if row["successful"] >= 3]
    candidates = []
    for index, first in enumerate(eligible):
        for second in eligible[index + 1:]:
            wins = {first: 0, second: 0}
            comparisons = 0
            dates = sorted({day for day, slot in samples if slot in {first, second}})
            for day in dates:
                left, right = samples.get((day, first)), samples.get((day, second))
                if not left or not right or "failed" in {left["status"], right["status"]}:
                    continue
                winner = None
                if left["status"] == "observed" and right["status"] == "not_observed":
                    winner = first
                elif right["status"] == "observed" and left["status"] == "not_observed":
                    winner = second
                elif left["status"] == right["status"] == "observed":
                    difference = int(left["ad_rank"]) - int(right["ad_rank"])
                    if abs(difference) >= 5:
                        winner = first if difference < 0 else second
                if winner:
                    wins[winner] += 1
                    comparisons += 1
            if comparisons:
                better = max(wins, key=wins.get)
                dominant = wins[better]
                consistency = dominant / comparisons
                worse = second if better == first else first
                candidates.append((dominant, consistency, comparisons, better, worse))
    if not candidates:
        return None
    dominant, consistency, comparisons, better, worse = max(candidates)
    if comparisons >= 4 and consistency >= 0.75:
        confidence = "高"
    elif comparisons >= 3 and consistency >= 0.6:
        confidence = "中"
    else:
        return None
    return {
        "better": better, "worse": worse, "repeat_days": dominant,
        "comparable_days": comparisons, "confidence": confidence,
    }


def _time_inference(better: str, worse: str) -> str:
    better_hour, worse_hour = int(better[:2]), int(worse[:2])
    daytime = {11, 17}
    nighttime = {23, 5}
    if better_hour in daytime and worse_hour in nighttime:
        return "疑似在美西白天提高竞价，并在深夜或清晨降低竞价或暂停投放"
    if better_hour in nighttime and worse_hour in daytime:
        return "疑似在美西深夜或清晨增强竞价，并在白天降低竞价"
    if better_hour in {5, 11} and worse_hour in {17, 23}:
        return "疑似在美西前半天增强投放；也可能是日预算在后半天逐步消耗"
    return f"疑似采用分时竞价，在美西{better[:2]}时附近增强、{worse[:2]}时附近减弱"


def summarize_ad_insights(
    observations: list[dict[str, Any]], week_start: date, week_end: date,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        grouped[(row["project_id"], row["asin"], row["keyword"])].append(row)
    result = []
    current_start, previous_start = week_start.isoformat(), (week_start - timedelta(days=7)).isoformat()
    current_end, previous_end = week_end.isoformat(), (week_start - timedelta(days=1)).isoformat()
    for (project_id, asin, keyword), rows in grouped.items():
        current = [row for row in rows if current_start <= row["pacific_date"] <= current_end]
        previous = [row for row in rows if previous_start <= row["pacific_date"] <= previous_end]
        if not current:
            continue
        samples = _final_slot_samples(current)
        stats = _slot_statistics(samples)
        observation = inference = confidence = ""
        current_success_days = {row["pacific_date"] for row in current if row["status"] != "failed"}
        previous_success_days = {row["pacific_date"] for row in previous if row["status"] != "failed"}
        current_ad_days = {row["pacific_date"] for row in current if row["status"] == "observed"}
        previous_ad_days = {row["pacific_date"] for row in previous if row["status"] == "observed"}
        if len(previous_success_days) >= 3 and not previous_ad_days and len(current_ad_days) >= 3:
            observation = f"上周有{len(current_ad_days)}天观察到广告，前一周在充分采样下未观察到"
            inference, confidence = "可能新开启或明显扩大了该关键词广告投放", "高" if len(current_ad_days) >= 4 else "中"
        elif len(current_success_days) >= 3 and not current_ad_days and len(previous_ad_days) >= 3:
            observation = f"上周在充分采样下未观察到广告，前一周曾有{len(previous_ad_days)}天出现"
            inference, confidence = "可能暂停或明显缩减了该关键词广告投放", "高" if len(previous_ad_days) >= 4 else "中"
        else:
            pattern = _pair_pattern(samples, stats)
            if pattern:
                observation = (
                    f"美西{pattern['better'][:2]}时广告通常强于{pattern['worse'][:2]}时，"
                    f"{pattern['comparable_days']}个可比日中有{pattern['repeat_days']}天重复；{_facts(stats)}"
                )
                inference = _time_inference(pattern["better"], pattern["worse"])
                confidence = pattern["confidence"]
            else:
                by_day: dict[str, list[int]] = defaultdict(list)
                for row in current:
                    if row["ad_rank"] is not None:
                        by_day[row["pacific_date"]].append(int(row["ad_rank"]))
                volatile = [max(values) - min(values) for values in by_day.values() if len(values) >= 2 and max(values) - min(values) >= 10]
                if len(volatile) >= 3:
                    observation = f"有{len(volatile)}个采样日的日内广告位跨度达到10名以上；{_facts(stats)}"
                    inference = "广告位波动较大，但高低时段不固定，可能受动态竞价或实时拍卖竞争影响"
                    confidence = "高" if len(volatile) >= 4 else "中"
                else:
                    eligible = [value for value in stats.values() if value["successful"] >= 3]
                    medians = [value["median_rank"] for value in eligible if value["median_rank"] is not None]
                    if len(eligible) >= 3 and all(value["observed_rate"] >= 0.75 for value in eligible) and medians and max(medians) - min(medians) < 5:
                        observation = f"多个时段持续观察到广告且中位排名接近；{_facts(stats)}"
                        inference, confidence = "可能采用全天相对稳定的广告竞价", "中"
        if observation:
            result.append({
                "project_id": project_id, "project_name": current[0]["project_name"],
                "product": current[0]["product"], "asin": asin, "keyword": keyword,
                "observation": observation, "inference": inference,
                "confidence": confidence, "slot_statistics": stats,
            })
    confidence_order = {"高": 0, "中": 1}
    return sorted(result, key=lambda row: (
        row["project_id"], confidence_order.get(row["confidence"], 9), row["product"], row["keyword"],
    ))


def build_weekly_ad_insights(db: Database, week_end: date) -> list[dict[str, Any]]:
    week_start = week_end - timedelta(days=6)
    observations = collect_ad_observations(db, week_start - timedelta(days=7), week_end)
    return summarize_ad_insights(observations, week_start, week_end)
