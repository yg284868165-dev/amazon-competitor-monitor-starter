from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from app.config import load_categories, load_competitors, load_keywords, load_projects, load_settings
from app.notifications.collection_alerts import flush_pending_collection_alerts, queue_scheduled_collection_alerts
from app.storage.database import Database


CATCH_UP_MINUTES = 10
MAX_LOOKBACK_DAYS = 30


def schedule_signature(config: dict[str, Any]) -> str:
    payload = {
        "times": sorted(dict.fromkeys(str(value) for value in config.get("times", []))),
        "task": config.get("task", "all"),
        "project": config.get("project", "all"),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def activate_schedule(db: Database, config: dict[str, Any], now: datetime | None = None) -> None:
    activated_at = (now or datetime.now()).isoformat(timespec="seconds")
    signature = schedule_signature(config)
    with db.connect() as connection:
        current = connection.execute("SELECT signature,enabled FROM scheduler_state WHERE id=1").fetchone()
        if current and current["signature"] == signature and int(current["enabled"]):
            return
        connection.execute(
            """INSERT INTO scheduler_state(id,activated_at,signature,enabled) VALUES(1,?,?,1)
               ON CONFLICT(id) DO UPDATE SET activated_at=excluded.activated_at,
                   signature=excluded.signature,enabled=1""",
            (activated_at, signature),
        )


def deactivate_schedule(db: Database) -> None:
    with db.connect() as connection:
        connection.execute("UPDATE scheduler_state SET enabled=0 WHERE id=1")


def _activation_time(db: Database, config: dict[str, Any], now: datetime) -> datetime:
    rows = db.fetchall("SELECT activated_at,signature,enabled FROM scheduler_state WHERE id=1")
    if not rows or not int(rows[0]["enabled"]) or rows[0]["signature"] != schedule_signature(config):
        activate_schedule(db, config, now)
        return now
    return datetime.fromisoformat(str(rows[0]["activated_at"]))


def due_slots(db: Database, config: dict[str, Any], now: datetime | None = None) -> list[datetime]:
    current = (now or datetime.now()).replace(microsecond=0)
    activated = max(_activation_time(db, config, current), current - timedelta(days=MAX_LOOKBACK_DAYS))
    day = activated.replace(hour=0, minute=0, second=0, microsecond=0)
    result: list[datetime] = []
    while day.date() <= current.date():
        for value in config.get("times", []):
            hour, minute = (int(part) for part in str(value).split(":"))
            slot = day.replace(hour=hour, minute=minute)
            if activated <= slot <= current:
                result.append(slot)
        day += timedelta(days=1)
    return sorted(set(result))


def claim_slot(
    db: Database, slot: datetime, config: dict[str, Any], now: datetime | None = None,
) -> bool:
    claimed_at = (now or datetime.now()).replace(microsecond=0)
    with db.connect() as connection:
        cursor = connection.execute(
            """INSERT OR IGNORE INTO scheduled_executions(
                   slot_time,task_type,project_selector,claimed_at,status,delay_seconds
               ) VALUES(?,?,?,?,?,?)""",
            (
                slot.isoformat(timespec="seconds"), config.get("task", "all"),
                config.get("project", "all"), claimed_at.isoformat(timespec="seconds"),
                "running", max(0, int((claimed_at - slot).total_seconds())),
            ),
        )
        if cursor.rowcount == 1:
            return True
        # launchd does not run two instances of one job concurrently. If a later invocation
        # sees a slot still marked running, the former wrapper ended before it could finish.
        cursor = connection.execute(
            """UPDATE scheduled_executions SET claimed_at=?,delay_seconds=?
               WHERE slot_time=? AND status='running'""",
            (
                claimed_at.isoformat(timespec="seconds"),
                max(0, int((claimed_at - slot).total_seconds())),
                slot.isoformat(timespec="seconds"),
            ),
        )
        return cursor.rowcount == 1


def finish_slot(db: Database, slot: datetime, exit_code: int, now: datetime | None = None) -> None:
    with db.connect() as connection:
        connection.execute(
            """UPDATE scheduled_executions SET finished_at=?,status=?,exit_code=?
               WHERE slot_time=?""",
            (
                (now or datetime.now()).isoformat(timespec="seconds"),
                "success" if exit_code == 0 else "failed", int(exit_code),
                slot.isoformat(timespec="seconds"),
            ),
        )


def _targets(project_id: str, task: str) -> dict[str, set[str]]:
    pages = int(load_settings()["search"].get("default_pages", 3))
    return {
        "product": {item.asin for item in load_competitors(project_id)} if task in {"product", "all"} else set(),
        "search": {item.keyword for item in load_keywords(pages, project_id)} if task in {"search", "all"} else set(),
        "bestseller": {item.category_name for item in load_categories(project_id)} if task in {"bestseller", "all"} else set(),
    }


def record_failed_schedule_projects(
    db: Database, slot: datetime, config: dict[str, Any], detected_at: datetime,
    error_type: str, error_message: str,
) -> list[str]:
    selected = [item for item in load_projects() if config.get("project", "all") in {"all", item.project_id}]
    run_ids: list[str] = []
    for project in selected:
        targets = _targets(project.project_id, config.get("task", "all"))
        total = sum(len(values) for values in targets.values())
        if not total:
            continue
        run_id = db.start_run(
            project.project_id, config.get("task", "all"), total,
            started_at=slot.isoformat(timespec="seconds"),
        )
        db.record_targets(run_id, project.project_id, targets)
        for task_type, values in targets.items():
            for target in values:
                db.insert("collection_errors", {
                    "run_id": run_id, "project_id": project.project_id,
                    "task_type": task_type, "target": target,
                    "error_type": error_type, "error_message": error_message,
                    "created_at": detected_at.isoformat(timespec="seconds"),
                })
        db.finish_run(run_id, 0, total)
        run_ids.append(run_id)
    queue_scheduled_collection_alerts(db, run_ids)
    flush_pending_collection_alerts(db)
    return run_ids


def record_missed_slot(
    db: Database, slot: datetime, config: dict[str, Any], detected_at: datetime | None = None,
) -> list[str]:
    detected = (detected_at or datetime.now()).replace(microsecond=0)
    with db.connect() as connection:
        cursor = connection.execute(
            """INSERT OR IGNORE INTO scheduled_executions(
                   slot_time,task_type,project_selector,claimed_at,finished_at,status,exit_code,delay_seconds
               ) VALUES(?,?,?,?,?,'missed',1,?)""",
            (
                slot.isoformat(timespec="seconds"), config.get("task", "all"),
                config.get("project", "all"), detected.isoformat(timespec="seconds"),
                detected.isoformat(timespec="seconds"), max(0, int((detected - slot).total_seconds())),
            ),
        )
        inserted = cursor.rowcount == 1
        if not inserted:
            cursor = connection.execute(
                """UPDATE scheduled_executions SET claimed_at=?,finished_at=?,status='missed',
                       exit_code=1,delay_seconds=? WHERE slot_time=? AND status='running'""",
                (
                    detected.isoformat(timespec="seconds"), detected.isoformat(timespec="seconds"),
                    max(0, int((detected - slot).total_seconds())), slot.isoformat(timespec="seconds"),
                ),
            )
            if cursor.rowcount != 1:
                return []
    return record_failed_schedule_projects(
        db, slot, config, detected, "schedule_missed", "macOS 定时任务未启动采集程序",
    )


def queue_delayed_start_alert(db: Database, slot: datetime, delay_seconds: int) -> None:
    if delay_seconds < 120:
        return
    title = "Amazon定时采集延迟启动｜已自动补跑"
    body = "\n".join([
        f"## {title}", "",
        f"原定采集时间：{slot.isoformat(timespec='seconds').replace('T', ' ')}",
        f"延迟时长：约{max(1, round(delay_seconds / 60))}分钟",
        "处理结果：守护检查已重新唤起采集任务，最终采集结果会按原有规则记录；如仍有失败项将另行通知。",
    ])
    db.queue_collection_alert(
        f"schedule-delay:{slot.isoformat(timespec='seconds')}", "all", "delayed", title, body,
    )
