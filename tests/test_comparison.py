from app.comparison.engine import compare_products
from app.presentation import format_change_summary
from app.storage.database import Database


def _snapshot(run_id, collected_at, **values):
    return {
        "run_id": run_id, "project_id": "project", "asin": "B000000001",
        "collected_at": collected_at, "success": 1, "confidence": "high", **values,
    }


def test_coupon_and_deal_disappearance_create_cancellation_events(tmp_path):
    db = Database(tmp_path / "test.db")
    db.insert("product_snapshots", _snapshot(
        "old", "2026-09-01T08:00:00", coupon_text="Save 10%", deal_text="Lightning Deal",
    ))
    db.insert("product_snapshots", _snapshot(
        "new", "2026-09-01T14:00:00", coupon_text=None, deal_text=None,
    ))

    compare_products(db, "new", "project", {
        "changes": {"price_percent_alert": 5, "bsr_position_alert": 20},
    })

    events = db.fetchall("SELECT * FROM change_events ORDER BY field_name")
    assert [row["field_name"] for row in events] == ["coupon_text", "deal_text"]
    assert all(row["new_value"] is None for row in events)
    assert format_change_summary(events[0]) == "优惠券活动已取消（原：Save 10%）"
    assert format_change_summary(events[1]) == "促销活动已取消（原：Lightning Deal）"


def test_business_price_disappearance_creates_daily_cancellation_event(tmp_path):
    db = Database(tmp_path / "test.db")
    db.insert("product_snapshots", _snapshot(
        "old", "2026-09-01T08:00:00", business_price_text="Business Price: $7.49",
    ))
    db.insert("product_snapshots", _snapshot(
        "new", "2026-09-01T14:00:00", business_price_text=None,
    ))

    compare_products(db, "new", "project", {
        "changes": {"price_percent_alert": 5, "bsr_position_alert": 20},
    })

    event = db.fetchall("SELECT * FROM change_events")[0]
    assert event["field_name"] == "business_price_text"
    assert event["severity"] == "high"
    assert format_change_summary(event) == "企业价已取消（原：Business Price: $7.49）"


def test_rating_change_includes_current_star_distribution(tmp_path):
    db = Database(tmp_path / "test.db")
    db.insert("product_snapshots", _snapshot(
        "old", "2026-09-01T08:00:00", rating_value=4.5,
        rating_breakdown_json={"5": 72, "4": 16, "3": 7, "2": 2, "1": 3},
    ))
    db.insert("product_snapshots", _snapshot(
        "new", "2026-09-01T14:00:00", rating_value=4.6,
        rating_breakdown_json={"5": 74, "4": 15, "3": 7, "2": 2, "1": 2},
    ))

    compare_products(db, "new", "project", {
        "changes": {"price_percent_alert": 5, "bsr_position_alert": 20},
    })

    event = db.fetchall("SELECT * FROM change_events")[0]
    assert event["field_name"] == "rating_value"
    assert format_change_summary(event) == (
        "星级：4.5 → 4.6；当前各星级评价占比："
        "5星 74%、4星 15%、3星 7%、2星 2%、1星 2%"
    )


def test_main_image_change_does_not_create_duplicate_image_set_event(tmp_path):
    db = Database(tmp_path / "test.db")
    db.insert("product_snapshots", _snapshot(
        "old", "2026-09-01T08:00:00", main_image_url="https://example.com/old.jpg",
        image_hash="old-main", product_images_json='["old.jpg","side.jpg"]',
        product_images_hash="old-set",
    ))
    db.insert("product_snapshots", _snapshot(
        "new", "2026-09-01T14:00:00", main_image_url="https://example.com/new.jpg",
        image_hash="new-main", product_images_json='["new.jpg","side.jpg"]',
        product_images_hash="new-set",
    ))

    compare_products(db, "new", "project", {
        "changes": {"price_percent_alert": 5, "bsr_position_alert": 20},
    })

    events = db.fetchall("SELECT field_name FROM change_events")
    assert events == [{"field_name": "image_hash"}]


def test_availability_uses_business_states_instead_of_raw_stock_copy(tmp_path):
    db = Database(tmp_path / "test.db")
    db.insert("product_snapshots", _snapshot("old", "2026-09-01T08:00:00", availability="Only 3 left in stock"))
    db.insert("product_snapshots", _snapshot("same", "2026-09-01T14:00:00", availability="Only 2 left in stock"))
    compare_products(db, "same", "project", {"changes": {"price_percent_alert": 5, "bsr_position_alert": 20}})
    assert db.fetchall("SELECT * FROM change_events") == []

    db.insert("product_snapshots", _snapshot("new", "2026-09-01T20:00:00", availability="Currently unavailable"))
    compare_products(db, "new", "project", {"changes": {"price_percent_alert": 5, "bsr_position_alert": 20}})
    event = db.fetchall("SELECT * FROM change_events")[0]
    assert (event["old_value"], event["new_value"]) == ("低库存", "断货")
    assert format_change_summary(event) == "发生断货（原状态：低库存）"


def test_ships_from_change_creates_daily_event(tmp_path):
    db = Database(tmp_path / "test.db")
    db.insert("product_snapshots", _snapshot("old", "2026-09-01T08:00:00", ships_from="Amazon"))
    db.insert("product_snapshots", _snapshot("new", "2026-09-01T14:00:00", ships_from="Merchant A"))

    compare_products(db, "new", "project", {"changes": {"price_percent_alert": 5, "bsr_position_alert": 20}})

    event = db.fetchall("SELECT * FROM change_events")[0]
    assert event["field_name"] == "ships_from"
    assert (event["old_value"], event["new_value"]) == ("Amazon", "Merchant A")


