import os
from datetime import datetime

from app.retention import cleanup_database_history, cleanup_files, cleanup_logs
from app.storage.database import Database


def _insert_run_history(db: Database, run_id: str, stamp: str, suffix: str) -> None:
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO collection_runs(run_id,project_id,task_type,started_at,finished_at,status,total_tasks) VALUES(?,?,?,?,?,'success',1)",
            (run_id, "project", "all", stamp, stamp),
        )
        connection.execute(
            "INSERT INTO product_snapshots(run_id,project_id,asin,collected_at,success,confidence) VALUES(?,?,?,?,1,'high')",
            (run_id, "project", f"B{suffix:0>9}", stamp),
        )
        connection.execute(
            "INSERT INTO search_snapshots(run_id,project_id,keyword,page,asin,is_sponsored,collected_at) VALUES(?,?,?,1,?,0,?)",
            (run_id, "project", f"keyword-{suffix}", f"B{suffix:0>9}", stamp),
        )
        connection.execute(
            "INSERT INTO bestseller_snapshots(run_id,project_id,category_name,snapshot_date,rank,asin,collected_at) VALUES(?,?,?,?,1,?,?)",
            (run_id, "project", f"category-{suffix}", stamp[:10], f"B{suffix:0>9}", stamp),
        )
        connection.execute(
            "INSERT INTO change_events(run_id,project_id,event_type,severity,source_type,event_time,message) VALUES(?,?,'field_changed','low','product',?,'test')",
            (run_id, "project", stamp),
        )
        connection.execute(
            "INSERT INTO collection_errors(run_id,project_id,task_type,target,error_type,created_at) VALUES(?,?,'product',?,'Error',?)",
            (run_id, "project", f"B{suffix:0>9}", stamp),
        )
        connection.execute(
            "INSERT INTO collection_task_outcomes(run_id,project_id,task_type,target,success,finished_at) VALUES(?,?,'product',?,0,?)",
            (run_id, "project", f"B{suffix:0>9}", stamp),
        )
        connection.execute(
            "INSERT INTO bsr_new_candidates(project_id,asin,first_seen_at,last_seen_at,relevance_status,classification_source) VALUES(?,?,?,?, 'pending','auto')",
            ("project", f"B{suffix:0>9}", stamp, stamp),
        )
        connection.execute(
            "INSERT INTO notification_logs(report_date,channel,sent_at,success,title) VALUES(?,'serverchan',?,1,'test')",
            (stamp[:10], stamp),
        )
        connection.execute(
            """INSERT INTO collection_alerts(
                   source_run_id,project_id,created_at,final_status,title,body
               ) VALUES(?,?,?,'failed','test','test')""",
            (run_id, "project", stamp),
        )


def test_database_history_keeps_only_last_30_days(tmp_path):
    db = Database(tmp_path / "test.db")
    _insert_run_history(db, "old-run", "2026-07-01T12:00:00", "1")
    _insert_run_history(db, "recent-run", "2026-08-20T12:00:00", "2")

    deleted = cleanup_database_history(db, 30, datetime(2026, 9, 2, 12))

    for table in (
        "collection_runs", "product_snapshots", "search_snapshots", "bestseller_snapshots",
        "change_events", "collection_errors", "collection_task_outcomes", "bsr_new_candidates",
        "notification_logs", "collection_alerts",
    ):
        rows = db.fetchall(f"SELECT COUNT(*) count FROM {table}")
        assert rows[0]["count"] == 1
        assert deleted[table] == 1


def test_recent_retry_preserves_old_source_metadata(tmp_path):
    db = Database(tmp_path / "test.db")
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO collection_runs(run_id,project_id,task_type,started_at,finished_at,status,total_tasks) VALUES('old-source','project','all','2026-07-01T12:00:00','2026-07-01T12:05:00','failed',1)"
        )
        connection.execute(
            "INSERT INTO collection_task_outcomes(run_id,project_id,task_type,target,success,finished_at) VALUES('old-source','project','product','B000000001',0,'2026-07-01T12:05:00')"
        )
        connection.execute(
            "INSERT INTO collection_runs(run_id,project_id,task_type,started_at,finished_at,status,total_tasks,source_run_id) VALUES('recent-retry','project','retry','2026-08-20T12:00:00','2026-08-20T12:05:00','failed',1,'old-source')"
        )

    cleanup_database_history(db, 30, datetime(2026, 9, 2, 12), vacuum=False)
    assert {row["run_id"] for row in db.fetchall("SELECT run_id FROM collection_runs")} == {"old-source", "recent-retry"}
    assert db.fetchall("SELECT target FROM collection_task_outcomes") == [{"target": "B000000001"}]


def test_file_cleanup_uses_separate_retention_window(tmp_path):
    old_file = tmp_path / "old.log"
    recent_file = tmp_path / "recent.log"
    hidden_file = tmp_path / ".keep"
    for path in (old_file, recent_file, hidden_file):
        path.write_text("test", encoding="utf-8")
    os.utime(old_file, (1_751_328_000, 1_751_328_000))  # 2025-07-01 UTC
    os.utime(hidden_file, (1_751_328_000, 1_751_328_000))

    assert cleanup_files(tmp_path, 30, datetime(2026, 9, 2, 12)) == 1
    assert not old_file.exists()
    assert recent_file.exists()
    assert hidden_file.exists()


def test_log_cleanup_truncates_reusable_log_and_deletes_dated_log(tmp_path):
    fixed_log = tmp_path / "scheduler.stdout.log"
    dated_log = tmp_path / "monitor_2026-01-01.log"
    for path in (fixed_log, dated_log):
        path.write_text("old content", encoding="utf-8")
        os.utime(path, (1_751_328_000, 1_751_328_000))

    assert cleanup_logs(tmp_path, 90, datetime(2026, 9, 2, 12)) == 2
    assert fixed_log.exists() and fixed_log.read_text(encoding="utf-8") == ""
    assert not dated_log.exists()
