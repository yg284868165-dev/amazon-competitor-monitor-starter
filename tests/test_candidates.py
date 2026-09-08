from datetime import date
from pathlib import Path
import tempfile
from types import SimpleNamespace

import app.candidates as candidates
import pytest
from app.storage.database import Database


def test_candidate_classification_requires_same_type_and_recent_listing(monkeypatch):
    monkeypatch.setattr(candidates, "load_rules", lambda: {
        "p": {"include_keywords": ["sample widget"], "exclude_keywords": ["excluded accessory"], "max_age_days": 90}
    })
    recent = candidates.classify_candidate("p", {
        "title": "Sample Widget Household Cleaner", "highlights_text": "", "about_items_json": [],
        "date_first_available": "2026-08-12",
    }, date(2026, 9, 1))
    assert recent["relevance_status"] == "same" and recent["age_days"] == 20
    old = candidates.classify_candidate("p", {
        "title": "Sample Widget Household Cleaner", "highlights_text": "", "about_items_json": [],
        "date_first_available": "2026-01-01",
    }, date(2026, 9, 1))
    assert old["relevance_status"] == "too_old"
    boundary = candidates.classify_candidate("p", {
        "title": "Sample Widget Household Cleaner", "highlights_text": "", "about_items_json": [],
        "date_first_available": "2026-06-03",
    }, date(2026, 9, 1))
    assert boundary["age_days"] == 90 and boundary["relevance_status"] == "too_old"
    old_without_keyword_match = candidates.classify_candidate("p", {
        "title": "Generic Household Cleaner", "highlights_text": "", "about_items_json": [],
        "date_first_available": "2026-01-01",
    }, date(2026, 9, 1))
    assert old_without_keyword_match["relevance_status"] == "too_old"
    excluded = candidates.classify_candidate("p", {
        "title": "Sample Widget Excluded Accessory", "highlights_text": "", "about_items_json": [],
        "date_first_available": "2026-08-12",
    }, date(2026, 9, 1))
    assert excluded["relevance_status"] == "not_same"


def test_candidate_include_phrase_broad_matches_any_word_and_rejects_no_match(monkeypatch):
    monkeypatch.setattr(candidates, "load_rules", lambda: {
        "p": {"include_keywords": ["sample widget"], "exclude_keywords": [], "max_age_days": 90}
    })
    widget_only = candidates.classify_candidate("p", {
        "title": "Natural Widget Cleaner", "highlights_text": "", "about_items_json": [],
        "date_first_available": "2026-08-20",
    }, date(2026, 9, 1))
    sample_only = candidates.classify_candidate("p", {
        "title": "Sample Cleaning Tool", "highlights_text": "", "about_items_json": [],
        "date_first_available": "2026-08-20",
    }, date(2026, 9, 1))
    unrelated = candidates.classify_candidate("p", {
        "title": "Kitchen Scrub Pad", "highlights_text": "", "about_items_json": [],
        "date_first_available": None,
    }, date(2026, 9, 1))

    assert widget_only["relevance_status"] == "same"
    assert "widget（规则：sample widget）" in widget_only["relevance_reason"]
    assert sample_only["relevance_status"] == "same"
    assert unrelated["relevance_status"] == "not_same"
    assert "自动判定为非同类" in unrelated["relevance_reason"]


def test_candidate_without_configured_include_keywords_stays_pending(monkeypatch):
    monkeypatch.setattr(candidates, "load_rules", lambda: {
        "p": {"include_keywords": [], "exclude_keywords": [], "max_age_days": 90}
    })
    result = candidates.classify_candidate("p", {
        "title": "Any Product", "highlights_text": "", "about_items_json": [],
        "date_first_available": "2026-08-20",
    }, date(2026, 9, 1))
    assert result["relevance_status"] == "pending"
    assert "尚未配置" in result["relevance_reason"]


