from __future__ import annotations

import logging
from datetime import datetime

from app.candidates import classify_candidate, create_candidate_alert
from app.collectors.base import record_error
from app.config import load_competitors
from app.parsers.product import parse_product

LOGGER = logging.getLogger(__name__)


async def collect_candidate_details(browser, db, run_id: str, project_id: str, candidates: list[dict], settings: dict) -> None:
    if not candidates:
        return
    retries = int(settings["amazon"].get("retries", 2))
    monitored_asins = {
        item.asin for item in load_competitors(project_id, include_disabled=True)
    }
    page = await browser.new_page()
    try:
        for candidate in candidates[:25]:
            asin, url = candidate["asin"], candidate["detail_url"]
            for attempt in range(retries + 1):
                try:
                    LOGGER.info("筛选BSR新竞争对手候选 %s", asin)
                    await browser.goto(page, url)
                    product = parse_product(await page.content(), asin, page.url)
                    if not product["success"]:
                        raise ValueError("新竞争对手候选详情页缺少标题")
                    classification = classify_candidate(project_id, product)
                    candidate_values = {key: value for key, value in candidate.items() if key != "id"}
                    db.upsert_candidate({
                        **candidate_values, "title": product.get("title"), "brand": product.get("brand"),
                        "date_first_available": product.get("date_first_available"),
                        "date_source": "auto" if product.get("date_first_available") else candidate.get("date_source"),
                        "last_checked_at": datetime.now().isoformat(timespec="seconds"),
                        **classification,
                    })
                    create_candidate_alert(db, run_id, project_id, asin, monitored_asins)
                    break
                except Exception as exc:
                    await record_error(db, browser, page, run_id, project_id, "candidate", asin, url, exc, attempt)
                    if attempt < retries:
                        page = await browser.recover_page(page, attempt)
    finally:
        await page.close()
