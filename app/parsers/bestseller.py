from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.parsers.common import first_text, parse_int, parse_price, parse_rating


def parse_bestseller_page(html: str, category_name: str, source_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []
    cards = soup.select("#gridItemRoot") or soup.select(".zg-grid-general-faceout")
    for card in cards:
        rank_text = first_text(card, [".zg-bdg-text", ".zg-badge-text"])
        rank = parse_int(rank_text)
        link = card.select_one("a[href*='/dp/'], a[href*='/gp/product/']")
        href = str(link.get("href") or "") if link else ""
        match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", href, re.I)
        if not rank or not match:
            continue
        asin = match.group(1).upper()
        title = first_text(card, ["._cDEzb_p13n-sc-css-line-clamp-3_g3dy1", "._cDEzb_p13n-sc-css-line-clamp-2_EWgCb", "a span div"])
        price_text = first_text(card, ["._cDEzb_p13n-sc-price_3mJ9Z", ".p13n-sc-price", ".a-price .a-offscreen"])
        rating_text = first_text(card, [".a-icon-alt"])
        count_text = first_text(card, ["a[href*='customerReviews'] span:last-child"])
        results.append({
            "category_name": category_name, "rank": rank, "asin": asin, "title": title,
            "brand": None, "price": parse_price(price_text), "rating_count": parse_int(count_text),
            "rating_value": parse_rating(rating_text), "badges_json": [], "source_url": source_url,
        })
    return results
