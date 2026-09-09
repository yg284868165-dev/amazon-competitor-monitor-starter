from datetime import datetime

import app.scheduling as scheduling
from app.config import Competitor, Project
from app.storage.database import Database


def _config():
    return {"enabled": True, "times": ["08:00", "14:00"], "task": "all", "project": "all"}


def test_schedule_slots_are_claimed_only_once(tmp_path):
    db = Database(tmp_path / "test.db")
    scheduling.activate_schedule(db, _config(), datetime(2026, 9, 9, 7, 0))
    now = datetime(2026, 9, 9, 14, 5)
    assert scheduling.due_slots(db, _config(), now) == [
        datetime(2026, 9, 9, 8, 0), datetime(2026, 9, 9, 14, 0),
    ]
    assert scheduling.claim_slot(db, datetime(2026, 9, 9, 14, 0), _config(), now) is True
    # A second wrapper invocation may reclaim a slot left in running state after an abrupt exit.
    assert scheduling.claim_slot(db, datetime(2026, 9, 9, 14, 0), _config(), now) is True
    scheduling.finish_slot(db, datetime(2026, 9, 9, 14, 0), 0, now)
    assert scheduling.claim_slot(db, datetime(2026, 9, 9, 14, 0), _config(), now) is False


def test_missed_slot_creates_retryable_failed_run(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    monkeypatch.setattr(scheduling, "load_projects", lambda: [Project("project", "测试项目", "90001")])
    monkeypatch.setattr(scheduling, "load_competitors", lambda project_id: [Competitor(project_id, "B000000001")])
    monkeypatch.setattr(scheduling, "load_keywords", lambda *_: [])
    monkeypatch.setattr(scheduling, "load_categories", lambda *_: [])
    monkeypatch.setattr(scheduling, "load_settings", lambda: {"search": {"default_pages": 3}})
    monkeypatch.setattr(scheduling, "queue_scheduled_collection_alerts", lambda db, run_ids: len(run_ids))
    monkeypatch.setattr(scheduling, "flush_pending_collection_alerts", lambda db: {"sent": 0, "pending": 0})

    run_ids = scheduling.record_missed_slot(
        db, datetime(2026, 9, 9, 14, 0), _config(), datetime(2026, 9, 9, 15, 0),
    )
    assert len(run_ids) == 1
    assert db.fetchall(
        "SELECT started_at,status,failed_count FROM collection_runs WHERE run_id=?", (run_ids[0],),
    ) == [{"started_at": "2026-09-09T14:00:00", "status": "failed", "failed_count": 1}]
    assert db.fetchall(
        "SELECT target,success FROM collection_task_outcomes WHERE run_id=?", (run_ids[0],),
    ) == [{"target": "B000000001", "success": 0}]
    assert scheduling.record_missed_slot(
        db, datetime(2026, 9, 9, 14, 0), _config(), datetime(2026, 9, 9, 15, 1),
    ) == []
