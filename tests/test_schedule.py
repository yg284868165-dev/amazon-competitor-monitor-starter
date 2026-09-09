from pathlib import Path
import asyncio
from datetime import datetime
import json
import tempfile
from unittest.mock import AsyncMock

import schedule
import wake_helper
from run import auto_retry_failed_runs, failed_targets
import run
from schedule import plist_data
from app.config import Category, Competitor, Keyword, Project
from app.storage.database import Database, PROJECT_TABLES


def test_scheduled_collection_uses_deduplicating_runner_and_checks_at_login():
    value = plist_data({"calendar": [{"Hour": 8, "Minute": 0}], "task": "all", "project": "all"})
    assert value["ProcessType"] == "Interactive"
    assert value["ProgramArguments"][0].endswith("/.venv/bin/python")
    assert value["ProgramArguments"][1].endswith("/scheduled_runner.py")
    assert value["RunAtLoad"] is True


def test_wake_daemon_checks_agent_before_and_after_each_slot():
    value = schedule._wake_plist_data("/python", {
        "times": ["00:00", "08:00"], "wake_lead_minutes": 2,
    })
    calendar = {(item["Hour"], item["Minute"]) for item in value["StartCalendarInterval"]}
    assert {(0, 10), (23, 59), (0, 5), (7, 59), (8, 5)} <= calendar
    assert value["RunAtLoad"] is True


def test_wake_helper_bootstraps_missing_agent_then_kickstarts_in_recovery_window(monkeypatch, tmp_path):
    config_path = tmp_path / "wake.json"
    plist_path = tmp_path / "agent.plist"
    plist_path.write_text("plist", encoding="utf-8")
    config_path.write_text(json.dumps({
        "times": ["14:00"], "user_id": 501,
        "agent_label": "test.agent", "agent_plist": str(plist_path),
        "recovery_delay_minutes": 5,
    }), encoding="utf-8")
    monkeypatch.setattr(wake_helper, "CONFIG_PATH", config_path)
    monkeypatch.setattr(wake_helper, "_console_user_id", lambda: 501)
    calls = []

    class Result:
        def __init__(self, returncode=0):
            self.returncode = returncode
            self.stdout = ""

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:2] == [wake_helper.LAUNCHCTL, "print"]:
            return Result(1)
        return Result()

    monkeypatch.setattr(wake_helper.subprocess, "run", fake_run)
    assert wake_helper.ensure_collection_agent(datetime(2026, 9, 9, 14, 5)) is True
    assert [wake_helper.LAUNCHCTL, "bootstrap", "gui/501", str(plist_path)] in calls
    assert [wake_helper.LAUNCHCTL, "kickstart", "gui/501/test.agent"] in calls


def test_schedule_status_reports_outdated_privileged_guard(monkeypatch, capsys, tmp_path):
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / "wake_helper.py").write_text("old", encoding="utf-8")
    monkeypatch.setattr(schedule, "WAKE_BASE_DIR", installed)
    monkeypatch.setattr(schedule, "load_schedule", lambda: {
        "enabled": True, "times": ["14:00"], "task": "all", "project": "all",
        "wake_enabled": True, "wake_lead_minutes": 2,
    })

    class Result:
        returncode = 0

    monkeypatch.setattr(schedule.subprocess, "run", lambda *args, **kwargs: Result())
    schedule.status()
    assert "重启自愈守护: 待更新" in capsys.readouterr().out


def test_collection_prevents_idle_sleep_after_python_starts(monkeypatch):
    captured = {}

    class Process:
        def poll(self):
            return None

        def terminate(self):
            captured["terminated"] = True

    def popen(command, **kwargs):
        captured["command"] = command
        return Process()

    monkeypatch.setattr(run.subprocess, "Popen", popen)
    with run.prevent_idle_sleep():
        pass
    assert captured["command"][:3] == ["/usr/bin/caffeinate", "-i", "-w"]
    assert captured["terminated"] is True