def test_follow_seller_disappearance_requires_two_missing_snapshots(tmp_path):
    db = Database(tmp_path / "test.db")
    settings = {"changes": {"price_percent_alert": 5, "bsr_position_alert": 20, "missing_confirmations": 2}}
    db.insert("product_snapshots", _snapshot("old", "2026-09-01T08:00:00", offer_count=3))
    db.insert("product_snapshots", _snapshot("missing-one", "2026-09-01T14:00:00", offer_count=None))
    compare_products(db, "missing-one", "project", settings)
    assert db.fetchall("SELECT * FROM change_events") == []

    db.insert("product_snapshots", _snapshot("missing-two", "2026-09-01T20:00:00", offer_count=None))
    compare_products(db, "missing-two", "project", settings)
    event = db.fetchall("SELECT * FROM change_events")[0]
    assert event["field_name"] == "offer_count"
    assert (event["old_value"], event["new_value"]) == ("3", "0")
    assert format_change_summary(event) == "跟卖已消失（此前最多3个）"


def test_high_return_rate_label_change_creates_event_without_backfill_noise(tmp_path):
    db = Database(tmp_path / "test.db")
    settings = {"changes": {"price_percent_alert": 5, "bsr_position_alert": 20}}
    db.insert("product_snapshots", _snapshot(
        "legacy", "2026-09-01T08:00:00", high_return_rate=None,
    ))
    db.insert("product_snapshots", _snapshot(
        "baseline", "2026-09-01T14:00:00", high_return_rate=0,
    ))
    compare_products(db, "baseline", "project", settings)
    assert db.fetchall("SELECT * FROM change_events") == []

    db.insert("product_snapshots", _snapshot(
        "new", "2026-09-01T20:00:00", high_return_rate=1,
    ))
    compare_products(db, "new", "project", settings)

    event = db.fetchall("SELECT * FROM change_events")[0]
    assert event["field_name"] == "high_return_rate"
    assert format_change_summary(event) == "出现高退货率标签"


def test_main_and_small_category_name_changes_create_events(tmp_path):
    db = Database(tmp_path / "test.db")
    settings = {"changes": {"price_percent_alert": 5, "bsr_position_alert": 20}}
    db.insert("product_snapshots", _snapshot(
        "old", "2026-09-01T08:00:00",
        category_ranks_json=[
            {"rank": 200, "category": "Health & Household"},
            {"rank": 8, "category": "Scouring Pads"},
        ],
    ))
    db.insert("product_snapshots", _snapshot(
        "new", "2026-09-01T14:00:00",
        category_ranks_json=[
            {"rank": 180, "category": "Home & Kitchen"},
            {"rank": 5, "category": "Cleaning Tools"},
        ],
    ))

    compare_products(db, "new", "project", settings)

    events = db.fetchall("SELECT * FROM change_events ORDER BY field_name")
    assert [row["field_name"] for row in events] == ["main_category_name", "subcategory_names"]
    assert format_change_summary(events[0]) == "大类目：Health & Household → Home & Kitchen"
    assert format_change_summary(events[1]) == "小类目：Scouring Pads → Cleaning Tools"


def test_category_disappearance_requires_two_missing_snapshots(tmp_path):
    db = Database(tmp_path / "test.db")
    settings = {"changes": {
        "price_percent_alert": 5, "bsr_position_alert": 20,
        "missing_confirmations": 2,
    }}
    ranks = [
        {"rank": 200, "category": "Health & Household"},
        {"rank": 8, "category": "Scouring Pads"},
    ]
    db.insert("product_snapshots", _snapshot(
        "old", "2026-09-01T08:00:00", category_ranks_json=ranks,
    ))
    db.insert("product_snapshots", _snapshot(
        "missing-one", "2026-09-01T14:00:00", category_ranks_json=[],
    ))
    compare_products(db, "missing-one", "project", settings)
    assert db.fetchall("SELECT * FROM change_events") == []

    db.insert("product_snapshots", _snapshot(
        "missing-two", "2026-09-01T20:00:00", category_ranks_json=[],
    ))
    compare_products(db, "missing-two", "project", settings)

    events = db.fetchall("SELECT * FROM change_events ORDER BY field_name")
    assert [row["field_name"] for row in events] == ["main_category_name", "subcategory_names"]
    assert all(row["new_value"] is None for row in events)


def test_listing_dog_state_and_recovery_create_daily_events(tmp_path):
    db = Database(tmp_path / "test.db")
    settings = {"changes": {"price_percent_alert": 5, "bsr_position_alert": 20}}
    db.insert("product_snapshots", _snapshot(
        "old", "2026-09-01T08:00:00", listing_status="active",
    ))
    db.insert("product_snapshots", _snapshot(
        "dog", "2026-09-01T14:00:00", success=0, listing_status="dog",
    ))
    compare_products(db, "dog", "project", settings)
    db.insert("product_snapshots", _snapshot(
        "recovered", "2026-09-01T20:00:00", listing_status="active",
    ))
    compare_products(db, "recovered", "project", settings)

    events = db.fetchall("SELECT * FROM change_events ORDER BY event_time,id")
    assert [format_change_summary(row) for row in events] == [
        "商品页面变狗", "商品页面已恢复正常",
    ]
