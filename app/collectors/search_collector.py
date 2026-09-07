from __future__ import annotations

import logging
from datetime import datetime

from app.collectors.base import record_error, search_url
from app.config import Keyword
from app.parsers.search import parse_search_page
from app.storage.database import Database

LOGGER = logging.getLogger(__name__)


async def collect_searches(browser, db: Database, run_id: str, project_id: str, keywords: list[Keyword], settings: dict) -> tuple[int, int, int]:
    success = failed = captcha = 0
    retries = int(settings["amazon"].get("retries", 2))
    base_url = settings["amazon"].get("base_url", "https://www.amazon.com")
    page = await browser.new_page()
    try:
        for item in keywords:
            counters = {"absolute": 0, "organic": 0, "ad": 0}
            keyword_ok = True
            for page_number in range(1, item.pages + 1):
                url = search_url(base_url, item.keyword, page_number)
                page_ok = False
                for attempt in range(retries + 1):
                    try:
                        LOGGER.info("采集关键词 %s 第 %d/%d 页", item.keyword, page_number, item.pages)
                        await browser.goto(page, url)
                        rows = parse_search_page(await page.content(), item.keyword, page_number, counters)
                        if len(rows) < 5:
                            raise ValueError(f"搜索结果仅解析到 {len(rows)} 个商品")
                        now = datetime.now().isoformat(timespec="seconds")
                        for row in rows:
                            row.update({"run_id": run_id, "project_id": project_id, "collected_at": now})
                            db.insert("search_snapshots", row)
                        page_ok = True
                        break
                    except Exception as exc:
                        is_captcha = await record_error(db, browser, page, run_id, project_id, "search", item.keyword, url, exc, attempt)
                        captcha += int(is_captcha)
                        if attempt < retries:
                            page = await browser.recover_page(page, attempt)
                if not page_ok:
                    keyword_ok = False
                    break
            success += int(keyword_ok)
            failed += int(not keyword_ok)
            db.record_outcome(run_id, project_id, "search", item.keyword, keyword_ok)
    finally:
        await page.close()
    return success, failed, captcha
