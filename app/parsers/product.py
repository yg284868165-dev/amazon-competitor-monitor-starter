from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

from app.parsers.common import clean_text, fingerprint, first_text, parse_int, parse_price, parse_rating


HIGH_RETURN_PATTERNS = (
    r"\bfrequently returned item\b",
    r"\bfrequently returned\b",
    r"\bhigh return rate(?: item)?\b",
)

DOG_PAGE_PATTERNS = (
    r"\bwe couldn['’]t find that page\b",
    r"\bthe web address you entered is not a functioning page\b",
    r"\bamazon(?:\.com)? page not found\b",
    r"\bdogs of amazon\b",
)

REMOVED_PAGE_PATTERNS = (
    r"\bthis item is no longer available\b",
    r"\bthis product is no longer available\b",
    r"\bthe item you are looking for is no longer available\b",
    r"\bthe product you are looking for is no longer available\b",
)

PROMOTION_PATTERNS = (
    r"\bPrime Day Deal\b",
    r"\bPrime Big Deal(?: Days?)?\b",
    r"\bPrime Early Access\b",
    r"\bPrime[- ]Exclusive (?:Deal|Price|Discount)\b",
    r"\bExclusive Prime (?:Deal|Price|Discount)\b",
    r"\bPrime Member (?:Deal|Price|Discount)\b",
    r"\bBlack Friday (?:Deal|Sale)\b",
    r"\bCyber Monday (?:Deal|Sale)\b",
    r"\b(?:Fall|Autumn) (?:Deal|Sale|Promotion)\b",
    r"\bBig Spring Sale\b",
    r"\bHoliday (?:Deal|Sale)\b",
    r"\bLimited time deal\b",
    r"\bLightning Deal\b",
    r"\bDeal of the Day\b",
)

PROMOTION_SELECTORS = (
    "#dealBadgeSupportingText",
    "#dealBadgeText",
    "#lightning-deal-title",
    "#dealprice_savings",
    ".dealBadge",
    ".dealBadgeTextColor",
    "#dealBadge_feature_div",
    "[data-feature-name='dealBadge']",
    "#primeSavingsUpsellCaption_feature_div",
    "#primeExclusivePricing_feature_div",
    "#primeExclusivePrice_feature_div",
    "[data-feature-name='primeSavingsUpsell']",
)

BUSINESS_PRICE_SELECTORS = (
    "#businessPrice_feature_div",
    "#business-price",
    "#businessPricing_feature_div",
    "#quantityPricingTable",
    "[data-feature-name='businessPrice']",
    "[data-feature-name='businessPricing']",
)

STAR_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def _normalize_image_url(url: str | None) -> str | None:
    if not url or "media-amazon.com/images/I/" not in url:
        return None
    parts = urlsplit(url)
    path = re.sub(r"\._[^/]+_(?=\.(?:jpe?g|png|webp)$)", "", parts.path, flags=re.I)
    return urlunsplit((parts.scheme or "https", parts.netloc, path, "", ""))


def _parse_first_available(soup: BeautifulSoup, page_text: str) -> tuple[str | None, str | None]:
    raw = None
    for row in soup.select("#productDetails_detailBullets_sections1 tr, #productDetails_techSpec_section_1 tr"):
        label = clean_text((row.select_one("th") or row.select_one("td")).get_text(" ", strip=True)) if row.select_one("th,td") else ""
        if label and "date first available" in label.lower():
            cells = row.select("td")
            raw = clean_text(cells[-1].get_text(" ", strip=True)) if cells else None
            break
    if not raw:
        for item in soup.select("#detailBullets_feature_div li, #detailBulletsWrapper_feature_div li"):
            label = item.select_one(".a-text-bold")
            label_text = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", label.get_text(" ", strip=True) if label else "")
            if "date first available" not in label_text.lower():
                continue
            spans = item.select("span")
            values = [clean_text(span.get_text(" ", strip=True)) for span in spans if "a-text-bold" not in (span.get("class") or [])]
            raw = next((value for value in reversed(values) if value and re.search(r"[A-Z][a-z]+\s+\d{1,2},\s+\d{4}", value)), None)
            if raw:
                break
    if not raw:
        match = re.search(r"Date First Available[\s\u200e\u200f\u202a-\u202e\u2066-\u2069]*:?[\s\u200e\u200f\u202a-\u202e\u2066-\u2069]*([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", page_text, re.I)
        raw = clean_text(match.group(1)) if match else None
    if not raw:
        return None, None
    try:
        return datetime.strptime(raw, "%B %d, %Y").date().isoformat(), raw
    except ValueError:
        return None, raw


