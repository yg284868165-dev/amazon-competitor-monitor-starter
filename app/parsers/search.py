from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.parsers.common import first_text, parse_int, parse_price, parse_rating


def parse_search_page(html: str, keyword: str, page_number: int, counters: dict[str, int]) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []
    for card in soup.select("[data-component-type='s-search-result'][data-asin]"):
        asin = str(card.get("data-asin") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{10}", asin):
            continue
        card_text = card.get_text(" ", strip=True)
        sponsored = bool(card.select_one("[aria-label*='Sponsored'], .puis-sponsored-label-text")) or "Sponsored" in card_text[:120]
        counters["absolute"] += 1
        if sponsored:
            counters["ad"] += 1
            ad_rank, organic_rank = counters["ad"], None
        else:
            counters["organic"] += 1
            organic_rank, ad_rank = counters["organic"], None
        title = first_text(card, ["h2 span", "h2 a span"])
        price_text = first_text(card, [".a-price .a-offscreen"])
        rating_text = first_text(card, [".a-icon-alt"])
        rating_count_text = first_text(card, ["a[href*='customerReviews'] span", "span[aria-label$='ratings']"])
        coupon = first_text(card, [".s-coupon-highlight-color", "[class*='coupon']"])
        badge_nodes = card.select(".a-badge-text, .a-color-state, [class*='badge']")
        badges = list(dict.fromkeys(x.get_text(" ", strip=True) for x in badge_nodes if x.get_text(" ", strip=True)))
        results.append({
            "keyword": keyword, "page": page_number, "absolute_position": counters["absolute"],
            "organic_rank": organic_rank, "ad_rank": ad_rank, "asin": asin,
            "is_sponsored": int(sponsored), "ad_type": "Sponsored Products" if sponsored else None,
            "title": title, "price": parse_price(price_text), "coupon_text": coupon,
            "rating_count": parse_int(rating_count_text), "rating_value": parse_rating(rating_text),
            "badges_json": badges,
        })
    return results