def test_wake_installer_stages_privileged_scripts_outside_desktop(monkeypatch):
    captured = {}

    def fake_administrator(command):
        captured["command"] = command
        for value in (command[1], command[3], command[4], command[5]):
            assert Path(value).exists()

    monkeypatch.setattr(schedule, "_administrator", fake_administrator)
    schedule.install_wake({"times": ["08:00", "14:00"], "wake_lead_minutes": 5})
    assert captured["command"][1].startswith("/tmp/amazon-wake-")
    assert captured["command"][3].startswith("/tmp/amazon-wake-")


def test_database_project_rename_updates_all_history_tables():
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.db")
        run_id = db.start_run("old_id", "product", 0)
        db.insert("change_events", {
            "run_id": run_id, "project_id": "old_id", "event_type": "field_changed",
            "severity": "low", "source_type": "product", "event_time": "2026-08-31T10:00:00",
            "message": "test",
        })
        db.rename_project("old_id", "new_id")
        for table in PROJECT_TABLES:
            assert db.fetchall(f"SELECT COUNT(*) AS count FROM {table} WHERE project_id='old_id'")[0]["count"] == 0
        assert db.fetchall("SELECT project_id FROM collection_runs")[0]["project_id"] == "new_id"
        assert db.fetchall("SELECT project_id FROM change_events")[0]["project_id"] == "new_id"


def test_failed_targets_use_final_per_target_outcomes():
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.db")
        run_id = db.start_run("project", "all", 3)
        db.record_outcome(run_id, "project", "product", "B000000001", True)
        db.record_outcome(run_id, "project", "product", "B000000002", False)
        db.record_outcome(run_id, "project", "search", "cleaning stone", False)
        assert failed_targets(db, run_id, 2) == {
            "product": {"B000000002"}, "search": {"cleaning stone"}, "bestseller": set(),
        }


