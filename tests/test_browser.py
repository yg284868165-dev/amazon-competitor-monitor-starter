import asyncio
from unittest.mock import AsyncMock

import pytest

import app.browser.manager as browser_manager
from app.browser.manager import BrowserManager, ContinueShoppingError


class _ContinueShoppingButton:
    def __init__(self, page, click_error: Exception | None = None):
        self.page = page
        self.click_error = click_error
        self.click_count = 0

    @property
    def first(self):
        return self

    async def count(self):
        return int(self.page.is_continue_shopping)

    async def inner_text(self):
        return "Continue shopping"

    async def click(self):
        self.click_count += 1
        if self.click_error:
            raise self.click_error


class _BodyLocator:
    def __init__(self, page):
        self.page = page

    async def inner_text(self):
        if self.page.is_continue_shopping:
            return "Click the button below to continue shopping"
        return "Amazon product page"


class _ContinueShoppingPage:
    def __init__(self, responses: list[bool], click_error: Exception | None = None):
        self.responses = responses
        self.goto_urls: list[str] = []
        self.waits: list[int] = []
        self.button = _ContinueShoppingButton(self, click_error)

    @property
    def is_continue_shopping(self):
        return self.responses[min(len(self.goto_urls) - 1, len(self.responses) - 1)]

    async def goto(self, url, wait_until):
        assert wait_until == "domcontentloaded"
        self.goto_urls.append(url)

    async def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)

    async def title(self):
        return "Amazon.com"

    def locator(self, selector):
        if selector == "body":
            return _BodyLocator(self)
        if selector == browser_manager._CONTINUE_SHOPPING_BUTTON_SELECTOR:
            return self.button
        raise AssertionError(f"Unexpected selector: {selector}")


def _settings(attempts: int = 3) -> dict:
    return {
        "amazon": {
            "postal_code": "90001",
            "initialization_attempts": attempts,
            "initialization_retry_delay_seconds": 3,
        }
    }


def test_postal_code_initialization_retries_transient_abort(monkeypatch):
    manager = BrowserManager(_settings())
    manager._set_postal_code_once = AsyncMock(side_effect=[RuntimeError("net::ERR_ABORTED"), None])
    sleep = AsyncMock()
    monkeypatch.setattr(browser_manager.asyncio, "sleep", sleep)

    asyncio.run(manager._set_postal_code(object()))

    assert manager._set_postal_code_once.await_count == 2
    sleep.assert_awaited_once_with(3.0)


def test_postal_code_initialization_reports_failure_after_all_attempts(monkeypatch):
    manager = BrowserManager(_settings(attempts=3))
    manager._set_postal_code_once = AsyncMock(side_effect=RuntimeError("net::ERR_ABORTED"))
    monkeypatch.setattr(browser_manager.asyncio, "sleep", AsyncMock())

    with pytest.raises(RuntimeError, match="已尝试 3 次"):
        asyncio.run(manager._set_postal_code(object()))

    assert manager._set_postal_code_once.await_count == 3


def test_recover_page_waits_and_replaces_error_tab(monkeypatch):
    manager = BrowserManager({"amazon": {"retry_delay_seconds": 5}})
    old_page = AsyncMock()
    fresh_page = object()
    manager.new_page = AsyncMock(return_value=fresh_page)
    sleep = AsyncMock()
    monkeypatch.setattr(browser_manager.asyncio, "sleep", sleep)

    result = asyncio.run(manager.recover_page(old_page, attempt=1))

    old_page.close.assert_awaited_once()
    sleep.assert_awaited_once_with(10.0)
    assert result is fresh_page


def test_goto_clicks_continue_shopping_once_and_revisits_original_url(monkeypatch):
    manager = BrowserManager({"amazon": {"min_delay_seconds": 0, "max_delay_seconds": 0}})
    page = _ContinueShoppingPage([True, False])
    monkeypatch.setattr(browser_manager.asyncio, "sleep", AsyncMock())

    asyncio.run(manager.goto(page, "https://www.amazon.com/zgbs/example"))

    assert page.goto_urls == ["https://www.amazon.com/zgbs/example"] * 2
    assert page.button.click_count == 1
    assert page.waits == [1500, 1500]


def test_goto_stops_after_continue_shopping_click_limit(monkeypatch):
    manager = BrowserManager({"amazon": {"min_delay_seconds": 0, "max_delay_seconds": 0}})
    page = _ContinueShoppingPage([True, True])
    monkeypatch.setattr(browser_manager.asyncio, "sleep", AsyncMock())

    with pytest.raises(ContinueShoppingError, match="自动点击已达上限（1 次）"):
        asyncio.run(manager.goto(page, "https://www.amazon.com/zgbs/example"))

    assert page.goto_urls == ["https://www.amazon.com/zgbs/example"] * 2
    assert page.button.click_count == 1


def test_goto_reports_clear_error_when_continue_shopping_click_fails(monkeypatch):
    manager = BrowserManager({"amazon": {"min_delay_seconds": 0, "max_delay_seconds": 0}})
    page = _ContinueShoppingPage([True], click_error=RuntimeError("click failed"))
    monkeypatch.setattr(browser_manager.asyncio, "sleep", AsyncMock())

    with pytest.raises(ContinueShoppingError, match="自动确认失败，无法返回目标页面"):
        asyncio.run(manager.goto(page, "https://www.amazon.com/zgbs/example"))

    assert page.goto_urls == ["https://www.amazon.com/zgbs/example"]
    assert page.button.click_count == 1
