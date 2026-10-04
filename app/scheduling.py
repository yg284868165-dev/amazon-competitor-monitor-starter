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


def _scheduled_projects(config: dict[str, Any]) -> list[Any]:
    selector = config.get("project", "all")
    return [item for item in load_projects() if selector in {"all", item.project_id}]


def _next_slot(slot: datetime, config: dict[str, Any]) -> datetime:
    """Return the next configured schedule boundary after ``slot``.

    A collection batch can legitimately begin after the first project in the
    same slot has finished, so a short fixed grace period would incorrectly
    detach later projects.  The next schedule boundary is the stable window
    available with the existing schema.
    """
    values = [str(value) for value in config.get("times", [])]
    for offset in range(3):
        day = (slot + timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        candidates = []
        for value in values:
            hour, minute = (int(part) for part in value.split(":"))
            candidate = day.replace(hour=hour, minute=minute)
            if candidate > slot:
                candidates.append(candidate)
        if candidates:
            return min(candidates)
    return slot + timedelta(days=1)


def _real_root_runs_for_slot(db: Database, slot: datetime, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Find roots created by a slot, excluding synthetic missed-start runs."""
    projects = _scheduled_projects(config)
    if not projects:
        return []
    project_ids = [item.project_id for item in projects]
    placeholders = ",".join("?" for _ in project_ids)
    window_end = _next_slot(slot, config)
    return db.fetchall(
        f"""SELECT run.* FROM collection_runs run
            WHERE run.source_run_id IS NULL
              AND run.task_type=?
              AND run.project_id IN ({placeholders})
              AND datetime(run.started_at)>=datetime(?)
              AND datetime(run.started_at)<datetime(?)
              AND NOT EXISTS(
                  SELECT 1 FROM collection_errors error
                  WHERE error.run_id=run.run_id
                    AND error.error_type IN ('schedule_missed','schedule_start_failed')
              )
            ORDER BY run.project_id,run.started_at""",
        (
            config.get("task", "all"), *project_ids,
            slot.isoformat(timespec="seconds"), window_end.isoformat(timespec="seconds"),
        ),
    )


def _run_tree(db: Database, root_run_id: str) -> list[dict[str, Any]]:
    return db.fetchall(
        """WITH RECURSIVE chain(run_id) AS (
               SELECT run_id FROM collection_runs WHERE run_id=?
               UNION ALL
               SELECT child.run_id FROM collection_runs child
               JOIN chain parent ON child.source_run_id=parent.run_id
           )
           SELECT run.* FROM collection_runs run JOIN chain ON chain.run_id=run.run_id
           ORDER BY run.started_at""",
        (root_run_id,),
    )


def _ensure_manifest_for_recovery(db: Database, run: dict[str, Any]) -> None:
    """Backfill only legacy manifests before marking an interrupted run failed."""
    existing = db.fetchall(
        "SELECT 1 FROM collection_task_outcomes WHERE run_id=? LIMIT 1", (run["run_id"],),
    )
    if existing or run["task_type"] not in {"product", "search", "bestseller", "all"}:
        return
    db.record_targets(run["run_id"], run["project_id"], _targets(run["project_id"], run["task_type"]))


def _recover_orphaned_tree(db: Database, root_run_id: str, now: datetime) -> list[dict[str, Any]]:
    """Finish all unfinished members of a root/retry chain after wrapper loss."""
    message = "采集进程因系统重启或异常退出而中断；未完成目标可在管理页面重试"
    recovered: list[dict[str, Any]] = []
    for run in _run_tree(db, root_run_id):
        if run["status"] != "running":
            continue
        _ensure_manifest_for_recovery(db, run)
        result = db.mark_run_interrupted(
            run["run_id"], "process_interrupted", message, finished_at=now,
        )
        if result:
            recovered.append(result)
    return recovered


def _effective_root_status(db: Database, root_run_id: str) -> str:
    tree = _run_tree(db, root_run_id)
    statuses = [str(run["status"]) for run in tree]
    if "running" in statuses:
        return "running"
    # A completed retry is the final result for the original scheduled batch.
    if "success" in statuses:
        return "success"
    if any(status in {"partial", "failed"} for status in statuses):
        return "failed"
    return statuses[-1] if statuses else "failed"


def _cancel_pending_schedule_missed_alerts(
    db: Database, slot: datetime, task_type: str, project_ids: set[str],
) -> int:
    """Suppress only unsent duplicate missed-start notifications.

    Sent messages are historical facts and must remain untouched.  The source
    runs themselves are intentionally retained for audit/recovery rather than
    being deleted here.
    """
    if not project_ids:
        return 0
    placeholders = ",".join("?" for _ in project_ids)
    with db.connect() as connection:
        cursor = connection.execute(
            f"""UPDATE collection_alerts AS alert
                SET delivery_status='cancelled',
                    last_error='已对账：同一时段已有真实采集批次，取消重复漏执行告警'
                WHERE alert.delivery_status='pending'
                  AND EXISTS(
                      SELECT 1 FROM collection_runs run
                      JOIN collection_errors error ON error.run_id=run.run_id
                      WHERE run.run_id=alert.source_run_id
                        AND run.started_at=?
                        AND run.task_type=?
                        AND run.project_id IN ({placeholders})
                        AND error.error_type='schedule_missed'
                  )""",
            (
                slot.isoformat(timespec="seconds"), task_type, *sorted(project_ids),
            ),
        )
        return cursor.rowcount


def _supersede_false_schedule_missed_runs(
    db: Database, slot: datetime, task_type: str, project_ids: set[str],
) -> int:
    """Keep synthetic audit rows, but remove them from the retryable failure state."""
    if not project_ids:
        return 0
    placeholders = ",".join("?" for _ in project_ids)
    with db.connect() as connection:
        cursor = connection.execute(
            f"""UPDATE collection_runs AS run SET status='superseded'
                WHERE run.started_at=? AND run.task_type=?
                  AND run.project_id IN ({placeholders})
                  AND run.status<>'superseded'
                  AND EXISTS(
                      SELECT 1 FROM collection_errors error
                      WHERE error.run_id=run.run_id
                        AND error.error_type='schedule_missed'
                  )""",
            (slot.isoformat(timespec="seconds"), task_type, *sorted(project_ids)),
        )
        return cursor.rowcount


def _has_sent_schedule_missed_alert(
    db: Database, slot: datetime, task_type: str, project_id: str,
) -> bool:
    rows = db.fetchall(
        """SELECT 1 FROM collection_alerts alert
           JOIN collection_runs run ON run.run_id=alert.source_run_id
           JOIN collection_errors error ON error.run_id=run.run_id
           WHERE alert.delivery_status='sent'
             AND run.started_at=? AND run.task_type=? AND run.project_id=?
             AND error.error_type='schedule_missed'
           LIMIT 1""",
        (slot.isoformat(timespec="seconds"), task_type, project_id),
    )
    return bool(rows)


def _finish_reconciled_slot(
    db: Database, slot: datetime, config: dict[str, Any], exit_code: int, now: datetime,
) -> None:
    """Finish an existing claimed slot, or persist an equivalent recovered slot."""
    status = "success" if exit_code == 0 else "failed"
    stamp = now.isoformat(timespec="seconds")
    with db.connect() as connection:
        connection.execute(
            """INSERT INTO scheduled_executions(
                   slot_time,task_type,project_selector,claimed_at,finished_at,status,exit_code,delay_seconds
               ) VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(slot_time) DO UPDATE SET
                   finished_at=excluded.finished_at,status=excluded.status,
                   exit_code=excluded.exit_code""",
            (
                slot.isoformat(timespec="seconds"), config.get("task", "all"),
                config.get("project", "all"), stamp, stamp, status, int(exit_code),
                max(0, int((now - slot).total_seconds())),
            ),
        )


def reconcile_slot(
    db: Database, slot: datetime, config: dict[str, Any], now: datetime | None = None,
) -> dict[str, Any]:
    """Reconcile a stale schedule slot with the actual collection roots.

    ``scheduled_executions`` predates a durable run-to-slot foreign key.  Until
    that schema can be evolved, roots are matched by task/project and the time
    range from this slot to the next configured slot.  This is deliberately
    performed before any missed-slot synthesis.
    """
    current = (now or datetime.now()).replace(microsecond=0)
    roots = _real_root_runs_for_slot(db, slot, config)
    if not roots:
        return {"matched": False, "cancelled_alerts": 0, "recovered_runs": []}

    actual_projects = {str(run["project_id"]) for run in roots}
    cancelled = _cancel_pending_schedule_missed_alerts(
        db, slot, str(config.get("task", "all")), actual_projects,
    )
    superseded = _supersede_false_schedule_missed_runs(
        db, slot, str(config.get("task", "all")), actual_projects,
    )
    recovered: list[dict[str, Any]] = []
    for root in roots:
        recovered.extend(_recover_orphaned_tree(db, str(root["run_id"]), current))

    # Refresh statuses after an interrupted root/retry chain was terminalized.
    project_roots: dict[str, list[str]] = {}
    for root in roots:
        project_roots.setdefault(str(root["project_id"]), []).append(str(root["run_id"]))
    project_success = {
        project_id: any(_effective_root_status(db, run_id) == "success" for run_id in run_ids)
        for project_id, run_ids in project_roots.items()
    }
    expected_projects = {item.project_id for item in _scheduled_projects(config)}
    missing_projects = expected_projects - actual_projects

    # A reboot may happen after project A has begun but before project B is
    # created.  Make only B retryable; never manufacture a full duplicate batch.
    missing_run_ids: list[str] = []
    if missing_projects:
        missing_run_ids = record_failed_schedule_projects(
            db, slot, config, current,
            "schedule_missed", "macOS 定时任务未启动采集程序", project_ids=missing_projects,
        )

    failed_roots = [
        root for root in roots
        if not project_success.get(str(root["project_id"]), False)
    ]
    for root in failed_roots:
        # Do not issue a second notification for a false missed-start alert
        # which was already sent before this recovery logic existed.
        if _has_sent_schedule_missed_alert(
            db, slot, str(config.get("task", "all")), str(root["project_id"]),
        ):
            continue
        queue_scheduled_collection_alerts(db, [str(root["run_id"])])

    exit_code = 0 if not missing_projects and all(project_success.values()) else 2
    _finish_reconciled_slot(db, slot, config, exit_code, current)
    return {
        "matched": True,
        "cancelled_alerts": cancelled,
        "superseded_runs": superseded,
        "recovered_runs": recovered,
        "missing_run_ids": missing_run_ids,
        "exit_code": exit_code,
    }


def reconcile_scheduled_slots(
    db: Database, slots: list[datetime], config: dict[str, Any], now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Reconcile stale running/missed slots before deciding what is pending."""
    current = (now or datetime.now()).replace(microsecond=0)
    if not slots:
        return []
    slot_keys = [slot.isoformat(timespec="seconds") for slot in slots]
    placeholders = ",".join("?" for _ in slot_keys)
    rows = db.fetchall(
        f"""SELECT slot_time,status,task_type,project_selector FROM scheduled_executions
            WHERE slot_time IN ({placeholders}) AND status IN ('running','missed')""",
        tuple(slot_keys),
    )
    results: list[dict[str, Any]] = []
    for row in rows:
        slot_config = dict(config)
        slot_config["task"] = row["task_type"]
        slot_config["project"] = row["project_selector"]
        result = reconcile_slot(
            db, datetime.fromisoformat(str(row["slot_time"])), slot_config, current,
        )
        if result["matched"]:
            results.append(result)
    return results


def _targets(project_id: str, task: str) -> dict[str, set[str]]:
    pages = int(load_settings()["search"].get("default_pages", 3))
    return {
        "product": {item.asin for item in load_competitors(project_id)} if task in {"product", "all"} else set(),
        "search": {item.keyword for item in load_keywords(pages, project_id)} if task in {"search", "all"} else set(),
        "bestseller": {item.category_name for item in load_categories(project_id)} if task in {"bestseller", "all"} else set(),
    }


def record_failed_schedule_projects(
    db: Database, slot: datetime, config: dict[str, Any], detected_at: datetime,
    error_type: str, error_message: str, project_ids: set[str] | None = None,
) -> list[str]:
    selected = _scheduled_projects(config)
    if project_ids is not None:
        selected = [item for item in selected if item.project_id in project_ids]
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
    # Never convert a slot with real collection roots into a synthetic all-target
    # failure.  This also repairs historical "missed" records on the next
    # scheduler launch before their pending alerts are flushed.  A slot without
    # a prior execution record has no evidence it was a scheduled invocation;
    # do not let an unrelated manual batch in the same time window hide a real
    # missed schedule.
    execution = db.fetchall(
        "SELECT status FROM scheduled_executions WHERE slot_time=?",
        (slot.isoformat(timespec="seconds"),),
    )
    if execution and execution[0]["status"] in {"running", "missed"}:
        if reconcile_slot(db, slot, config, detected)["matched"]:
            return []
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
