import asyncio
from unittest.mock import AsyncMock

import pytest

import app.browser.manager as browser_manager
from app.browser.manager import BrowserManager


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
