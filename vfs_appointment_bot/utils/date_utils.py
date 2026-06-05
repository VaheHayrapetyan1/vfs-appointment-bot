"""
Date extraction from free-form VFS UI copy.

VFS shows availability in many shapes (hyphens, slashes, spelled months).
We normalize detected calendar days to ISO YYYY-MM-DD for stable notifications.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import List, Set, Tuple

_MONTH_WORDS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

_MONTH_PATTERN = (
    r"January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)


def _to_iso(y: int, m: int, d: int) -> str:
    return f"{y:04d}-{m:02d}-{d:02d}"


def _append_unique(bucket: List[str], seen: Set[Tuple[int, int, int]], y: int, m: int, d: int) -> None:
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return
    key = (y, m, d)
    if key in seen:
        return
    try:
        datetime(y, m, d)
    except ValueError:
        return
    seen.add(key)
    bucket.append(_to_iso(y, m, d))


def _month_num(word: str) -> int | None:
    return _MONTH_WORDS.get(word.strip().lower())


def extract_all_dates_normalized(text: str) -> List[str]:
    """
    Every calendar date found in ``text``, normalized to ``YYYY-MM-DD``,
    unique, first occurrence order.
    """
    if not text or not str(text).strip():
        return []

    out: List[str] = []
    seen: Set[Tuple[int, int, int]] = set()
    s = str(text)

    # ISO: YYYY-MM-DD
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", s):
        _append_unique(out, seen, int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # DD-MM-YYYY (European / AM-LT style)
    for m in re.finditer(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b", s):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        _append_unique(out, seen, y, mo, d)

    # DD/MM/YYYY — VFS often uses slashes (India-CH screenshot style). Assume DD/MM.
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", s):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        _append_unique(out, seen, y, mo, d)

    # YYYY/MM/DD
    for m in re.finditer(r"\b(\d{4})/(\d{2})/(\d{2})\b", s):
        _append_unique(out, seen, int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # DD-MM-YY — assume DD-MM-YY, pivot at 70 -> 20yy vs 19yy
    for m in re.finditer(r"\b(\d{1,2})-(\d{1,2})-(\d{2})\b(?!\d)", s):
        d, mo, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = 2000 + yy if yy < 70 else 1900 + yy
        _append_unique(out, seen, y, mo, d)

    # "May 5, 2026" / "May 05 2026"
    for m in re.finditer(
        rf"\b({_MONTH_PATTERN})\s+(\d{{1,2}}),?\s+(\d{{4}})\b",
        s,
        re.IGNORECASE,
    ):
        mon = _month_num(m.group(1))
        if mon is None:
            continue
        d, y = int(m.group(2)), int(m.group(3))
        _append_unique(out, seen, y, mon, d)

    # "5 May 2026" / "05 May 2026"
    for m in re.finditer(
        rf"\b(\d{{1,2}})\s+({_MONTH_PATTERN})\s+(\d{{4}})\b",
        s,
        re.IGNORECASE,
    ):
        mon = _month_num(m.group(2))
        if mon is None:
            continue
        d, y = int(m.group(1)), int(m.group(3))
        _append_unique(out, seen, y, mon, d)

    return out


def extract_date_from_string(text):
    """Backward-compatible: first ISO date found, or None."""
    dates = extract_all_dates_normalized(text or "")
    return dates[0] if dates else None