def _parse_high_return_rate(soup: BeautifulSoup, page_text: str) -> tuple[int, str | None]:
    nodes = soup.select(
        "#productAlert_feature_div, #frequentlyReturnedItem_feature_div, "
        "#highReturnRate_feature_div, [data-feature-name='productAlert']"
    )
    targeted = " ".join(
        value for value in (clean_text(node.get_text(" ", strip=True)) for node in nodes) if value
    )
    for source, patterns in (
        (targeted, HIGH_RETURN_PATTERNS),
        (page_text, (HIGH_RETURN_PATTERNS[0], HIGH_RETURN_PATTERNS[2])),
    ):
        for pattern in patterns:
            match = re.search(pattern, source, re.I)
            if match:
                return 1, clean_text(match.group(0))
    return 0, None


def _unique_text(values: list[str]) -> str | None:
    unique = list(dict.fromkeys(value for value in values if value))
    return "；".join(unique) or None


def _parse_deal_text(soup: BeautifulSoup) -> str | None:
    """Extract stable, visible promotion labels without scanning page scripts.

    Amazon's deal container can include a live countdown and placeholder copy.
    Keeping only known campaign labels prevents a new snapshot on every tick.
    """
    labels: list[str] = []
    fallback_selectors = {
        "#dealBadgeSupportingText", "#dealBadgeText", "#lightning-deal-title",
        "#dealprice_savings", ".dealBadge", ".dealBadgeTextColor",
    }
    for selector in PROMOTION_SELECTORS:
        for node in soup.select(selector):
            text = clean_text(node.get_text(" ", strip=True)) or ""
            matched_labels: list[str] = []
            for pattern in PROMOTION_PATTERNS:
                matched_labels.extend(clean_text(match.group(0)) or "" for match in re.finditer(pattern, text, re.I))
            labels.extend(matched_labels)
            if not matched_labels and selector in fallback_selectors and text and len(text) <= 100:
                if not re.search(r"\b(?:ends? in|hours?|minutes?|seconds?)\b", text, re.I):
                    labels.append(text)
    return _unique_text(labels)


def _parse_business_price(soup: BeautifulSoup) -> str | None:
    values: list[str] = []
    for selector in BUSINESS_PRICE_SELECTORS:
        for node in soup.select(selector):
            text = clean_text(node.get_text(" ", strip=True)) or ""
            if not text:
                continue
            price_node = node.select_one(".a-price .a-offscreen, .a-offscreen")
            price_text = clean_text(price_node.get_text(" ", strip=True)) if price_node else None
            if price_text and re.search(r"\b(?:business|quantity|volume)\b", text, re.I):
                values.append(f"Business Price: {price_text}")
                continue
            match = re.search(
                r"\bBusiness Price\b\s*:?\s*(\$\s*[\d,]+(?:\.\d{1,2})?)",
                text, re.I,
            )
            if match:
                values.append(f"Business Price: {clean_text(match.group(1))}")
            elif len(text) <= 240:
                values.append(text)
    return _unique_text(values)


def _parse_rating_breakdown(soup: BeautifulSoup) -> dict[str, int | float]:
    result: dict[int, int | float] = {}
    nodes = soup.select("#histogramTable a[aria-label], [data-hook='rating-histogram'] a[aria-label]")
    for node in nodes:
        label = clean_text(node.get("aria-label")) or ""
        match = re.search(
            r"([\d.]+)\s*(?:percent|%)\s+of reviews (?:have|are)\s+([1-5])\s+stars?",
            label, re.I,
        )
        if not match:
            match = re.search(r"([1-5])\s+stars?.*?([\d.]+)\s*(?:percent|%)", label, re.I)
            if match:
                star, percentage = int(match.group(1)), float(match.group(2))
            else:
                continue
        else:
            percentage, star = float(match.group(1)), int(match.group(2))
        result[star] = int(percentage) if percentage.is_integer() else percentage

    if len(result) < 5:
        for row in soup.select("#histogramTable li"):
            marker = " ".join(filter(None, [
                row.get("data-csa-c-content-id"),
                (row.select_one("[data-csa-c-content-id]") or {}).get("data-csa-c-content-id"),
                (row.select_one("a") or {}).get("href"),
            ])).lower()
            star = next((number for word, number in STAR_WORDS.items() if f"{word}star" in marker or f"{word}_star" in marker), None)
            progress = row.select_one("[role='progressbar'][aria-valuenow]")
            if star and progress:
                try:
                    percentage = float(progress.get("aria-valuenow"))
                except (TypeError, ValueError):
                    continue
                result[star] = int(percentage) if percentage.is_integer() else percentage
    return {str(star): result[star] for star in range(5, 0, -1) if star in result}


