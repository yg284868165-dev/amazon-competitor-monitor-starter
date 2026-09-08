from datetime import date

import app.notifications.serverchan as serverchan
import notify_schedule
import pytest
from app.config import Project
from app.notifications.serverchan import (
    _collapse_daily_events, _daily_change_items, _weekly_rating_changes,
    _daily_run_statistics, _weekly_recap_items,
    validate_notification_config,
)
from app.presentation import format_change_summary
from app.storage.database import Database


def test_notification_config_validation():
    assert validate_notification_config({
        "enabled": True, "time": "09:30", "send_when_no_changes": False, "max_items": 20,
    }) == {"enabled": True, "time": "09:30", "send_when_no_changes": False, "max_items": 20}


def test_notification_launch_agent_runs_daily_summary():
    value = notify_schedule.plist_data({"time": "09:00"})
    assert value["StartCalendarInterval"] == {"Hour": 9, "Minute": 0}
    assert value["ProgramArguments"][-2].endswith("/notify.py")
    assert value["ProgramArguments"][-1] == "daily"
    assert value["ProgramArguments"][0].endswith("/.venv/bin/python")


def test_notification_launch_agent_runs_separate_weekly_summary_monday_at_ten():
    value = notify_schedule.plist_data({"time": "09:00"}, "weekly")
    assert value["StartCalendarInterval"] == {"Weekday": 1, "Hour": 10, "Minute": 0}
    assert value["ProgramArguments"][-2].endswith("/notify.py")
    assert value["ProgramArguments"][-1] == "weekly"


def test_notification_history_stores_briefing_body(tmp_path):
    db = Database(tmp_path / "test.db")
    db.record_notification(
        "2026-09-01", False, "Amazon竞品昨日监控｜9月1日", 3,
        "网络不可达", body="## 原始日报\n\n- 变化内容",
    )

    row = db.fetchall("SELECT * FROM notification_logs")[0]
    assert row["success"] == 0
    assert row["body"] == "## 原始日报\n\n- 变化内容"


