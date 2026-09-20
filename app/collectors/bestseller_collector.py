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
PAGE_SIZE = 50
SINGLE_PAGE_MINIMUM = 48


def with_page(url: str, page_number: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["pg"] = str(page_number)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


async def wait_for_bestseller_rows(
    page, category_name: str, wait_seconds: float, poll_seconds: float,
    minimum_rows: int = SINGLE_PAGE_MINIMUM,
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
        if len(best_rows) >= minimum_rows:
            return best_rows
        if check < checks:
            LOGGER.info(
                "榜单 %s 当前仅渲染 %d 个商品，继续等待页面补全（%d/%d）",
                category_name, len(best_rows), check + 1, checks,
            )
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(int(poll_seconds * 1000))
    return best_rows


def bestseller_collection_plan(
    max_rank: int, minimum_complete_items: int,
) -> tuple[list[tuple[int, int]], int]:
    """Return each required page/minimum row count and the overall unique-ASIN target.

    ``minimum_complete_items`` historically represents the required count for a
    100-place list (normally 98), so it also defines the completeness ratio for
    shorter configured ranges.
    """
    if not 1 <= max_rank <= 100:
        raise ValueError("BSR最大排名必须在1–100之间")
    ratio = min(1.0, max(0.01, minimum_complete_items / 100))
    plan = []
    for page_number in range(1, math.ceil(max_rank / PAGE_SIZE) + 1):
        requested_on_page = min(PAGE_SIZE, max_rank - (page_number - 1) * PAGE_SIZE)
        # Preserve the historical 48-row tolerance on a full Amazon page while
        # requiring all requested rows when the final page is only a partial range.
        page_minimum = min(
            SINGLE_PAGE_MINIMUM,
            max(1, math.ceil(requested_on_page * ratio)),
        )
        plan.append((page_number, page_minimum))
    return plan, max(1, math.ceil(max_rank * ratio))


async def collect_bestsellers(browser, db: Database, run_id: str, project_id: str, categories: list[Category], settings: dict) -> tuple[int, int, int]:
    success = failed = captcha = 0
    retries = int(settings["amazon"].get("retries", 2))
    minimum = int(settings["bestseller"].get("minimum_complete_items", 90))
    render_wait = float(settings["bestseller"].get("render_wait_seconds", 20))
    render_poll = float(settings["bestseller"].get("render_poll_seconds", 4))
    page = await browser.new_page()
    try:
        for category in categories:
            page_plan, required_unique = bestseller_collection_plan(
                int(category.max_rank), minimum,
            )
            combined: dict[int, dict] = {}
            category_failed = False
            for page_number, page_minimum in page_plan:
                url = with_page(category.category_url, page_number)
                page_ok = False
                for attempt in range(retries + 1):
                    try:
                        LOGGER.info("采集榜单 %s 第 %d 页", category.category_name, page_number)
                        await browser.goto(page, url)
                        rows = await wait_for_bestseller_rows(
                            page, category.category_name, render_wait, render_poll,
                            page_minimum,
                        )
                        LOGGER.info("榜单 %s 第 %d 页解析到 %d 个商品，排名范围 %s-%s", category.category_name, page_number, len(rows), min((x['rank'] for x in rows), default='-'), max((x['rank'] for x in rows), default='-'))
                        if len(rows) < page_minimum:
                            raise ValueError(
                                f"榜单页面等待 {render_wait:g} 秒后仅解析到 {len(rows)} 个商品，"
                                f"低于当前监控范围的单页完整性门槛 {page_minimum}"
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
            if category_failed or len(unique_asins) < required_unique:
                failed += 1
                db.record_outcome(run_id, project_id, "bestseller", category.category_name, False)
                LOGGER.error(
                    "榜单 %s 完整性校验失败，目标前 %d 名，"
                    "仅有 %d 个唯一 ASIN，要求至少 %d 个",
                    category.category_name, category.max_rank, len(unique_asins), required_unique,
                )
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
