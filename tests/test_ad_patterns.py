from datetime import date

import app.ad_patterns as ad_patterns
from app.ad_patterns import build_daily_ad_paths, collect_ad_observations, summarize_ad_insights
from app.config import Competitor, Keyword, Project
from app.storage.database import Database


def _observation(day: str, slot: str, status: str, rank: int | None = None) -> dict:
    return {
        "run_id": f"{day}-{slot}", "run_type": "all", "project_id": "project",
        "project_name": "测试项目", "asin": "B000000001", "product": "测试商品｜B000000001",
        "keyword": "cleaning stone", "status": status, "ad_rank": rank,
        "china_datetime": f"{day}T20:00+08:00",
        "pacific_datetime": f"{day}T{slot}:00-07:00", "pacific_date": day,
        "slot": slot, "slot_order": int(slot[:2]),
    }


def test_repeated_daypart_pattern_becomes_compact_high_confidence_insight():
    observations = []
    for day, strong, weak in (
        ("2026-08-31", 3, 17), ("2026-09-01", 4, 20),
        ("2026-09-02", 2, 15), ("2026-09-03", 5, 22),
    ):
        observations.extend([
            _observation(day, "11:00", "observed", strong),
            _observation(day, "23:00", "observed", weak),
        ])

    insights = summarize_ad_insights(observations, date(2026, 8, 31), date(2026, 9, 6))

    assert len(insights) == 1
    assert insights[0]["confidence"] == "高"
    assert "4个可比日中有4天重复" in insights[0]["observation"]
    assert "白天提高竞价" in insights[0]["inference"]


def test_daily_path_preserves_observed_absent_and_failed_samples():
    rows = [
        _observation("2026-09-01", "05:00", "observed", 19),
        _observation("2026-09-01", "11:00", "not_observed"),
        _observation("2026-09-01", "17:00", "failed"),
        _observation("2026-09-01", "23:00", "observed", 5),
    ]

    paths = build_daily_ad_paths(rows)

    assert paths[0]["path"] == "05:00 第19位 → 11:00 未观察到 → 17:00 采集失败 → 23:00 第5位"
    assert (paths[0]["best_rank"], paths[0]["worst_rank"]) == (5, 19)
    assert paths[0]["successful_count"] == 3


def test_collection_keeps_each_batch_and_converts_to_pacific_time(monkeypatch, tmp_path):
    db = Database(tmp_path / "test.db")
    monkeypatch.setattr(ad_patterns, "load_projects", lambda: [Project("project", "测试项目", "90001")])
    monkeypatch.setattr(ad_patterns, "load_competitors", lambda *_: [Competitor("project", "B000000001")])
    monkeypatch.setattr(ad_patterns, "load_keywords", lambda *args, **kwargs: [Keyword("project", "cleaning stone", 3)])
    monkeypatch.setattr(ad_patterns, "build_product_identities", lambda *_: {"B000000001": "测试商品｜B000000001"})
    with db.connect() as connection:
        for run_id, stamp, success in (
            ("observed", "2026-09-02T20:00:00", 1),
            ("absent", "2026-09-02T21:00:00", 1),
            ("failed", "2026-09-02T22:00:00", 0),
        ):
            connection.execute(
                """INSERT INTO collection_runs(run_id,project_id,task_type,started_at,finished_at,status,total_tasks)
                   VALUES(?,?, 'all',?,?,?,1)""",
                (run_id, "project", stamp, stamp, "success" if success else "failed"),
            )
            connection.execute(
                """INSERT INTO collection_task_outcomes(run_id,project_id,task_type,target,success,finished_at)
                   VALUES(?,?,'search','cleaning stone',?,?)""",
                (run_id, "project", success, stamp),
            )
        connection.execute(
            """INSERT INTO search_snapshots(
                   run_id,project_id,keyword,page,absolute_position,ad_rank,asin,is_sponsored,collected_at
               ) VALUES('observed','project','cleaning stone',1,1,4,'B000000001',1,'2026-09-02T20:03:00')"""
        )
        connection.execute(
            """INSERT INTO search_snapshots(
                   run_id,project_id,keyword,page,absolute_position,asin,is_sponsored,collected_at
               ) VALUES('absent','project','cleaning stone',1,1,'B999999999',0,'2026-09-02T21:03:00')"""
        )

    rows = collect_ad_observations(db, date(2026, 9, 2), date(2026, 9, 2))

    assert [row["status"] for row in rows] == ["observed", "not_observed", "failed"]
    assert rows[0]["pacific_datetime"].startswith("2026-09-02T05:03")
