from pathlib import Path
from io import BytesIO

import web
from web import app
from app.config import Project
from app.web_config import parse_asin_batch, parse_keyword_batch
from app.paths import report_filename
from app.storage.database import Database
import app.web_config as web_config


def test_dashboard_and_read_apis():
    client = app.test_client()
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    dashboard_text = dashboard.get_data(as_text=True)
    assert "Mac 需保持用户登录并使用“睡眠”" in dashboard_text
    assert "schedule.py permission-test" in dashboard_text
    assert "无需手动开启“网络唤醒”" in dashboard_text
    assert client.get("/api/health").get_json()["ok"] is True
    for kind in ("projects", "competitors", "keywords", "categories"):
        response = client.get(f"/api/config/{kind}")
        assert response.status_code == 200
        assert response.get_json()["ok"] is True
    assert client.get("/api/schedule").get_json()["ok"] is True
    assert client.get("/api/runs").get_json()["ok"] is True
    reports = client.get("/api/reports").get_json()
    assert reports["ok"] is True
    assert reports["daily"]["date"]
    assert reports["weekly"]["start_date"] and reports["weekly"]["end_date"]
    notification = client.get("/api/notification")
    assert notification.status_code == 200
    assert notification.get_json()["ok"] is True
    new_products = client.get("/api/new-products")
    assert new_products.status_code == 200
    assert new_products.get_json()["ok"] is True


def test_rejects_unknown_config():
    assert app.test_client().get("/api/config/unknown").status_code == 404


def test_parse_asin_batch_deduplicates_and_reports_invalid():
    valid, invalid = parse_asin_batch("b000000010, B000000011\nB000000010；bad")
    assert valid == ["B000000010", "B000000011"]
    assert invalid == ["BAD"]


def test_parse_keyword_batch_preserves_spaces_and_deduplicates():
    result = parse_keyword_batch("sample widget\nwidget cleaner；SAMPLE WIDGET, household cleaner")
    assert result == ["sample widget", "widget cleaner", "household cleaner"]


def test_project_id_rename_cascades_to_child_tables(monkeypatch):
    tables = {
        "projects": [{"enabled": 1, "project_id": "old_id", "project_name": "旧项目", "postal_code": "90001"}],
        "competitors": [{"enabled": 1, "project_id": "old_id", "asin": "B000000010", "internal_name": "", "brand": "", "remark": ""}],
        "keywords": [{"enabled": 1, "project_id": "old_id", "keyword": "sample widget", "pages": 3}],
        "categories": [{"enabled": 1, "project_id": "old_id", "category_name": "Test", "category_url": "https://www.amazon.com/zgbs/test", "max_rank": 100}],
    }
    written = {}
    monkeypatch.setattr(web_config, "read_table", lambda kind: [dict(row) for row in tables[kind]])
    monkeypatch.setattr(web_config, "_write_clean_table", lambda kind, rows: written.update({kind: rows}))
    renamed = []
    monkeypatch.setattr(web_config.Database, "rename_project", lambda self, old, new: renamed.append((old, new)))
    monkeypatch.setattr(web_config, "load_rules", lambda: {
        "old_id": {"include_keywords": ["craft brush"], "exclude_keywords": [], "max_age_days": 90}
    })
    saved_rules = []
    monkeypatch.setattr(web_config, "save_rules", lambda rules: saved_rules.append(rules))

    web_config.write_table("projects", [{
        "enabled": 1, "project_id": "new_id", "project_name": "旧项目", "postal_code": "90001",
        "_original_project_id": "old_id",
    }])

    assert written["projects"][0]["project_id"] == "new_id"
    for kind in ("competitors", "keywords", "categories"):
        assert written[kind][0]["project_id"] == "new_id"
    assert renamed == [("old_id", "new_id")]
    assert saved_rules == [{
        "new_id": {"include_keywords": ["craft brush"], "exclude_keywords": [], "max_age_days": 90}
    }]


def test_report_filename_includes_safe_project_name():
    assert report_filename("示例产品") == "示例产品_Amazon竞品监控.xlsx"
    assert report_filename("厨房/清洁") == "厨房_清洁_Amazon竞品监控.xlsx"


def test_summary_reports_can_be_downloaded_and_change_report_is_removed(monkeypatch):
    monkeypatch.setattr(web, "build_daily_excel_report", lambda *args, **kwargs: BytesIO(b"excel"))
    monkeypatch.setattr(web, "build_weekly_excel_report", lambda *args, **kwargs: BytesIO(b"weekly-excel"))
    monkeypatch.setattr(web, "load_projects", lambda: [Project("project", "测试项目", "90001")])
    client = app.test_client()

    daily = client.get("/reports/summary/daily")
    weekly = client.get("/reports/summary/weekly")
    assert daily.status_code == 200 and daily.data == b"excel"
    assert weekly.status_code == 200 and weekly.data == b"weekly-excel"
    assert "attachment" in daily.headers["Content-Disposition"]
    assert ".xlsx" in daily.headers["Content-Disposition"]
    assert daily.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert ".xlsx" in weekly.headers["Content-Disposition"]
    assert weekly.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert client.get("/reports/project/changes").status_code == 404


def test_candidate_date_endpoint_accepts_manual_date(monkeypatch):
    captured = {}

    def fake_set_date(db, project_id, asin, value):
        captured.update({"project_id": project_id, "asin": asin, "value": value})
        return {"project_id": project_id, "asin": asin, "date_first_available": value}

    monkeypatch.setattr(web, "set_candidate_date", fake_set_date)
    monkeypatch.setattr(web, "load_projects", lambda: [])
    response = app.test_client().put("/api/new-products/date", json={
        "project_id": "project", "asin": "b000000001",
        "date_first_available": "2026-08-12",
    })

    assert response.status_code == 200
    assert captured == {
        "project_id": "project", "asin": "B000000001", "value": "2026-08-12",
    }


def test_retry_endpoint_accepts_failed_retry_with_source_lineage(monkeypatch, tmp_path: Path):
    db = Database(tmp_path / "test.db")
    source_id = db.start_run("project", "all", 1)
    db.record_outcome(source_id, "project", "bestseller", "Test Category", False)
    db.finish_run(source_id, 0, 1)
    failed_retry_id = db.start_run("project", "retry", 1, source_run_id=source_id)
    db.finish_run(failed_retry_id, 0, 1)
    captured = {}

    class Process:
        pid = 12345

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return Process()

    monkeypatch.setattr(web, "Database", lambda: db)
    monkeypatch.setattr(web.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(web, "LOG_DIR", tmp_path)
    monkeypatch.setattr(web, "ensure_directories", lambda: None)

    response = app.test_client().post(f"/api/runs/{failed_retry_id}/retry")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert captured["command"][-2:] == ["--run-id", failed_retry_id]
