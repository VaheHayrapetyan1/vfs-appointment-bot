"""
Structured result of scraping VFS availability banners (e.g. div.alert).

Keeps normalized dates for logic and verbatim excerpts for notifications when
wording or formats shift between VFS deployments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

_DEFAULT_EXCERPT = 2000


@dataclass(frozen=True)
class AppointmentScanResult:
    """ISO dates (YYYY-MM-DD) plus optional raw VFS alert lines."""

    dates_iso: Tuple[str, ...]
    alert_excerpts: Tuple[str, ...] = ()

    @property
    def has_dates(self) -> bool:
        return len(self.dates_iso) > 0

    @staticmethod
    def empty() -> "AppointmentScanResult":
        return AppointmentScanResult((), ())


def truncate_excerpt(text: str, max_len: int = _DEFAULT_EXCERPT) -> str:
    """Collapse whitespace and cap length so emails/Telegram stay reasonable."""
    if not text:
        return ""
    single = " ".join(text.split())
    if len(single) <= max_len:
        return single
    return single[: max_len - 3] + "..."
