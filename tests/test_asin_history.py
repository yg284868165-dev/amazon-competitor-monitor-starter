from datetime import datetime

import pytest

import app.asin_history as history
from app.config import Competitor, Keyword
from app.storage.database import Database


def _product(db, run_id, collected_at, price, bsr, image="https://example.com/old.jpg"):
    db.insert("product_snapshots", {
        "run_id": run_id, "project_id": "project", "asin": "B000000001",
        "collected_at": collected_at, "title": "Test Product", "brand": "Brand",
        "current_price": price, "main_bsr": bsr,
        "category_ranks_json": '[{"category":"Home & Kitchen","rank":%d},{"category":"Cleaning Tools","rank":10}]' % bsr,
        "main_image_url": image, "success": 1, "confidence": "high",
    })


def _search(db, run_id, collected_at, organic=None, ad=None):
    db.insert("collection_task_outcomes", {
        "run_id": run_id, "project_id": "project", "task_type": "search",
        "target": "cleaning stone", "success": 1, "finished_at": collected_at,
    })
    if organic is not None:
        db.insert("search_snapshots", {
            "run_id": run_id, "project_id": "project", "keyword": "cleaning stone",
            "page": 1, "absolute_position": organic, "organic_rank": organic,
            "asin": "B000000001", "is_sponsored": 0, "collected_at": collected_at,
        })
    if ad is not None:
        db.insert("search_snapshots", {
            "run_id": run_id, "project_id": "project", "keyword": "cleaning stone",
            "page": 1, "absolute_position": ad, "ad_rank": ad,
            "asin": "B000000001", "is_sponsored": 1, "collected_at": collected_at,
        })


def test_history_groups_actions_and_compares_followup_ranks(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    monkeypatch.setattr(history, "load_competitors", lambda project_id, include_disabled=False: [
        Competitor("project", "B000000001", "内部名称", "Brand"),
    ])
    monkeypatch.setattr(history, "load_keywords", lambda project_id=None: [
        Keyword("project", "cleaning stone", 3),
    ])

    _product(db, "baseline", "2026-09-09T08:00:00", 12.99, 8420)
    _search(db, "baseline", "2026-09-09T08:05:00", organic=18, ad=4)
    _product(db, "action", "2026-09-10T08:00:00", 10.99, 8000, "https://example.com/new.jpg")
    db.insert("change_events", {
        "run_id": "action", "project_id": "project", "event_type": "field_changed",
        "severity": "medium", "source_type": "product", "asin": "B000000001",
        "field_name": "current_price", "old_value": "12.99", "new_value": "10.99",
        "event_time": "2026-09-10T08:01:00", "message": "price changed",
    })
    db.insert("change_events", {
        "run_id": "action", "project_id": "project", "event_type": "field_changed",
        "severity": "medium", "source_type": "product", "asin": "B000000001",
        "field_name": "image_hash", "old_value": "https://example.com/old.jpg",
        "new_value": "https://example.com/new.jpg",
        "event_time": "2026-09-10T08:01:01", "message": "image changed",
    })
    _product(db, "day1", "2026-09-11T08:02:00", 10.99, 7100, "https://example.com/new.jpg")
    _search(db, "day1", "2026-09-11T08:06:00", organic=15)
    _product(db, "day3", "2026-09-13T08:02:00", 10.99, 5310, "https://example.com/new.jpg")
    _search(db, "day3", "2026-09-13T08:06:00", organic=8, ad=2)

    result = history.build_asin_history(
        db, "project", "b000000001", 30, datetime(2026, 9, 16, 12),
    )

    assert result["identity"] == "内部名称｜Brand｜B000000001"
    assert result["summary"]["important_nodes"] == 1
    assert result["summary"]["price_promo"] == 1
    assert result["summary"]["listing"] == 1
    assert result["actions"][0]["title"] == "价格与促销、Listing内容组合变化"
    assert len(result["actions"][0]["items"]) == 2
    assert result["listing_changes"][0]["item"]["old"] == ["https://example.com/old.jpg"]
    assert result["listing_changes"][0]["item"]["summary"] == "主图变化"

    assert all(row["metric"] != "main_bsr" for row in result["impacts"])
    subcategory = next(row for row in result["impacts"] if row["metric"] == "subcategory_bsr")
    assert subcategory["metric_label"] == "小类目BSR"
    assert subcategory["points"]["before"]["value"] == 10
    organic = next(row for row in result["impacts"] if row["metric"] == "organic_rank")
    assert organic["points"]["before"]["value"] == 18
    assert organic["points"]["day_1"]["value"] == 15
    assert organic["points"]["day_3"]["value"] == 8
    assert result["actions"][0]["impacts"] == result["impacts"]
    assert result["intraday"]["ads"][0]["status"] == "observed"
    assert any(row["status"] == "not_observed" for row in result["intraday"]["ads"])


def test_history_rejects_unknown_asin_and_invalid_period(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    monkeypatch.setattr(history, "load_competitors", lambda project_id, include_disabled=False: [])
    with pytest.raises(ValueError, match="不在所选产品项目"):
        history.build_asin_history(db, "project", "B000000001")
    with pytest.raises(ValueError, match="只能选择"):
        history.build_asin_history(db, "project", "B000000001", 90)


def test_history_ignores_legacy_blank_rank_values(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    monkeypatch.setattr(history, "load_competitors", lambda project_id, include_disabled=False: [
        Competitor("project", "B000000001", "内部名称", "Brand"),
    ])
    monkeypatch.setattr(history, "load_keywords", lambda project_id=None: [
        Keyword("project", "cleaning stone", 3),
    ])
    _product(db, "run", "2026-09-15T08:00:00", 12.99, 8420)
    _search(db, "run", "2026-09-15T08:05:00", organic="")

    result = history.build_asin_history(
        db, "project", "B000000001", 7, datetime(2026, 9, 16, 12),
    )

    assert result["asin"] == "B000000001"
