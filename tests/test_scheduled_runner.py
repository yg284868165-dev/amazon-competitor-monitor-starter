from datetime import datetime

import app.scheduling as scheduling
import scheduled_runner
from app.config import Project
from app.storage.database import Database


def _config():
    return {"enabled": True, "times": ["08:00", "14:00"], "task": "all", "project": "all"}


def test_startup_reconciles_and_cancels_false_alert_before_flush(monkeypatch, tmp_path):
    """A reboot recovery must not send the old synthetic failure on startup."""
    db = Database(tmp_path / "test.db")
    slot = datetime(2026, 9, 9, 14, 0)
    monkeypatch.setattr(scheduling, "load_projects", lambda: [Project("project", "测试项目", "90001")])
    monkeypatch.setattr(scheduling, "queue_scheduled_collection_alerts", lambda *_: 0)
    monkeypatch.setattr(scheduling, "flush_pending_collection_alerts", lambda *_: {"sent": 0, "pending": 0})
    fake = db.start_run("project", "all", 1, started_at="2026-09-09T14:00:00")
    db.record_targets(fake, "project", {"product": {"B000000001"}, "search": set(), "bestseller": set()})
    db.insert("collection_errors", {
        "run_id": fake, "project_id": "project", "task_type": "product", "target": "B000000001",
        "error_type": "schedule_missed", "error_message": "old false failure", "created_at": "2026-09-09T15:00:00",
    })
    db.finish_run(fake, 0, 1)
    db.queue_collection_alert(fake, "project", "failed", "old", "old")
    root = db.start_run("project", "all", 1, started_at="2026-09-09T14:00:05")
    db.record_targets(root, "project", {"product": {"B000000001"}, "search": set(), "bestseller": set()})
    db.record_outcome(root, "project", "product", "B000000001", True)
    db.finish_run(root, 1, 0)
    assert scheduling.claim_slot(db, slot, _config(), slot)

    flushed_statuses: list[str] = []
    monkeypatch.setattr(scheduled_runner, "ensure_directories", lambda: None)
    monkeypatch.setattr(scheduled_runner, "load_config", _config)
    monkeypatch.setattr(scheduled_runner, "Database", lambda: db)
    monkeypatch.setattr(scheduled_runner, "due_slots", lambda *_: [slot])
    monkeypatch.setattr(
        scheduled_runner, "flush_pending_collection_alerts",
        lambda database: flushed_statuses.extend(
            row["delivery_status"] for row in database.fetchall("SELECT delivery_status FROM collection_alerts")
        ) or {"sent": 0, "pending": 0},
    )

    assert scheduled_runner.main(datetime(2026, 9, 9, 15, 0)) == 0
    assert flushed_statuses == ["cancelled"]
    assert db.pending_collection_alerts() == []
