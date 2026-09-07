import asyncio
from unittest.mock import AsyncMock

from app.collectors.product_collector import collect_products
from app.config import Competitor
from app.storage.database import Database


class FakePage:
    url = "https://www.amazon.com/dp/B000000001"

    def __init__(self, html: str):
        self.content = AsyncMock(return_value=html)
        self.close = AsyncMock()


class FakeBrowser:
    def __init__(self, page: FakePage):
        self.page = page
        self.goto = AsyncMock()
        self.recover_page = AsyncMock(return_value=page)

    async def new_page(self):
        return self.page


def test_confirmed_dog_page_is_valid_monitoring_result_after_retries(tmp_path):
    db = Database(tmp_path / "test.db")
    page = FakePage(
        "<html><head><title>Amazon.com Page Not Found</title></head>"
        "<body>Sorry! We couldn't find that page. Dogs of Amazon</body></html>"
    )
    browser = FakeBrowser(page)
    settings = {"amazon": {"retries": 2, "base_url": "https://www.amazon.com"}}

    result = asyncio.run(collect_products(
        browser, db, "run", "project",
        [Competitor("project", "B000000001")], settings,
    ))

    assert result == (1, 0, 0)
    assert browser.goto.await_count == 3
    assert browser.recover_page.await_count == 2
    assert db.fetchall("SELECT success,listing_status FROM product_snapshots") == [
        {"success": 0, "listing_status": "dog"},
    ]
    assert db.fetchall("SELECT success FROM collection_task_outcomes") == [{"success": 1}]
