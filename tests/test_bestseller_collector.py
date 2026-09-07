import asyncio
from unittest.mock import AsyncMock

import app.collectors.bestseller_collector as collector


class FakePage:
    url = "https://www.amazon.com/zgbs/example"

    def __init__(self):
        self.evaluate = AsyncMock()
        self.wait_for_timeout = AsyncMock()
        self.content = AsyncMock(return_value="html")


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