def test_failed_daily_send_keeps_exact_generated_body(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    monkeypatch.setattr(serverchan, "Database", lambda: db)
    monkeypatch.setattr(serverchan, "load_notification_config", lambda: {
        "enabled": True, "send_when_no_changes": True, "max_items": 15,
    })
    monkeypatch.setattr(
        serverchan, "build_daily_summary",
        lambda *_: ("测试日报", "## 测试日报\n\n- 精确正文", 1),
    )
    monkeypatch.setattr(
        serverchan, "send_serverchan",
        lambda *_: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    with pytest.raises(RuntimeError, match="offline"):
        serverchan.send_daily(date(2026, 9, 1), force=True)

    row = db.fetchall("SELECT success,response_message,body FROM notification_logs")[0]
    assert row == {
        "success": 0, "response_message": "offline",
        "body": "## 测试日报\n\n- 精确正文",
    }


def _insert_change(db, event_id, field_name, old_value, new_value, event_time, asin="B000000001"):
    db.insert("change_events", {
        "run_id": f"run-{event_id}", "project_id": "project", "event_type": "field_changed",
        "severity": "low", "source_type": "product", "asin": asin,
        "field_name": field_name,
        "old_value": None if old_value is None else str(old_value),
        "new_value": None if new_value is None else str(new_value),
        "event_time": event_time, "message": "test",
    })


def test_intraday_change_returning_to_original_value_is_removed():
    rows = [
        {"project_id": "project", "source_type": "product", "asin": "B000000001", "keyword": None,
         "category_name": None, "field_name": "variation_count", "old_value": "5", "new_value": "4",
         "event_time": "2026-09-01T14:00:00", "message": "test"},
        {"project_id": "project", "source_type": "product", "asin": "B000000001", "keyword": None,
         "category_name": None, "field_name": "variation_count", "old_value": "4", "new_value": "5.0",
         "event_time": "2026-09-01T20:00:00", "message": "test"},
    ]
    assert _collapse_daily_events(rows) == []


def test_main_image_and_image_set_changes_are_collapsed_to_main_image():
    base = {
        "project_id": "project", "source_type": "product", "asin": "B000000001",
        "keyword": None, "category_name": None, "old_value": "old", "new_value": "new",
        "event_time": "2026-09-01T08:00:00", "message": "test",
    }
    rows = [
        {**base, "field_name": "image_hash"},
        {**base, "field_name": "product_images_hash"},
    ]
    assert [row["field_name"] for row in _collapse_daily_events(rows)] == ["image_hash"]


def test_intraday_price_drop_is_kept_even_when_final_price_recovers():
    base = {
        "project_id": "project", "source_type": "product", "asin": "B000000001",
        "keyword": None, "category_name": None, "field_name": "current_price", "message": "test",
    }
    rows = [
        {**base, "old_value": "10", "new_value": "8", "event_time": "2026-09-01T14:00:00"},
        {**base, "old_value": "8", "new_value": "10", "event_time": "2026-09-01T20:00:00"},
    ]

    collapsed = _collapse_daily_events(rows)

    assert len(collapsed) == 1
    assert format_change_summary(collapsed[0]) == "日内短时降价：$10 → 最低$8 → $10，最低价首次出现在14:00"


def test_intraday_follow_seller_appearance_and_disappearance_is_kept():
    base = {
        "project_id": "project", "source_type": "product", "asin": "B000000001",
        "keyword": None, "category_name": None, "field_name": "offer_count", "message": "test",
    }
    rows = [
        {**base, "old_value": None, "new_value": "2", "event_time": "2026-09-01T08:00:00"},
        {**base, "old_value": "2", "new_value": "0", "event_time": "2026-09-01T20:00:00"},
    ]

    collapsed = _collapse_daily_events(rows)

    assert len(collapsed) == 1
    assert format_change_summary(collapsed[0]) == "跟卖动态：08:00发现2个跟卖，20:00确认消失"


def test_intraday_business_price_is_kept_after_it_disappears():
    base = {
        "project_id": "project", "source_type": "product", "asin": "B000000001",
        "keyword": None, "category_name": None, "field_name": "business_price_text",
        "message": "test",
    }
    rows = [
        {**base, "old_value": None, "new_value": "Business Price: $7.49", "event_time": "2026-09-01T08:00:00"},
        {**base, "old_value": "Business Price: $7.49", "new_value": None, "event_time": "2026-09-01T20:00:00"},
    ]

    collapsed = _collapse_daily_events(rows)

    assert len(collapsed) == 1
    assert "企业价日内变化" in format_change_summary(collapsed[0])


def test_intraday_dog_page_and_high_return_label_are_kept_after_recovery():
    base = {
        "project_id": "project", "source_type": "product", "asin": "B000000001",
        "keyword": None, "category_name": None, "message": "test",
    }
    rows = [
        {**base, "field_name": "listing_status", "old_value": "正常在售", "new_value": "页面变狗", "event_time": "2026-09-01T14:00:00"},
        {**base, "field_name": "listing_status", "old_value": "页面变狗", "new_value": "正常在售", "event_time": "2026-09-01T20:00:00"},
        {**base, "field_name": "high_return_rate", "old_value": "0", "new_value": "1", "event_time": "2026-09-01T14:00:00"},
        {**base, "field_name": "high_return_rate", "old_value": "1", "new_value": "0", "event_time": "2026-09-01T20:00:00"},
    ]

    collapsed = _collapse_daily_events(rows)

    assert [row["field_name"] for row in collapsed] == ["listing_status", "high_return_rate"]
    assert [format_change_summary(row) for row in collapsed] == [
        "日内曾变狗，随后恢复正常",
        "日内曾出现高退货率标签，随后消失",
    ]


def test_rating_count_is_excluded_from_ordinary_daily_changes(tmp_path):
    db = Database(tmp_path / "test.db")
    _insert_change(db, 1, "rating_count", 100, 103, "2026-09-01T08:00:00")
    _insert_change(db, 2, "current_price", 10, 9, "2026-09-01T08:00:00")

    items = _daily_change_items(db, date(2026, 9, 1))

    assert [row["field_name"] for row in items] == ["current_price"]


def test_rating_changes_are_summarized_for_monday_to_sunday_only(tmp_path):
    db = Database(tmp_path / "test.db")
    _insert_change(db, 1, "rating_count", 100, 103, "2026-08-31T08:00:00")
    _insert_change(db, 2, "rating_count", 103, 108, "2026-09-06T20:00:00")
    _insert_change(db, 3, "rating_count", 50, 49, "2026-09-01T08:00:00", "B000000002")
    _insert_change(db, 4, "rating_count", 49, 50, "2026-09-05T08:00:00", "B000000002")

    assert _weekly_rating_changes(db, date(2026, 9, 2)) == []
    assert _weekly_rating_changes(db, date(2026, 9, 6)) == [{
        "project_id": "project", "asin": "B000000001",
        "old_count": 100, "new_count": 108, "change_count": 8,
    }]


def test_weekly_recap_uses_daily_scope_and_marks_recovered_changes(tmp_path):
    db = Database(tmp_path / "test.db")
    _insert_change(db, 1, "current_price", 10, 8, "2026-08-31T08:00:00")
    _insert_change(db, 2, "current_price", 8, 10, "2026-09-02T20:00:00")
    _insert_change(db, 3, "image_hash", "old.jpg", "new.jpg", "2026-09-03T08:00:00")
    _insert_change(db, 4, "rating_count", 100, 105, "2026-09-04T08:00:00")
    _insert_change(db, 5, "current_price", 10, 9, "2026-09-07T08:00:00")

    items = _weekly_recap_items(db, date(2026, 9, 6))

    assert [(row["field_name"], row["report_date"], row["weekly_state"]) for row in items] == [
        ("current_price", "2026-08-31", "周内已恢复原状态"),
        ("current_price", "2026-09-02", "周内已恢复原状态"),
        ("image_hash", "2026-09-03", "截至周末仍保持变化"),
    ]


def test_weekly_recap_treats_missing_and_zero_follow_sellers_as_recovered(tmp_path):
    db = Database(tmp_path / "test.db")
    _insert_change(db, 1, "offer_count", None, 2, "2026-09-02T08:00:00")
    _insert_change(db, 2, "offer_count", 2, 0, "2026-09-04T20:00:00")

    items = _weekly_recap_items(db, date(2026, 9, 6))

    assert {row["weekly_state"] for row in items} == {"周内已恢复原状态"}


def test_daily_run_statistics_reports_later_full_rerun_as_recovered(tmp_path):
    db = Database(tmp_path / "test.db")
    with db.connect() as connection:
        connection.execute(
            """INSERT INTO collection_runs(
                   run_id,project_id,task_type,started_at,finished_at,status,total_tasks,failed_count
               ) VALUES('failed','project','all','2026-09-06T08:00:00','2026-09-06T08:01:00','failed',2,2)"""
        )
        connection.execute(
            """INSERT INTO collection_runs(
                   run_id,project_id,task_type,started_at,finished_at,status,total_tasks,success_count
               ) VALUES('rerun','project','all','2026-09-06T08:06:00','2026-09-06T08:16:00','success',2,2)"""
        )

    assert _daily_run_statistics(db, date(2026, 9, 6))["project"] == {
        "batches": 2, "completed": 1, "recovered": 1,
        "unresolved": 0, "running": 0, "failed_items": 0,
    }


def test_weekly_summary_contains_natural_week_rank_sections(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    monkeypatch.setattr(serverchan, "Database", lambda: db)
    monkeypatch.setattr(serverchan, "load_projects", lambda: [Project("project", "测试项目", "90001")])
    monkeypatch.setattr(serverchan, "build_product_identities", lambda *_: {"B000000001": "测试商品｜B000000001"})
    monkeypatch.setattr(serverchan, "_weekly_rating_changes", lambda *_: [])
    monkeypatch.setattr(serverchan, "analyze_all_weekly_trends", lambda *_: [{
        "project_id": "project", "metric": "main_bsr", "asin": "B000000001",
        "product": "测试商品｜B000000001", "category_name": "Health & Household",
        "keyword": "", "summaries": ["7日从20000名提升到16000名", "14日数据不足", "28日数据不足"],
        "presence_change": None, "observed_days": None,
    }])

    _, body, count = serverchan.build_weekly_summary(date(2026, 9, 6))

    assert count == 1
    assert "大类目BSR趋势" in body
    assert "Health & Household" in body
    assert "7日从20000名提升到16000名；14日数据不足；28日数据不足" in body


def test_weekly_summary_displays_every_recap_item_without_daily_limit(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    recap = [{
        "project_id": "project", "source_type": "product", "asin": f"B{index:09d}",
        "keyword": None, "category_name": None, "field_name": "variation_count",
        "event_type": "field_changed", "old_value": "1", "new_value": "2",
        "event_time": f"2026-09-01T08:{index:02d}:00", "report_date": "2026-09-01",
        "weekly_state": "截至周末仍保持变化", "message": "test",
    } for index in range(20)]
    monkeypatch.setattr(serverchan, "Database", lambda: db)
    monkeypatch.setattr(serverchan, "load_projects", lambda: [Project("project", "测试项目", "90001")])
    monkeypatch.setattr(serverchan, "build_product_identities", lambda *_: {
        row["asin"]: f"测试商品{index}｜{row['asin']}" for index, row in enumerate(recap)
    })
    monkeypatch.setattr(serverchan, "_weekly_recap_items", lambda *_: recap)
    monkeypatch.setattr(serverchan, "_weekly_rating_changes", lambda *_: [])
    monkeypatch.setattr(serverchan, "analyze_all_weekly_trends", lambda *_: [])

    _, body, count = serverchan.build_weekly_summary(date(2026, 9, 6))

    assert count == 20
    assert "测试商品0" in body
    assert "测试商品19" in body
    assert "未展开" not in body


def test_weekly_summary_uses_compact_ad_observation_instead_of_rank_trend(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    monkeypatch.setattr(serverchan, "Database", lambda: db)
    monkeypatch.setattr(serverchan, "load_projects", lambda: [Project("project", "测试项目", "90001")])
    monkeypatch.setattr(serverchan, "build_product_identities", lambda *_: {"B000000001": "测试商品｜B000000001"})
    monkeypatch.setattr(serverchan, "_weekly_recap_items", lambda *_: [])
    monkeypatch.setattr(serverchan, "_weekly_rating_changes", lambda *_: [])
    monkeypatch.setattr(serverchan, "analyze_all_weekly_trends", lambda *_: [])
    monkeypatch.setattr(serverchan, "build_weekly_ad_insights", lambda *_: [{
        "project_id": "project", "product": "测试商品｜B000000001", "asin": "B000000001",
        "keyword": "cleaning stone", "observation": "美西11时通常强于23时，4天中有3天重复",
        "inference": "疑似白天提高竞价", "confidence": "高",
    }])

    _, body, count = serverchan.build_weekly_summary(date(2026, 9, 6))

    assert count == 1
    assert "广告投放观察" in body
    assert "观察：美西11时通常强于23时" in body
    assert "推测：疑似白天提高竞价（可信度：高）" in body
    assert "广告位排名趋势" not in body