def test_failed_targets_rebuild_legacy_batch_that_failed_before_first_target(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.db")
        run_id = db.start_run("project", "all", 3)
        db.finish_run(run_id, 0, 3)
        monkeypatch.setattr(run, "load_competitors", lambda project_id: [
            Competitor(project_id, "B000000001"),
        ])
        monkeypatch.setattr(run, "load_keywords", lambda pages, project_id: [
            Keyword(project_id, "cleaning stone", 3),
        ])
        monkeypatch.setattr(run, "load_categories", lambda project_id: [
            Category(project_id, "Cleaning Tools", "https://www.amazon.com/zgbs/test", 100),
        ])

        assert failed_targets(db, run_id, 2) == {
            "product": {"B000000001"},
            "search": {"cleaning stone"},
            "bestseller": {"Cleaning Tools"},
        }


def test_project_records_full_target_manifest_before_browser_initialization(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.db")

        class OfflineBrowser:
            def __init__(self, settings):
                pass

            async def __aenter__(self):
                raise RuntimeError("net::ERR_INTERNET_DISCONNECTED")

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        monkeypatch.setattr(run, "Database", lambda: db)
        monkeypatch.setattr(run, "BrowserManager", OfflineBrowser)
        monkeypatch.setattr(run, "load_competitors", lambda project_id: [
            Competitor(project_id, "B000000001"),
        ])
        monkeypatch.setattr(run, "load_keywords", lambda pages, project_id: [
            Keyword(project_id, "cleaning stone", 3),
        ])
        monkeypatch.setattr(run, "load_categories", lambda project_id: [
            Category(project_id, "Cleaning Tools", "https://www.amazon.com/zgbs/test", 100),
        ])

        exit_code, run_id = asyncio.run(run.execute_project(
            "all", Project("project", "Project", "90001"),
            {"amazon": {}, "search": {"default_pages": 3}},
        ))

        assert exit_code == 1
        assert db.fetchall(
            "SELECT task_type,target,success FROM collection_task_outcomes "
            "WHERE run_id=? ORDER BY task_type,target",
            (run_id,),
        ) == [
            {"task_type": "bestseller", "target": "Cleaning Tools", "success": 0},
            {"task_type": "product", "target": "B000000001", "success": 0},
            {"task_type": "search", "target": "cleaning stone", "success": 0},
        ]
        assert failed_targets(db, run_id, 2) == {
            "product": {"B000000001"},
            "search": {"cleaning stone"},
            "bestseller": {"Cleaning Tools"},
        }


def test_failed_retry_inherits_unfinished_targets_from_source_run():
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.db")
        source_id = db.start_run("project", "all", 2)
        db.record_outcome(source_id, "project", "bestseller", "Category A", False)
        db.record_outcome(source_id, "project", "bestseller", "Category B", False)
        db.finish_run(source_id, 0, 2)

        retry_id = db.start_run("project", "retry", 2, source_run_id=source_id)
        db.finish_run(retry_id, 0, 2)
        assert failed_targets(db, retry_id, 2)["bestseller"] == {"Category A", "Category B"}

        db.record_outcome(retry_id, "project", "bestseller", "Category A", True)
        assert failed_targets(db, retry_id, 2)["bestseller"] == {"Category B"}


def test_auto_retry_waits_then_runs_failed_targets_once(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.db")
        source_id = db.start_run("project", "all", 1)
        db.record_outcome(source_id, "project", "bestseller", "Category", False)
        db.finish_run(source_id, 0, 1)
        calls = []

        async def fake_retry(run_id, task_type="retry"):
            calls.append((run_id, task_type))
            return 0

        sleep = AsyncMock()
        monkeypatch.setattr(run, "Database", lambda: db)
        monkeypatch.setattr(run, "retry_run", fake_retry)
        monkeypatch.setattr(run.asyncio, "sleep", sleep)
        settings = {"amazon": {"retries": 2}, "auto_retry": {"enabled": True, "delay_seconds": 300}}

        result = asyncio.run(auto_retry_failed_runs([source_id], settings))

        sleep.assert_awaited_once_with(300)
        assert calls == [(source_id, "auto_retry")]
        assert result == [0]


def test_auto_retry_runs_manifest_targets_after_initialization_failure(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.db")
        source_id = db.start_run("project", "all", 2)
        db.record_targets(source_id, "project", {
            "product": {"B000000001"}, "search": {"cleaning stone"}, "bestseller": set(),
        })
        db.finish_run(source_id, 0, 2)
        calls = []

        async def fake_retry(run_id, task_type="retry"):
            calls.append((run_id, task_type))
            return 0

        monkeypatch.setattr(run, "Database", lambda: db)
        monkeypatch.setattr(run, "retry_run", fake_retry)
        monkeypatch.setattr(run.asyncio, "sleep", AsyncMock())
        settings = {"amazon": {"retries": 2}, "auto_retry": {"enabled": True, "delay_seconds": 300}}

        assert asyncio.run(auto_retry_failed_runs([source_id], settings)) == [0]
        assert calls == [(source_id, "auto_retry")]


def test_auto_retry_skips_source_already_resolved_manually(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.db")
        source_id = db.start_run("project", "all", 1)
        db.record_outcome(source_id, "project", "product", "B000000001", False)
        db.finish_run(source_id, 0, 1)
        retry_id = db.start_run("project", "retry", 1, source_run_id=source_id)
        db.record_outcome(retry_id, "project", "product", "B000000001", True)
        db.finish_run(retry_id, 1, 0)
        retry = AsyncMock()

        monkeypatch.setattr(run, "Database", lambda: db)
        monkeypatch.setattr(run, "retry_run", retry)
        monkeypatch.setattr(run.asyncio, "sleep", AsyncMock())
        settings = {"amazon": {"retries": 2}, "auto_retry": {"enabled": True, "delay_seconds": 0}}

        assert asyncio.run(auto_retry_failed_runs([source_id], settings)) == [0]
        retry.assert_not_awaited()
