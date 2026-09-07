from __future__ import annotations

import logging
import math
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.collectors.base import record_error
from app.config import Category
from app.parsers.bestseller import parse_bestseller_page
from app.storage.database import Database

LOGGER = logging.getLogger(__name__)
SINGLE_PAGE_MINIMUM = 48


def with_page(url: str, page_number: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["pg"] = str(page_number)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


async def wait_for_bestseller_rows(
    page, category_name: str, wait_seconds: float, poll_seconds: float,
) -> list[dict]:
    # Trigger Amazon's lazy-rendered cards from top to bottom once.
    for index in range(12):
        await page.evaluate(
            "([step,total]) => window.scrollTo(0, document.body.scrollHeight * step / total)",
            [index + 1, 12],
        )
        await page.wait_for_timeout(650)
    await page.evaluate("window.scrollTo(0, 0)")

    poll_seconds = max(1.0, poll_seconds)
    checks = max(0, math.ceil(max(0.0, wait_seconds) / poll_seconds))
    best_rows: list[dict] = []
    for check in range(checks + 1):
        rows = parse_bestseller_page(await page.content(), category_name, page.url)
        if len(rows) > len(best_rows):
            best_rows = rows
        if len(best_rows) >= SINGLE_PAGE_MINIMUM:
            return best_rows
        if check < checks:
            LOGGER.info(
                "榜单 %s 当前仅渲染 %d 个商品，继续等待页面补全（%d/%d）",
                category_name, len(best_rows), check + 1, checks,
            )
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(int(poll_seconds * 1000))
    return best_rows


async def collect_bestsellers(browser, db: Database, run_id: str, project_id: str, categories: list[Category], settings: dict) -> tuple[int, int, int]:
    success = failed = captcha = 0
    retries = int(settings["amazon"].get("retries", 2))
    minimum = int(settings["bestseller"].get("minimum_complete_items", 90))
    render_wait = float(settings["bestseller"].get("render_wait_seconds", 20))
    render_poll = float(settings["bestseller"].get("render_poll_seconds", 4))
    page = await browser.new_page()
    try:
        for category in categories:
            combined: dict[int, dict] = {}
            category_failed = False
            for page_number in (1, 2):
                url = with_page(category.category_url, page_number)
                page_ok = False
                for attempt in range(retries + 1):
                    try:
                        LOGGER.info("采集榜单 %s 第 %d 页", category.category_name, page_number)
                        await browser.goto(page, url)
                        rows = await wait_for_bestseller_rows(
                            page, category.category_name, render_wait, render_poll,
                        )
                        LOGGER.info("榜单 %s 第 %d 页解析到 %d 个商品，排名范围 %s-%s", category.category_name, page_number, len(rows), min((x['rank'] for x in rows), default='-'), max((x['rank'] for x in rows), default='-'))
                        if len(rows) < SINGLE_PAGE_MINIMUM:
                            raise ValueError(
                                f"榜单页面等待 {render_wait:g} 秒后仅解析到 {len(rows)} 个商品，"
                                f"低于单页完整性门槛 {SINGLE_PAGE_MINIMUM}"
                            )
                        for row in rows:
                            if row["rank"] <= category.max_rank:
                                combined[row["rank"]] = row
                        page_ok = True
                        break
                    except Exception as exc:
                        is_captcha = await record_error(db, browser, page, run_id, project_id, "bestseller", category.category_name, url, exc, attempt)
                        captcha += int(is_captcha)
                        if attempt < retries:
                            page = await browser.recover_page(page, attempt)
                if not page_ok:
                    category_failed = True
                    break
            unique_asins = {row["asin"] for row in combined.values()}
            if category_failed or len(unique_asins) < minimum:
                failed += 1
                db.record_outcome(run_id, project_id, "bestseller", category.category_name, False)
                LOGGER.error("榜单 %s 完整性校验失败，仅有 %d 个唯一 ASIN", category.category_name, len(unique_asins))
                try:
                    await browser.save_diagnostics(page, f"bestseller_incomplete_{category.category_name}")
                except Exception:
                    LOGGER.exception("保存榜单不完整诊断文件失败")
                continue
            now = datetime.now().isoformat(timespec="seconds")
            for row in combined.values():
                row.update({"run_id": run_id, "project_id": project_id, "snapshot_date": date.today().isoformat(), "collected_at": now})
                db.insert("bestseller_snapshots", row)
            success += 1
            db.record_outcome(run_id, project_id, "bestseller", category.category_name, True)
    finally:
        await page.close()
    return success, failed, captcha
