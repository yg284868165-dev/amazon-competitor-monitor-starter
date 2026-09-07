from datetime import date, timedelta

from app.trends import _period_result, _qualify


def test_seven_day_rank_trend_uses_absolute_positions():
    result = _qualify("organic_rank", [
        ("2026-09-01", 25), ("2026-09-02", 24), ("2026-09-03", 21),
        ("2026-09-04", 18), ("2026-09-05", 15), ("2026-09-06", 13), ("2026-09-07", 11),
    ], 7)
    assert result is not None
    assert result["start_rank"] == 24
    assert result["end_rank"] == 12
    assert result["direction"] == "提升"
    assert result["change_positions"] == 12


def test_noisy_rank_changes_do_not_form_trend():
    assert _qualify("organic_rank", [
        ("2026-09-01", 20), ("2026-09-02", 10), ("2026-09-03", 23),
        ("2026-09-04", 9), ("2026-09-05", 21), ("2026-09-06", 11), ("2026-09-07", 20),
    ], 7) is None


def _week(values):
    start = date(2026, 8, 31)
    return [((start + timedelta(days=index)).isoformat(), value) for index, value in enumerate(values)]


def test_small_category_threshold_uses_five_positions_or_ten_percent():
    five_positions = _period_result("category_bsr", _week([104, 103, 102, 101, 100, 98, 97]), 7, date(2026, 9, 6))
    ten_percent = _period_result("category_bsr", _week([22, 22, 21, 20, 20, 19, 18]), 7, date(2026, 9, 6))
    assert five_positions["status"] == "trend"  # 6 places, less than 10% of ~100.
    assert ten_percent["status"] == "trend"  # 4 places, less than five but over 10%.


def test_four_week_trend_requires_consistent_weekly_direction():
    start = date(2026, 8, 10)
    values = []
    for week, rank in enumerate((40, 34, 29, 22)):
        values.extend(((start + timedelta(days=week * 7 + day)).isoformat(), rank) for day in range(5))
    result = _period_result("organic_rank", values, 28, date(2026, 9, 6))
    assert result["status"] == "trend"
    assert (result["start_rank"], result["end_rank"]) == (40, 22)
