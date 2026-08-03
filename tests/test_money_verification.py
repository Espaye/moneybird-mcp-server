from __future__ import annotations

import unittest

from moneybird_mcp.tools.sales_batches import _money_values_equal


class MoneyVerificationTests(unittest.TestCase):
    def test_equivalent_decimal_scales_match(self) -> None:
        self.assertTrue(_money_values_equal("121.0", "121.00"))
        self.assertTrue(_money_values_equal("121.000", "121.00"))

    def test_one_cent_difference_does_not_match(self) -> None:
        self.assertFalse(_money_values_equal("121.00", "121.01"))

    def test_invalid_or_nonfinite_value_fails_closed(self) -> None:
        for value in (None, "", "not-money", "NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                self.assertFalse(_money_values_equal(value, "1.00"))
