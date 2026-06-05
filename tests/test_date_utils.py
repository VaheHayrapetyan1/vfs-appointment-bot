"""Unit tests for VFS date parsing (multiple visible formats -> ISO)."""

import unittest

from vfs_appointment_bot.utils.date_utils import (
    extract_all_dates_normalized,
    extract_date_from_string,
)


class TestExtractDates(unittest.TestCase):
    def test_hyphen_dd_mm_yyyy_multiple(self) -> None:
        s = "Earliest available slot for 3 Applicants is : 20-05-2026, 21-05-2026"
        self.assertEqual(
            extract_all_dates_normalized(s),
            ["2026-05-20", "2026-05-21"],
        )

    def test_slash_dd_mm_yyyy(self) -> None:
        self.assertEqual(
            extract_all_dates_normalized("Earliest Available Slot : 28/11/2022"),
            ["2022-11-28"],
        )

    def test_month_name(self) -> None:
        self.assertEqual(
            extract_all_dates_normalized("Slot: 5 May 2026"),
            ["2026-05-05"],
        )
        self.assertEqual(
            extract_all_dates_normalized("Slot: May 05, 2026"),
            ["2026-05-05"],
        )

    def test_extract_date_from_string_first_only(self) -> None:
        self.assertEqual(extract_date_from_string("on 10-06-2026 and later"), "2026-06-10")


if __name__ == "__main__":
    unittest.main()