def _parse_listing_status(
    soup: BeautifulSoup, page_text: str, product_title: str | None,
) -> tuple[str, str | None]:
    title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    text = f"{title} {page_text}"
    for pattern in DOG_PAGE_PATTERNS:
        match = re.search(pattern, text, re.I)
        if match:
            return "dog", clean_text(match.group(0))
    status_nodes = soup.select(
        "#availability, #outOfStock, #availabilityInsideBuyBox_feature_div, #buybox"
    )
    status_text = " ".join(
        value for value in (clean_text(node.get_text(" ", strip=True)) for node in status_nodes) if value
    )
    removal_sources = (status_text, text) if not product_title else (status_text,)
    for source in removal_sources:
        for pattern in REMOVED_PAGE_PATTERNS:
            match = re.search(pattern, source, re.I)
            if match:
                return "removed", clean_text(match.group(0))
    return "active", None


def parse_product(html: str, asin: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = first_text(soup, ["#productTitle", "#title", "h1.a-size-large"])
    brand = first_text(soup, ["#bylineInfo", "tr.po-brand td.a-span9", "#productOverview_feature_div tr.a-spacing-small:first-child td:last-child"])
    if brand:
        brand = re.sub(r"^Visit\s+the\s+", "", brand, flags=re.I)
        brand = re.sub(r"\s+Store$", "", brand, flags=re.I)
        brand = re.sub(r"^Brand:\s*", "", brand, flags=re.I).strip()
    price_text = first_text(soup, [
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#priceblock_dealprice", "#priceblock_ourprice", ".reinventPricePriceToPayMargin .a-offscreen",
    ])
    list_price_text = first_text(soup, [
        "#corePrice_feature_div .a-price[data-a-strike='true'] .a-offscreen",
        "#corePrice_feature_div .a-text-price[data-a-strike='true'] .a-offscreen",
        "#listPrice", "#priceblock_listprice",
    ])
    coupon = first_text(soup, ["#couponText", "#promoPriceBlockMessage_feature_div", "label[id*='coupon']", ".couponBadge"])
    deal = _parse_deal_text(soup)
    business_price = _parse_business_price(soup)
    rating_text = first_text(soup, ["#acrPopover", "span[data-hook='rating-out-of-text']", "#averageCustomerReviews .a-icon-alt"])
    rating_count_text = first_text(soup, ["#acrCustomerReviewText", "span[data-hook='total-review-count']"])
    rating_breakdown = _parse_rating_breakdown(soup)
    availability = first_text(soup, ["#availability", "#outOfStock", "#availabilityInsideBuyBox_feature_div"])
    seller = first_text(soup, ["#sellerProfileTriggerId", "#merchant-info a", "#tabular-buybox-truncate-1 .a-truncate-full"])
    ships_from = first_text(soup, ["#tabular-buybox-truncate-0 .a-truncate-full", "#fulfillerInfoFeature_feature_div"])
    offer_count_text = first_text(soup, ["#olpLinkWidget_feature_div", "#buybox-see-all-buying-choices"])
    main_image = soup.select_one("#landingImage, #imgBlkFront")
    raw_image_url = (main_image.get("data-old-hires") or main_image.get("src")) if main_image else None
    image_url = _normalize_image_url(raw_image_url) or raw_image_url
    image_nodes = soup.select("#altImages img")
    product_images: list[str] = []
    candidates = []
    if main_image:
        candidates.extend([main_image.get("data-old-hires"), main_image.get("src")])
        dynamic = main_image.get("data-a-dynamic-image")
        if dynamic:
            try:
                candidates.extend(json.loads(dynamic).keys())
            except (json.JSONDecodeError, AttributeError):
                pass
    for node in image_nodes:
        candidates.extend([node.get("data-old-hires"), node.get("src")])
    for candidate in candidates:
        normalized = _normalize_image_url(candidate)
        if normalized and normalized not in product_images:
            product_images.append(normalized)

    about_items: list[str] = []
    for node in soup.select("#feature-bullets li span.a-list-item, #productFactsDesktop_feature_div li span.a-list-item"):
        value = clean_text(node.get_text(" ", strip=True))
        if value and value.lower() not in {"about this item", "see more"} and value not in about_items:
            about_items.append(value)
    highlights = first_text(soup, [
        "#title_feature_div .a-color-secondary",
        "#titleSection .a-color-secondary",
        "#productSubtitle",
    ])

    page_text = clean_text(soup.get_text(" ", strip=True)) or ""
    high_return_rate, high_return_rate_text = _parse_high_return_rate(soup, page_text)
    listing_status, listing_status_detail = _parse_listing_status(soup, page_text, title)
    first_available, first_available_text = _parse_first_available(soup, page_text)
    ranks: list[dict] = []
    for match in re.finditer(r"#([\d,]+)\s+in\s+([^#\(]+?)(?=\s*\(|\s*#|$)", page_text):
        rank = int(match.group(1).replace(",", ""))
        name = clean_text(re.split(r"\s+(?:ASIN\s+[A-Z0-9]{10}|Customer Reviews:?|Date First Available)\b", match.group(2), maxsplit=1, flags=re.I)[0])
        if name and not any(item["rank"] == rank and item["category"] == name for item in ranks):
            ranks.append({"rank": rank, "category": name})
    main_bsr = ranks[0]["rank"] if ranks else None

    variation_asins: set[str] = set()
    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if "dimensionValuesDisplayData" in text or "variationValues" in text:
            variation_asins.update(re.findall(r'"(B0[A-Z0-9]{8})"', text))

    badges_text = " ".join(node.get_text(" ", strip=True) for node in soup.select("#acBadge_feature_div, .ac-badge-wrapper, .zg-badge-text, #zeitgeistBadge_feature_div"))
    amazon_choice = "amazon's choice" in badges_text.lower() or "amazons choice" in badges_text.lower()
    best_seller = "best seller" in badges_text.lower()
    new_release = "new release" in badges_text.lower()
    confidence = (
        "high" if listing_status != "active"
        else "high" if title and (price_text or availability)
        else "medium" if title else "low"
    )

    return {
        "asin": asin, "title": title, "brand": brand, "current_price": parse_price(price_text),
        "list_price": parse_price(list_price_text), "coupon_text": coupon, "deal_text": deal,
        "business_price_text": business_price,
        "rating_count": parse_int(rating_count_text), "rating_value": parse_rating(rating_text),
        "rating_breakdown_json": rating_breakdown,
        "main_bsr": main_bsr, "category_ranks_json": ranks, "amazon_choice": int(amazon_choice),
        "best_seller": int(best_seller), "new_release": int(new_release),
        "high_return_rate": high_return_rate, "high_return_rate_text": high_return_rate_text,
        "listing_status": listing_status, "listing_status_detail": listing_status_detail,
        "availability": availability,
        "delivery_text": None, "featured_seller": seller, "ships_from": ships_from,
        "offer_count": parse_int(offer_count_text), "variation_count": len(variation_asins) or None,
        "main_image_url": image_url, "title_hash": fingerprint(title), "image_hash": fingerprint(image_url),
        "highlights_json": None, "highlights_text": highlights, "highlights_hash": fingerprint(highlights),
        "about_items_json": about_items,
        "about_items_hash": fingerprint(json.dumps(about_items, ensure_ascii=False)),
        "product_images_json": product_images,
        "product_images_hash": fingerprint(json.dumps(product_images, ensure_ascii=False)),
        "product_image_count": len(product_images),
        "date_first_available": first_available, "date_first_available_text": first_available_text,
        "source_url": url, "success": int(bool(title) and listing_status == "active"), "confidence": confidence,
        "raw_json": json.dumps({
            "price_text": price_text, "business_price_text": business_price,
            "rating_text": rating_text, "rating_breakdown": rating_breakdown,
            "badges_text": badges_text,
            "high_return_rate_text": high_return_rate_text,
            "listing_status_detail": listing_status_detail,
        }, ensure_ascii=False),
    }
