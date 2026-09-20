from __future__ import annotations

import logging
from datetime import datetime

from app.candidates import classify_candidate, sync_candidate_alerts
from app.collectors.base import record_error
from app.parsers.product import parse_product

LOGGER = logging.getLogger(__name__)


async def collect_candidate_details(browser, db, run_id: str, project_id: str, candidates: list[dict], settings: dict) -> None:
    if not candidates:
        return
    retries = int(settings["amazon"].get("retries", 2))
    page = await browser.new_page()
    classified_asins: set[str] = set()
    try:
        for candidate in candidates[:25]:
            asin, url = candidate["asin"], candidate["detail_url"]
            for attempt in range(retries + 1):
                try:
                    LOGGER.info("筛选BSR潜力竞品候选 %s", asin)
                    await browser.goto(page, url)
                    product = parse_product(await page.content(), asin, page.url)
                    if not product["success"]:
                        raise ValueError("潜力竞品候选详情页缺少标题")
                    date_value = (
                        candidate.get("date_first_available")
                        if candidate.get("date_source") == "manual"
                        else product.get("date_first_available")
                    )
                    classification = classify_candidate(
                        project_id, {**product, "date_first_available": date_value},
                    )
                    candidate_values = {key: value for key, value in candidate.items() if key != "id"}
                    db.upsert_candidate({
                        **candidate_values, "title": product.get("title"), "brand": product.get("brand"),
                        "date_first_available": date_value,
                        "date_source": candidate.get("date_source") if candidate.get("date_source") == "manual" else ("auto" if date_value else None),
                        "last_checked_at": datetime.now().isoformat(timespec="seconds"),
                        **classification,
                    })
                    classified_asins.add(asin)
                    break
                except Exception as exc:
                    await record_error(db, browser, page, run_id, project_id, "candidate", asin, url, exc, attempt)
                    if attempt < retries:
                        page = await browser.recover_page(page, attempt)
    finally:
        await page.close()
    if classified_asins:
        sync_candidate_alerts(db, run_id, project_id, classified_asins)
