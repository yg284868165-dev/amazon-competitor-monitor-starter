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


def _patch_project(monkeypatch, competitors: list[Competitor] | None = None):
    monkeypatch.setattr(scheduling, "load_projects", lambda: [Project("project", "测试项目", "90001")])
    monkeypatch.setattr(
        scheduling, "load_competitors",
        lambda project_id: competitors if competitors is not None else [Competitor(project_id, "B000000001")],
    )
    monkeypatch.setattr(scheduling, "load_keywords", lambda *_: [])
    monkeypatch.setattr(scheduling, "load_categories", lambda *_: [])
    monkeypatch.setattr(scheduling, "load_settings", lambda: {"search": {"default_pages": 3}})
    monkeypatch.setattr(scheduling, "queue_scheduled_collection_alerts", lambda db, run_ids: len(run_ids))
    monkeypatch.setattr(scheduling, "flush_pending_collection_alerts", lambda db: {"sent": 0, "pending": 0})


def _claim(db: Database, slot: datetime, claimed_at: datetime | None = None):
    assert scheduling.claim_slot(db, slot, _config(), claimed_at or slot) is True


def test_real_completed_batch_reconciles_without_synthetic_missed_run(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    _patch_project(monkeypatch)
    slot = datetime(2026, 9, 9, 14, 0)
    root = db.start_run("project", "all", 1, started_at="2026-09-09T14:02:05")
    db.record_targets(root, "project", {"product": {"B000000001"}, "search": set(), "bestseller": set()})
    db.record_outcome(root, "project", "product", "B000000001", True)
    db.finish_run(root, 1, 0)
    _claim(db, slot, datetime(2026, 9, 9, 14, 2))

    reconciled = scheduling.reconcile_scheduled_slots(db, [slot], _config(), datetime(2026, 9, 9, 15, 0))

    assert reconciled[0]["matched"] is True
    assert db.fetchall(
        "SELECT status,exit_code,delay_seconds FROM scheduled_executions WHERE slot_time=?", ("2026-09-09T14:00:00",),
    ) == [{"status": "success", "exit_code": 0, "delay_seconds": 120}]
    assert db.fetchall(
        "SELECT run_id FROM collection_runs WHERE started_at=? AND run_id<>?",
        ("2026-09-09T14:00:00", root),
    ) == []
    # Direct missed-slot callers receive the same protection.
    assert scheduling.record_missed_slot(db, slot, _config(), datetime(2026, 9, 9, 15, 1)) == []


def test_manual_root_without_execution_record_does_not_hide_missed_slot(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    _patch_project(monkeypatch)
    slot = datetime(2026, 9, 9, 14, 0)
    manual = db.start_run("project", "all", 1, started_at="2026-09-09T14:05:00")
    db.record_targets(manual, "project", {"product": {"B000000001"}, "search": set(), "bestseller": set()})
    db.record_outcome(manual, "project", "product", "B000000001", True)
    db.finish_run(manual, 1, 0)

    missed = scheduling.record_missed_slot(db, slot, _config(), datetime(2026, 9, 9, 15, 0))

    assert len(missed) == 1
    assert missed[0] != manual
    assert db.fetchall(
        "SELECT status FROM scheduled_executions WHERE slot_time=?", ("2026-09-09T14:00:00",),
    ) == [{"status": "missed"}]


def test_orphaned_running_batch_becomes_retryable_partial_run(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    _patch_project(monkeypatch)
    slot = datetime(2026, 9, 9, 14, 0)
    root = db.start_run("project", "all", 2, started_at="2026-09-09T14:00:05")
    db.record_targets(root, "project", {
        "product": {"B000000001", "B000000002"}, "search": set(), "bestseller": set(),
    })
    db.record_outcome(root, "project", "product", "B000000001", True)
    _claim(db, slot)

    reconciled = scheduling.reconcile_scheduled_slots(db, [slot], _config(), datetime(2026, 9, 9, 15, 0))

    assert reconciled[0]["recovered_runs"] == [{
        "run_id": root, "project_id": "project", "status": "partial", "success_count": 1, "failed_count": 1,
    }]
    assert db.fetchall(
        "SELECT status,success_count,failed_count,finished_at FROM collection_runs WHERE run_id=?", (root,),
    ) == [{
        "status": "partial", "success_count": 1, "failed_count": 1, "finished_at": "2026-09-09T15:00:00",
    }]
    assert db.fetchall(
        "SELECT task_type,target,error_type FROM collection_errors WHERE run_id=?", (root,),
    ) == [{"task_type": "product", "target": "B000000002", "error_type": "process_interrupted"}]
    assert db.fetchall(
        "SELECT status,exit_code FROM scheduled_executions WHERE slot_time=?", ("2026-09-09T14:00:00",),
    ) == [{"status": "failed", "exit_code": 2}]

    # Terminal slots are skipped on repeated startup; no duplicate diagnostics.
    assert scheduling.reconcile_scheduled_slots(db, [slot], _config(), datetime(2026, 9, 9, 15, 1)) == []
    assert db.fetchall("SELECT COUNT(*) AS count FROM collection_errors WHERE run_id=?", (root,)) == [{"count": 1}]


def test_reconciliation_cancels_only_pending_false_missed_alert(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    _patch_project(monkeypatch)
    slot = datetime(2026, 9, 9, 14, 0)
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
    _claim(db, slot)

    reconciled = scheduling.reconcile_scheduled_slots(db, [slot], _config(), datetime(2026, 9, 9, 15, 0))

    assert reconciled[0]["cancelled_alerts"] == 1
    assert db.fetchall("SELECT delivery_status FROM collection_alerts WHERE source_run_id=?", (fake,)) == [
        {"delivery_status": "cancelled"},
    ]
    assert db.fetchall("SELECT run_id,status FROM collection_runs WHERE run_id=?", (fake,)) == [
        {"run_id": fake, "status": "superseded"},
    ]


def test_sent_false_missed_alert_is_preserved_without_second_real_alert(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    _patch_project(monkeypatch)
    queued: list[list[str]] = []
    monkeypatch.setattr(
        scheduling, "queue_scheduled_collection_alerts", lambda db, run_ids: queued.append(run_ids) or len(run_ids),
    )
    slot = datetime(2026, 9, 9, 14, 0)
    fake = db.start_run("project", "all", 1, started_at="2026-09-09T14:00:00")
    db.record_targets(fake, "project", {"product": {"B000000001"}, "search": set(), "bestseller": set()})
    db.insert("collection_errors", {
        "run_id": fake, "project_id": "project", "task_type": "product", "target": "B000000001",
        "error_type": "schedule_missed", "error_message": "already sent", "created_at": "2026-09-09T15:00:00",
    })
    db.finish_run(fake, 0, 1)
    db.queue_collection_alert(fake, "project", "failed", "old", "old")
    with db.connect() as connection:
        connection.execute("UPDATE collection_alerts SET delivery_status='sent' WHERE source_run_id=?", (fake,))
    root = db.start_run("project", "all", 1, started_at="2026-09-09T14:00:05")
    db.record_targets(root, "project", {"product": {"B000000001"}, "search": set(), "bestseller": set()})
    _claim(db, slot)

    scheduling.reconcile_scheduled_slots(db, [slot], _config(), datetime(2026, 9, 9, 15, 0))

    assert queued == []
    assert db.fetchall("SELECT delivery_status FROM collection_alerts WHERE source_run_id=?", (fake,)) == [
        {"delivery_status": "sent"},
    ]
    assert db.fetchall("SELECT status FROM collection_runs WHERE run_id=?", (fake,)) == [
        {"status": "superseded"},
    ]