def test_refresh_automatic_candidates_applies_new_broad_rule(monkeypatch, tmp_path):
    monkeypatch.setattr(candidates, "load_rules", lambda: {
        "p": {"include_keywords": ["sample widget"], "exclude_keywords": [], "max_age_days": 90}
    })
    db = Database(tmp_path / "test.db")
    for asin, title in (("B000000001", "Natural Widget Cleaner"), ("B000000002", "Kitchen Scrub Pad")):
        db.upsert_candidate({
            "project_id": "p", "asin": asin, "first_seen_at": "2026-09-01T08:00:00",
            "last_seen_at": "2026-09-01T08:00:00", "title": title,
            "date_first_available": "2026-08-20", "relevance_status": "pending",
            "classification_source": "auto", "relevance_reason": "old",
        })

    changed = candidates.refresh_automatic_classifications(db, today=date(2026, 9, 1))
    stored = db.fetchall("SELECT asin,relevance_status FROM bsr_new_candidates ORDER BY asin")

    assert {row["asin"] for row in changed} == {"B000000001", "B000000002"}
    assert stored == [
        {"asin": "B000000001", "relevance_status": "same"},
        {"asin": "B000000002", "relevance_status": "not_same"},
    ]


def test_only_qualified_candidate_creates_alert(monkeypatch):
    monkeypatch.setattr(candidates, "load_rules", lambda: {"p": {"max_age_days": 90}})
    monkeypatch.setattr(candidates, "load_competitors", lambda project_id, include_disabled=False: [])
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.db")
        run_id = db.start_run("p", "bestseller", 1)
        db.upsert_candidate({
            "project_id": "p", "asin": "B000000001", "first_seen_at": "2026-09-01T08:00:00",
            "last_seen_at": "2026-09-01T08:00:00", "category_name": "Test", "current_rank": 12,
            "age_days": 20, "relevance_status": "same", "classification_source": "auto",
        })
        assert candidates.create_candidate_alert(db, run_id, "p", "B000000001") is True
        assert candidates.create_candidate_alert(db, run_id, "p", "B000000001") is False
        events = db.fetchall("SELECT * FROM change_events")
        assert len(events) == 1 and events[0]["event_type"] == "new_competitor_found"


def test_current_unmonitored_asin_enters_candidate_pool_without_being_new_to_ranking(monkeypatch):
    monkeypatch.setattr(
        candidates,
        "load_competitors",
        lambda project_id, include_disabled=False: [SimpleNamespace(asin="B000000002")],
    )
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.db")
        old_run = db.start_run("p", "bestseller", 1)
        new_run = db.start_run("p", "bestseller", 1)
        base = {"project_id": "p", "category_name": "Test", "snapshot_date": "2026-09-01", "source_url": "https://example.com"}
        db.insert("bestseller_snapshots", {**base, "run_id": old_run, "rank": 1, "asin": "B000000001", "collected_at": "2026-09-01T08:00:00"})
        db.insert("bestseller_snapshots", {**base, "run_id": new_run, "rank": 1, "asin": "B000000001", "title": "Existing Sample Widget", "collected_at": "2026-09-01T14:00:00"})
        db.insert("bestseller_snapshots", {**base, "run_id": new_run, "rank": 2, "asin": "B000000002", "title": "Configured Sample Widget", "collected_at": "2026-09-01T14:00:00"})
        found = candidates.discover_candidates(db, new_run, "p")
        assert [row["asin"] for row in found] == ["B000000001"]
        stored = db.fetchall("SELECT asin,relevance_status FROM bsr_new_candidates")
        assert stored == [{"asin": "B000000001", "relevance_status": "pending"}]
        assert db.fetchall("SELECT COUNT(*) count FROM change_events")[0]["count"] == 0


def test_monitored_asin_cannot_create_candidate_alert(monkeypatch):
    monkeypatch.setattr(
        candidates,
        "load_competitors",
        lambda project_id, include_disabled=False: [SimpleNamespace(asin="B000000001")],
    )
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.db")
        run_id = db.start_run("p", "bestseller", 1)
        db.upsert_candidate({
            "project_id": "p", "asin": "B000000001", "first_seen_at": "2026-09-01T08:00:00",
            "last_seen_at": "2026-09-01T08:00:00", "category_name": "Test", "current_rank": 12,
            "age_days": 20, "relevance_status": "same", "classification_source": "auto",
        })
        assert candidates.create_candidate_alert(db, run_id, "p", "B000000001") is False
        assert db.fetchall("SELECT COUNT(*) count FROM change_events")[0]["count"] == 0


