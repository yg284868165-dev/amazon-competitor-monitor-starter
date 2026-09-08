import app.notifications.collection_alerts as alerts
from app.config import Project
from app.storage.database import Database


def _failed_root(db: Database) -> str:
    run_id = db.start_run("project", "all", 2)
    db.record_targets(run_id, "project", {
        "product": {"B000000001"}, "search": {"cleaning stone"}, "bestseller": set(),
    })
    db.finish_run(run_id, 0, 2)
    return run_id


def test_recovered_scheduled_failure_is_queued_and_sent_once(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    root = _failed_root(db)
    retry = db.start_run("project", "auto_retry", 2, source_run_id=root)
    db.record_outcome(retry, "project", "product", "B000000001", True)
    db.record_outcome(retry, "project", "search", "cleaning stone", True)
    db.finish_run(retry, 2, 0)
    monkeypatch.setattr(alerts, "load_projects", lambda: [Project("project", "测试项目", "90001")])
    monkeypatch.setattr(alerts, "load_notification_config", lambda: {"enabled": True})
    sent = []
    monkeypatch.setattr(alerts, "send_serverchan", lambda title, body: sent.append((title, body)) or "发送成功")

    assert alerts.queue_scheduled_collection_alerts(db, [root]) == 1
    assert alerts.flush_pending_collection_alerts(db) == {"sent": 1, "pending": 0}
    assert "异常已恢复" in sent[0][0]
    assert "无需人工操作" in sent[0][1]
    assert "无需人工操作" in db.fetchall("SELECT body FROM notification_logs")[0]["body"]
    assert alerts.flush_pending_collection_alerts(db) == {"sent": 0, "pending": 0}


def test_unresolved_failure_lists_targets_and_stays_pending_when_offline(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    root = _failed_root(db)
    monkeypatch.setattr(alerts, "load_projects", lambda: [Project("project", "测试项目", "90001")])
    monkeypatch.setattr(alerts, "load_notification_config", lambda: {"enabled": True})
    monkeypatch.setattr(alerts, "send_serverchan", lambda *_: (_ for _ in ()).throw(RuntimeError("offline")))

    alerts.queue_scheduled_collection_alerts(db, [root])
    assert alerts.flush_pending_collection_alerts(db) == {"sent": 0, "pending": 1}
    row = db.pending_collection_alerts()[0]
    assert "商品1项、关键词1项" in row["body"]
    assert row["attempt_count"] == 1
    assert row["last_error"] == "offline"
