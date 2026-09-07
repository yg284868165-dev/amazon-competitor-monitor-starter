from __future__ import annotations

import hashlib
import re
from typing import Iterable

from bs4 import BeautifulSoup, Tag


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def first_text(root: BeautifulSoup | Tag, selectors: Iterable[str]) -> str | None:
    for selector in selectors:
        node = root.select_one(selector)
        if node:
            value = clean_text(node.get_text(" ", strip=True))
            if value:
                return value
    return None


def parse_price(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"(?:US\$|\$)\s*([\d,]+(?:\.\d{1,2})?)", value)
    return float(match.group(1).replace(",", "")) if match else None


def parse_int(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"([\d,]+)", value)
    return int(match.group(1).replace(",", "")) if match else None


def parse_rating(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"([0-5](?:\.\d+)?)\s*(?:out of|/)", value, re.I)
    return float(match.group(1)) if match else None


def fingerprint(value: str | None) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None
