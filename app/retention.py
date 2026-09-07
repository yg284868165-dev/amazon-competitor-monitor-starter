from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.paths import HTML_DIR, LOG_DIR, SCREENSHOT_DIR
from app.storage.database import Database


LOGGER = logging.getLogger("monitor")

TIMED_TABLES = {
    "product_snapshots": "collected_at",
    "search_snapshots": "collected_at",
    "bestseller_snapshots": "collected_at",
    "change_events": "event_time",
    "bsr_new_candidates": "last_seen_at",
    "notification_logs": "sent_at",
    "collection_alerts": "created_at",
}


def _retention_days(value: Any, default: int) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        days = default
    return max(1, days)


def cleanup_database_history(
    db: Database, history_days: int, now: datetime | None = None, vacuum: bool = True,
) -> dict[str, int]:
    cutoff = (now or datetime.now()) - timedelta(days=_retention_days(history_days, 30))
    cutoff_text = cutoff.isoformat(timespec="seconds")
    deleted: dict[str, int] = {}
    with db.connect() as connection:
        keep_rows = connection.execute(
            """
            WITH RECURSIVE keep(run_id) AS (
                SELECT run_id FROM collection_runs WHERE datetime(started_at)>=datetime(?)
                UNION
                SELECT runs.source_run_id
                FROM collection_runs runs JOIN keep ON runs.run_id=keep.run_id
                WHERE runs.source_run_id IS NOT NULL
            )
            SELECT run_id FROM keep
            """,
            (cutoff_text,),
        ).fetchall()
        keep_run_ids = [row[0] for row in keep_rows]
        run_placeholders = ",".join("?" for _ in keep_run_ids)
        preserve_run_clause = f" AND run_id NOT IN ({run_placeholders})" if keep_run_ids else ""

        for table, column in TIMED_TABLES.items():
            cursor = connection.execute(
                f"DELETE FROM {table} WHERE datetime({column})<datetime(?)",
                (cutoff_text,),
            )
            deleted[table] = cursor.rowcount

        for table, column in {
            "collection_errors": "created_at",
            "collection_task_outcomes": "finished_at",
        }.items():
            cursor = connection.execute(
                f"DELETE FROM {table} WHERE datetime({column})<datetime(?)" + preserve_run_clause,
                (cutoff_text, *keep_run_ids),
            )
            deleted[table] = cursor.rowcount

        cursor = connection.execute(
            "DELETE FROM collection_runs WHERE datetime(started_at)<datetime(?)" +
            (f" AND run_id NOT IN ({run_placeholders})" if keep_run_ids else ""),
            (cutoff_text, *keep_run_ids),
        )
        deleted["collection_runs"] = cursor.rowcount

    if vacuum and sum(deleted.values()):
        with sqlite3.connect(db.path) as connection:
            connection.execute("VACUUM")
    return deleted


def cleanup_files(directory: Path, retention_days: int, now: datetime | None = None) -> int:
    if not directory.exists():
        return 0
    cutoff_timestamp = ((now or datetime.now()) - timedelta(days=_retention_days(retention_days, 30))).timestamp()
    deleted = 0
    for path in directory.iterdir():
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            if path.stat().st_mtime < cutoff_timestamp:
                path.unlink()
                deleted += 1
        except FileNotFoundError:
            continue
    return deleted


def cleanup_logs(directory: Path, retention_days: int, now: datetime | None = None) -> int:
    """Delete dated logs and truncate reusable fixed-name logs after the retention window."""
    if not directory.exists():
        return 0
    cutoff_timestamp = ((now or datetime.now()) - timedelta(days=_retention_days(retention_days, 90))).timestamp()
    cleaned = 0
    for path in directory.iterdir():
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            if path.stat().st_mtime >= cutoff_timestamp:
                continue
            if path.name.startswith("monitor_"):
                path.unlink()
            else:
                path.write_text("", encoding="utf-8")
            cleaned += 1
        except FileNotFoundError:
            continue
    return cleaned


def apply_retention(
    db: Database, settings: dict[str, Any], now: datetime | None = None,
    screenshot_dir: Path = SCREENSHOT_DIR, html_dir: Path = HTML_DIR, log_dir: Path = LOG_DIR,
) -> dict[str, Any]:
    config = settings.get("retention", {})
    history_days = _retention_days(config.get("history_days"), 30)
    diagnostics_days = _retention_days(config.get("diagnostics_days"), 30)
    logs_days = _retention_days(config.get("logs_days"), 90)
    result = {
        "database": cleanup_database_history(db, history_days, now),
        "screenshots": cleanup_files(screenshot_dir, diagnostics_days, now),
        "html": cleanup_files(html_dir, diagnostics_days, now),
        "logs": cleanup_logs(log_dir, logs_days, now),
    }
    total = sum(result["database"].values()) + result["screenshots"] + result["html"] + result["logs"]
    if total:
        LOGGER.info(
            "保留策略清理完成: 数据库=%d 截图=%d HTML=%d 日志=%d",
            sum(result["database"].values()), result["screenshots"], result["html"], result["logs"],
        )
    return result
