from __future__ import annotations

import unittest
from decimal import Decimal

from moneybird_mcp.config import MoneybirdError
from moneybird_mcp.invoicing import parse_decimal_number


class DecimalInputValidationTests(unittest.TestCase):
    def test_accepts_only_complete_plain_decimal_values(self) -> None:
        accepted = {
            "121": Decimal("121"),
            "-121.50": Decimal("-121.50"),
            "+0,25": Decimal("0.25"),
        }
        for raw, expected in accepted.items():
            with self.subTest(raw=raw):
                self.assertEqual(
                    parse_decimal_number(raw, label="amount"),
                    expected,
                )

    def test_rejects_trailing_junk_and_ambiguous_separators(self) -> None:
        invalid = [
            "121.00 EUR",
            "1.2.3",
            "1,2,3",
            "1,234.56",
            "1 234,56",
            "NaN",
            "Infinity",
            "-Infinity",
            "",
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(MoneybirdError, "Invalid amount"):
                    parse_decimal_number(raw, label="amount")


if __name__ == "__main__":
    unittest.main()
