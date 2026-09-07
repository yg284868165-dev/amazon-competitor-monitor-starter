from app.presentation import format_change_summary, format_product_identity


def test_product_identity_prefers_internal_name_and_brand():
    assert format_product_identity("B000000010", "主竞品", "ExampleCo", "Long title") == "主竞品｜ExampleCo｜B000000010"


def test_product_identity_falls_back_to_observed_title():
    value = format_product_identity("B000000010", "", "ExampleCo", "ExampleCo Sample Widget")
    assert value == "ExampleCo Sample Widget｜B000000010"


def test_rank_summary_uses_absolute_positions_not_percentages():
    value = format_change_summary({
        "event_type": "rank_changed", "field_name": "organic_rank", "keyword": "sample widget",
        "old_value": "24", "new_value": "11",
    })
    assert value == "“sample widget”自然排名从24名提升到11名"
    assert "%" not in value


def test_main_image_change_summary_is_concise():
    assert format_change_summary({
        "field_name": "image_hash", "old_value": "https://example.com/old.jpg",
        "new_value": "https://example.com/new.jpg",
    }) == "主图变化"
