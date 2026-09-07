from __future__ import annotations

from datetime import datetime
from urllib.parse import quote_plus

from app.browser.manager import BrowserManager, CaptchaError
from app.storage.database import Database


async def record_error(db: Database, browser: BrowserManager, page, run_id: str, project_id: str, task_type: str, target: str, url: str, exc: Exception, retry: int) -> bool:
    screenshot = html = None
    try:
        screenshot, html = await browser.save_diagnostics(page, f"{task_type}_{target}")
    except Exception:
        pass
    is_captcha = isinstance(exc, CaptchaError)
    db.insert("collection_errors", {
        "run_id": run_id, "project_id": project_id, "task_type": task_type, "target": target, "url": url,
        "error_type": "captcha" if is_captcha else type(exc).__name__, "error_message": str(exc),
        "retry_count": retry, "screenshot_path": screenshot, "html_path": html,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    return is_captcha


def search_url(base_url: str, keyword: str, page: int) -> str:
    return f"{base_url}/s?k={quote_plus(keyword)}&page={page}"
