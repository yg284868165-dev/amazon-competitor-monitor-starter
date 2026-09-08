from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from app.paths import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_runs (
    run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL DEFAULT 'default', task_type TEXT NOT NULL, started_at TEXT NOT NULL,
    finished_at TEXT, status TEXT NOT NULL, total_tasks INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0, failed_count INTEGER DEFAULT 0,
    captcha_count INTEGER DEFAULT 0, report_path TEXT, source_run_id TEXT
);
CREATE TABLE IF NOT EXISTS product_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'default', asin TEXT NOT NULL,
    collected_at TEXT NOT NULL, title TEXT, brand TEXT, current_price REAL,
    list_price REAL, coupon_text TEXT, deal_text TEXT, business_price_text TEXT,
    rating_count INTEGER, rating_value REAL, rating_breakdown_json TEXT,
    main_bsr INTEGER, category_ranks_json TEXT,
    amazon_choice INTEGER, best_seller INTEGER, new_release INTEGER,
    high_return_rate INTEGER, high_return_rate_text TEXT,
    listing_status TEXT, listing_status_detail TEXT,
    availability TEXT, delivery_text TEXT, featured_seller TEXT, ships_from TEXT,
    offer_count INTEGER, variation_count INTEGER, main_image_url TEXT,
    title_hash TEXT, image_hash TEXT, highlights_json TEXT, highlights_text TEXT, highlights_hash TEXT,
    about_items_json TEXT, about_items_hash TEXT, product_images_json TEXT,
    product_images_hash TEXT, product_image_count INTEGER,
    date_first_available TEXT, date_first_available_text TEXT,
    source_url TEXT, success INTEGER NOT NULL,
    confidence TEXT NOT NULL, raw_json TEXT,
    UNIQUE(run_id, asin)
);
CREATE INDEX IF NOT EXISTS idx_product_asin_time ON product_snapshots(asin, collected_at);
CREATE TABLE IF NOT EXISTS search_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'default', keyword TEXT NOT NULL,
    page INTEGER NOT NULL, absolute_position INTEGER, organic_rank INTEGER, ad_rank INTEGER,
    asin TEXT NOT NULL, is_sponsored INTEGER NOT NULL, ad_type TEXT, title TEXT,
    price REAL, coupon_text TEXT, rating_count INTEGER, rating_value REAL,
    badges_json TEXT, collected_at TEXT NOT NULL,
    UNIQUE(run_id, keyword, page, asin, absolute_position)
);
CREATE INDEX IF NOT EXISTS idx_search_keyword_time ON search_snapshots(keyword, collected_at);
CREATE TABLE IF NOT EXISTS bestseller_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'default', category_name TEXT NOT NULL,
    snapshot_date TEXT NOT NULL, rank INTEGER NOT NULL, asin TEXT NOT NULL, title TEXT,
    brand TEXT, price REAL, rating_count INTEGER, rating_value REAL, badges_json TEXT,
    source_url TEXT, collected_at TEXT NOT NULL,
    UNIQUE(run_id, category_name, rank)
);
CREATE INDEX IF NOT EXISTS idx_bestseller_category_date ON bestseller_snapshots(category_name, snapshot_date);
CREATE TABLE IF NOT EXISTS change_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'default', event_type TEXT NOT NULL,
    severity TEXT NOT NULL, source_type TEXT NOT NULL, asin TEXT, keyword TEXT,
    category_name TEXT, field_name TEXT, old_value TEXT, new_value TEXT,
    change_value REAL, event_time TEXT NOT NULL, confirmed INTEGER DEFAULT 1,
    message TEXT NOT NULL, details_json TEXT
);
CREATE TABLE IF NOT EXISTS collection_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'default', task_type TEXT NOT NULL,
    target TEXT NOT NULL, url TEXT, error_type TEXT NOT NULL, error_message TEXT,
    retry_count INTEGER DEFAULT 0, screenshot_path TEXT, html_path TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collection_task_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, project_id TEXT NOT NULL,
    task_type TEXT NOT NULL, target TEXT NOT NULL, success INTEGER NOT NULL,
    finished_at TEXT NOT NULL, UNIQUE(run_id, task_type, target)
);
CREATE TABLE IF NOT EXISTS notification_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, report_date TEXT NOT NULL, channel TEXT NOT NULL,
    sent_at TEXT NOT NULL, success INTEGER NOT NULL, title TEXT NOT NULL,
    item_count INTEGER DEFAULT 0, response_message TEXT, body TEXT
);
CREATE TABLE IF NOT EXISTS collection_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source_run_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL, created_at TEXT NOT NULL, final_status TEXT NOT NULL,
    title TEXT NOT NULL, body TEXT NOT NULL, delivery_status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0, last_attempt_at TEXT, last_error TEXT, sent_at TEXT
);
CREATE TABLE IF NOT EXISTS bsr_new_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, asin TEXT NOT NULL,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, category_name TEXT,
    current_rank INTEGER, title TEXT, brand TEXT, date_first_available TEXT,
    date_source TEXT,
    age_days INTEGER, relevance_status TEXT NOT NULL DEFAULT 'pending',
    classification_source TEXT NOT NULL DEFAULT 'auto', relevance_reason TEXT,
    detail_url TEXT, last_checked_at TEXT, alerted_at TEXT,
    UNIQUE(project_id, asin)
);
"""

PROJECT_TABLES = (
    "collection_runs", "product_snapshots", "search_snapshots",
    "bestseller_snapshots", "change_events", "collection_errors", "collection_task_outcomes",
    "bsr_new_candidates", "collection_alerts",
)


class Database:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        run_columns = {row[1] for row in connection.execute("PRAGMA table_info(collection_runs)")}
        if "source_run_id" not in run_columns:
            connection.execute("ALTER TABLE collection_runs ADD COLUMN source_run_id TEXT")
        existing = {row[1] for row in connection.execute("PRAGMA table_info(product_snapshots)")}
        for column, definition in {
            "highlights_json": "TEXT",
            "highlights_text": "TEXT",
            "highlights_hash": "TEXT",
            "about_items_json": "TEXT",
            "about_items_hash": "TEXT",
            "product_images_json": "TEXT",
            "product_images_hash": "TEXT",
            "product_image_count": "INTEGER",
            "date_first_available": "TEXT",
            "date_first_available_text": "TEXT",
            "high_return_rate": "INTEGER",
            "high_return_rate_text": "TEXT",
            "listing_status": "TEXT",
            "listing_status_detail": "TEXT",
            "business_price_text": "TEXT",
            "rating_breakdown_json": "TEXT",
        }.items():
            if column not in existing:
                connection.execute(f"ALTER TABLE product_snapshots ADD COLUMN {column} {definition}")
        connection.execute(
            "UPDATE product_snapshots SET listing_status='active' "
            "WHERE success=1 AND listing_status IS NULL"
        )
        connection.execute(
            "UPDATE product_snapshots SET deal_text='Limited time deal' "
            "WHERE deal_text LIKE '%NO_OF_%' AND lower(deal_text) LIKE '%limited time deal%'"
        )
        candidate_columns = {row[1] for row in connection.execute("PRAGMA table_info(bsr_new_candidates)")}
        if "date_source" not in candidate_columns:
            connection.execute("ALTER TABLE bsr_new_candidates ADD COLUMN date_source TEXT")
        event_columns = {row[1] for row in connection.execute("PRAGMA table_info(change_events)")}
        if "details_json" not in event_columns:
            connection.execute("ALTER TABLE change_events ADD COLUMN details_json TEXT")
        notification_columns = {row[1] for row in connection.execute("PRAGMA table_info(notification_logs)")}
        if "body" not in notification_columns:
            connection.execute("ALTER TABLE notification_logs ADD COLUMN body TEXT")
        connection.execute(
            "UPDATE bsr_new_candidates SET date_source='auto' "
            "WHERE date_first_available IS NOT NULL AND date_source IS NULL"
        )
        for table in PROJECT_TABLES:
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if "project_id" not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN project_id TEXT NOT NULL DEFAULT 'default'")
        connection.execute("""
            INSERT OR IGNORE INTO bsr_new_candidates(
                project_id,asin,first_seen_at,last_seen_at,category_name,current_rank,title,brand,
                relevance_status,classification_source,relevance_reason,detail_url,alerted_at
            )
            SELECT e.project_id,e.asin,e.event_time,e.event_time,e.category_name,CAST(e.new_value AS INTEGER),
                   (SELECT b.title FROM bestseller_snapshots b WHERE b.project_id=e.project_id AND b.asin=e.asin ORDER BY b.collected_at DESC LIMIT 1),
                   (SELECT b.brand FROM bestseller_snapshots b WHERE b.project_id=e.project_id AND b.asin=e.asin ORDER BY b.collected_at DESC LIMIT 1),
                   'pending','auto','历史榜单新ASIN，等待补充筛选','https://www.amazon.com/dp/' || e.asin,e.event_time
            FROM change_events e
            WHERE e.event_type='entered_bestseller' AND e.asin IS NOT NULL
        """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def start_run(self, project_id: str, task_type: str, total_tasks: int, source_run_id: str | None = None) -> str:
        run_id = uuid.uuid4().hex
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO collection_runs(run_id, project_id, task_type, started_at, status, total_tasks, source_run_id) VALUES(?,?,?,?,?,?,?)",
                (run_id, project_id, task_type, datetime.now().isoformat(timespec="seconds"), "running", total_tasks, source_run_id),
            )
        return run_id

    def finish_run(self, run_id: str, success: int, failed: int, captcha: int = 0, report_path: str | None = None) -> None:
        status = "success" if failed == 0 else ("partial" if success else "failed")
        with self.connect() as connection:
            connection.execute(
                "UPDATE collection_runs SET finished_at=?, status=?, success_count=?, failed_count=?, captcha_count=?, report_path=? WHERE run_id=?",
                (datetime.now().isoformat(timespec="seconds"), status, success, failed, captcha, report_path, run_id),
            )

    def insert(self, table: str, values: dict[str, Any]) -> None:
        allowed = {"product_snapshots", "search_snapshots", "bestseller_snapshots", "change_events", "collection_errors", "collection_task_outcomes"}
        if table not in allowed:
            raise ValueError(f"Unsupported table: {table}")
        normalized = {key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for key, value in values.items()}
        columns = ",".join(normalized)
        placeholders = ",".join("?" for _ in normalized)
        with self.connect() as connection:
            connection.execute(f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})", tuple(normalized.values()))

    def upsert_candidate(self, values: dict[str, Any]) -> None:
        normalized = {key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for key, value in values.items()}
        columns = ",".join(normalized)
        placeholders = ",".join("?" for _ in normalized)
        updates = ",".join(f"{column}=excluded.{column}" for column in normalized if column not in {"project_id", "asin", "first_seen_at"})
        with self.connect() as connection:
            connection.execute(
                f"INSERT INTO bsr_new_candidates ({columns}) VALUES ({placeholders}) ON CONFLICT(project_id,asin) DO UPDATE SET {updates}",
                tuple(normalized.values()),
            )

    def record_outcome(self, run_id: str, project_id: str, task_type: str, target: str, success: bool) -> None:
        self.insert("collection_task_outcomes", {
            "run_id": run_id, "project_id": project_id, "task_type": task_type,
            "target": target, "success": int(success),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        })

    def record_targets(self, run_id: str, project_id: str, targets: dict[str, set[str]]) -> None:
        """Persist the full task manifest before browser initialization can fail."""
        created_at = datetime.now().isoformat(timespec="seconds")
        rows = [
            (run_id, project_id, task_type, target, created_at)
            for task_type, values in targets.items()
            for target in values
        ]
        if not rows:
            return
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO collection_task_outcomes(
                    run_id,project_id,task_type,target,success,finished_at
                ) VALUES(?,?,?,?,0,?)
                """,
                rows,
            )

    def fetchall(self, query: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters).fetchall()]

    def record_notification(
        self, report_date: str, success: bool, title: str, item_count: int,
        response_message: str, channel: str = "serverchan", body: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO notification_logs(
                       report_date,channel,sent_at,success,title,item_count,response_message,body
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    report_date, channel, datetime.now().isoformat(timespec="seconds"),
                    int(success), title, item_count, response_message, body,
                ),
            )

    def queue_collection_alert(
        self, source_run_id: str, project_id: str, final_status: str, title: str, body: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO collection_alerts(
                       source_run_id,project_id,created_at,final_status,title,body,delivery_status
                   ) VALUES(?,?,?,?,?,?,'pending')
                   ON CONFLICT(source_run_id) DO UPDATE SET
                       final_status=excluded.final_status,title=excluded.title,body=excluded.body,
                       delivery_status=CASE WHEN collection_alerts.delivery_status='sent' THEN 'sent' ELSE 'pending' END""",
                (source_run_id, project_id, datetime.now().isoformat(timespec="seconds"), final_status, title, body),
            )

    def pending_collection_alerts(self) -> list[dict[str, Any]]:
        return self.fetchall(
            "SELECT * FROM collection_alerts WHERE delivery_status='pending' ORDER BY created_at,id"
        )

    def mark_collection_alert_sent(self, alert_id: int) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.execute(
                """UPDATE collection_alerts SET delivery_status='sent',attempt_count=attempt_count+1,
                   last_attempt_at=?,last_error=NULL,sent_at=? WHERE id=?""",
                (now, now, alert_id),
            )

    def mark_collection_alert_failed(self, alert_id: int, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE collection_alerts SET attempt_count=attempt_count+1,last_attempt_at=?,last_error=?
                   WHERE id=?""",
                (datetime.now().isoformat(timespec="seconds"), error[:1000], alert_id),
            )

    def rename_project(self, old_project_id: str, new_project_id: str) -> dict[str, int]:
        if not old_project_id or not new_project_id:
            raise ValueError("项目 ID 不能为空")
        counts: dict[str, int] = {}
        with self.connect() as connection:
            for table in PROJECT_TABLES:
                cursor = connection.execute(
                    f"UPDATE {table} SET project_id=? WHERE project_id=?",
                    (new_project_id, old_project_id),
                )
                counts[table] = cursor.rowcount
        return counts
