import asyncio
from unittest.mock import AsyncMock

import app.collectors.bestseller_collector as collector
from app.config import Category


class FakePage:
    url = "https://www.amazon.com/zgbs/example"

    def __init__(self):
        self.evaluate = AsyncMock()
        self.wait_for_timeout = AsyncMock()
        self.content = AsyncMock(return_value="html")
        self.close = AsyncMock()


def _rows(count: int) -> list[dict]:
    return [{"rank": rank, "asin": f"B{rank:09d}"} for rank in range(1, count + 1)]


def test_bestseller_waits_until_lazy_items_reach_minimum(monkeypatch):
    page = FakePage()
    parsed = iter((_rows(30), _rows(50)))
    monkeypatch.setattr(collector, "parse_bestseller_page", lambda *args: next(parsed))

    rows = asyncio.run(collector.wait_for_bestseller_rows(page, "Category", 4, 4))

    assert len(rows) == 50
    assert page.content.await_count == 2
    assert page.wait_for_timeout.await_args_list[-1].args == (4000,)


def test_bestseller_returns_best_partial_render_after_wait(monkeypatch):
    page = FakePage()
    parsed = iter((_rows(38), _rows(30), _rows(30)))
    monkeypatch.setattr(collector, "parse_bestseller_page", lambda *args: next(parsed))

    rows = asyncio.run(collector.wait_for_bestseller_rows(page, "Category", 8, 4))

    assert len(rows) == 38
    assert page.content.await_count == 3


def test_bestseller_plan_for_top_50_uses_one_page_and_scaled_threshold():
    pages, required_unique = collector.bestseller_collection_plan(50, 98)

    assert pages == [(1, 48)]
    assert required_unique == 49


def test_bestseller_plan_for_top_75_uses_partial_second_page():
    pages, required_unique = collector.bestseller_collection_plan(75, 98)

    assert pages == [(1, 48), (2, 25)]
    assert required_unique == 74


def test_bestseller_plan_for_top_100_preserves_existing_threshold():
    pages, required_unique = collector.bestseller_collection_plan(100, 98)

    assert pages == [(1, 48), (2, 48)]
    assert required_unique == 98


def test_top_50_collection_fetches_only_first_page(monkeypatch):
    page = FakePage()

    class Browser:
        new_page = AsyncMock(return_value=page)
        goto = AsyncMock()
        save_diagnostics = AsyncMock()
        recover_page = AsyncMock(return_value=page)

    class Database:
        def __init__(self):
            self.snapshots = []
            self.outcomes = []

        def insert(self, table, row):
            self.snapshots.append((table, dict(row)))

        def record_outcome(self, run_id, project_id, task_type, target, success):
            self.outcomes.append((target, success))

    async def complete_page(*args, **kwargs):
        return _rows(50)

    monkeypatch.setattr(collector, "wait_for_bestseller_rows", complete_page)
    database = Database()
    result = asyncio.run(collector.collect_bestsellers(
        Browser(), database, "run", "project",
        [Category("project", "Top 50", "https://www.amazon.com/zgbs/example", 50)],
        {
            "amazon": {"retries": 2},
            "bestseller": {
                "minimum_complete_items": 98,
                "render_wait_seconds": 20,
                "render_poll_seconds": 4,
            },
        },
    ))

    assert result == (1, 0, 0)
    assert Browser.goto.await_count == 1
    assert "pg=1" in Browser.goto.await_args.args[1]
    assert len(database.snapshots) == 50
    assert database.outcomes == [("Top 50", True)]