def test_existing_qualified_candidate_is_alerted_when_still_in_current_top_100(monkeypatch):
    monkeypatch.setattr(candidates, "load_competitors", lambda project_id, include_disabled=False: [])
    monkeypatch.setattr(candidates, "load_rules", lambda: {"p": {"max_age_days": 90}})
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.db")
        run_id = db.start_run("p", "bestseller", 1)
        db.upsert_candidate({
            "project_id": "p", "asin": "B000000001", "first_seen_at": "2026-09-01T08:00:00",
            "last_seen_at": "2026-09-01T08:00:00", "category_name": "Test", "current_rank": 20,
            "age_days": 20, "relevance_status": "same", "classification_source": "auto",
        })
        db.insert("bestseller_snapshots", {
            "run_id": run_id, "project_id": "p", "category_name": "Test", "rank": 18,
            "asin": "B000000001", "snapshot_date": "2026-09-03",
            "source_url": "https://example.com", "collected_at": "2026-09-03T08:00:00",
        })
        assert candidates.discover_candidates(db, run_id, "p") == []
        event = db.fetchall("SELECT event_type,new_value FROM change_events")
        assert event == [{"event_type": "new_competitor_found", "new_value": "18"}]


def test_expired_candidate_rejects_manual_confirmation():
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.db")
        db.upsert_candidate({
            "project_id": "p", "asin": "B000000001", "first_seen_at": "2026-09-01T08:00:00",
            "last_seen_at": "2026-09-01T08:00:00", "category_name": "Test", "current_rank": 12,
            "age_days": 90, "relevance_status": "too_old", "classification_source": "auto",
        })
        with pytest.raises(ValueError, match="无需人工确认"):
            candidates.set_manual_status(db, "p", "B000000001", "same")


def test_existing_pending_candidate_is_automatically_marked_expired(monkeypatch):
    monkeypatch.setattr(candidates, "load_rules", lambda: {"p": {"max_age_days": 90}})
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.db")
        db.upsert_candidate({
            "project_id": "p", "asin": "B000000001", "first_seen_at": "2026-01-01T08:00:00",
            "last_seen_at": "2026-09-01T08:00:00", "category_name": "Test", "current_rank": 12,
            "date_first_available": "2026-01-01", "age_days": 30,
            "relevance_status": "pending", "classification_source": "auto",
        })
        assert candidates.mark_expired_candidates(db, "p", date(2026, 9, 1)) == 1
        row = db.fetchall("SELECT age_days,relevance_status,relevance_reason FROM bsr_new_candidates")[0]
        assert row["age_days"] == 243
        assert row["relevance_status"] == "too_old"
        assert "达到或超过新品期限" in row["relevance_reason"]


def test_manual_candidate_date_recalculates_status_and_can_be_cleared(monkeypatch):
    monkeypatch.setattr(candidates, "load_rules", lambda: {
        "p": {"include_keywords": ["sample widget"], "exclude_keywords": [], "max_age_days": 90}
    })
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.db")
        db.upsert_candidate({
            "project_id": "p", "asin": "B000000001", "first_seen_at": "2026-09-01T08:00:00",
            "last_seen_at": "2026-09-01T08:00:00", "category_name": "Test", "current_rank": 12,
            "title": "Sample Widget Cleaner", "relevance_status": "pending",
            "classification_source": "auto",
        })
        saved = candidates.set_candidate_date(db, "p", "B000000001", "2026-08-12", date(2026, 9, 1))
        assert saved["date_source"] == "manual"
        assert saved["age_days"] == 20
        assert saved["relevance_status"] == "same"

        cleared = candidates.set_candidate_date(db, "p", "B000000001", "", date(2026, 9, 1))
        assert cleared["date_first_available"] is None
        assert cleared["date_source"] is None
        assert cleared["age_days"] is None
        assert cleared["relevance_status"] == "pending"


def test_manual_candidate_date_rejects_future_date(monkeypatch):
    monkeypatch.setattr(candidates, "load_rules", lambda: {})
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.db")
        db.upsert_candidate({
            "project_id": "p", "asin": "B000000001", "first_seen_at": "2026-09-01T08:00:00",
            "last_seen_at": "2026-09-01T08:00:00", "relevance_status": "pending",
            "classification_source": "auto",
        })
        with pytest.raises(ValueError, match="不能晚于今天"):
            candidates.set_candidate_date(db, "p", "B000000001", "2026-09-02", date(2026, 9, 1))
