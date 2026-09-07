from __future__ import annotations

import logging
from datetime import datetime

from app.collectors.base import record_error
from app.config import Competitor
from app.parsers.product import parse_product
from app.storage.database import Database

LOGGER = logging.getLogger(__name__)


async def collect_products(browser, db: Database, run_id: str, project_id: str, competitors: list[Competitor], settings: dict) -> tuple[int, int, int]:
    success = failed = captcha = 0
    retries = int(settings["amazon"].get("retries", 2))
    base_url = settings["amazon"].get("base_url", "https://www.amazon.com")
    page = await browser.new_page()
    try:
        for item in competitors:
            url = f"{base_url}/dp/{item.asin}"
            item_ok = False
            for attempt in range(retries + 1):
                try:
                    LOGGER.info("采集商品 %s (%d/%d)", item.asin, competitors.index(item) + 1, len(competitors))
                    await browser.goto(page, url)
                    data = parse_product(await page.content(), item.asin, page.url)
                    if data["listing_status"] in {"dog", "removed"}:
                        if attempt < retries:
                            LOGGER.warning(
                                "商品 %s 页面状态为 %s，重新访问确认 (%d/%d)",
                                item.asin, data["listing_status"], attempt + 1, retries + 1,
                            )
                            page = await browser.recover_page(page, attempt)
                            continue
                        data.update({
                            "run_id": run_id, "project_id": project_id,
                            "collected_at": datetime.now().isoformat(timespec="seconds"),
                        })
                        db.insert("product_snapshots", data)
                        LOGGER.warning(
                            "商品 %s 经 %d 次访问确认页面状态为 %s",
                            item.asin, retries + 1, data["listing_status"],
                        )
                        success += 1
                        item_ok = True
                        break
                    if not data["success"]:
                        raise ValueError("商品页缺少标题，页面可能未完整加载")
                    data.update({"run_id": run_id, "project_id": project_id, "collected_at": datetime.now().isoformat(timespec="seconds")})
                    db.insert("product_snapshots", data)
                    success += 1
                    item_ok = True
                    break
                except Exception as exc:
                    is_captcha = await record_error(db, browser, page, run_id, project_id, "product", item.asin, url, exc, attempt)
                    captcha += int(is_captcha)
                    if attempt >= retries:
                        failed += 1
                    else:
                        page = await browser.recover_page(page, attempt)
            db.record_outcome(run_id, project_id, "product", item.asin, item_ok)
    finally:
        await page.close()
    return success, failed, captcha
