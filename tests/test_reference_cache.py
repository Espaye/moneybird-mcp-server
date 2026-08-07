"""Reference/membership caching: correct keying, honest expiry, safe modes."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault(
    "MONEYBIRD_MCP_DATA_DIR",
    tempfile.mkdtemp(prefix="moneybird_mcp_test_state_"),
)

from moneybird_mcp import rate_budget, reference_cache
from moneybird_mcp.config import MoneybirdError
from moneybird_mcp.credentials import CREDENTIAL_MODE_HOSTED_REQUEST_ONLY


class Counter:
    def __init__(self, value="v"):
        self.calls = 0
        self.value = value

    def __call__(self):
        self.calls += 1
        return self.value


class ReferenceCacheTests(unittest.TestCase):
    def setUp(self):
        reference_cache.clear()
        self.addCleanup(reference_cache.clear)

    def read(self, loader, *, token="tok", admin="1", resource="ledger_accounts", ttl=60.0):
        return reference_cache.cached_read(
            token=token,
            administration_id=admin,
            resource=resource,
            ttl_seconds=ttl,
            loader=loader,
        )

    def test_second_read_is_served_from_cache(self):
        loader = Counter()
        self.assertEqual(self.read(loader), "v")
        self.assertEqual(self.read(loader), "v")
        self.assertEqual(loader.calls, 1)

    def test_a_different_token_never_shares_an_entry(self):
        first, second = Counter("a"), Counter("b")
        self.assertEqual(self.read(first, token="tok-a"), "a")
        self.assertEqual(self.read(second, token="tok-b"), "b")
        self.assertEqual((first.calls, second.calls), (1, 1))

    def test_a_different_administration_never_shares_an_entry(self):
        first, second = Counter("a"), Counter("b")
        self.assertEqual(self.read(first, admin="1"), "a")
        self.assertEqual(self.read(second, admin="2"), "b")
        self.assertEqual((first.calls, second.calls), (1, 1))

    def test_a_different_resource_never_shares_an_entry(self):
        first, second = Counter("a"), Counter("b")
        self.assertEqual(self.read(first, resource="tax_rates"), "a")
        self.assertEqual(self.read(second, resource="ledger_accounts"), "b")
        self.assertEqual((first.calls, second.calls), (1, 1))

    def test_zero_ttl_disables_caching(self):
        loader = Counter()
        self.read(loader, ttl=0)
        self.read(loader, ttl=0)
        self.assertEqual(loader.calls, 2)

    def test_a_failed_load_is_never_cached(self):
        calls = []

        def failing():
            calls.append(1)
            raise MoneybirdError("nope")

        for _ in range(2):
            with self.assertRaises(MoneybirdError):
                self.read(failing)
        self.assertEqual(len(calls), 2)

    def test_expiry_reloads(self):
        loader = Counter()
        self.read(loader)
        clock = [1_000_000.0]
        with mock.patch.object(
            reference_cache.time, "monotonic", lambda: clock[0] + 10_000
        ):
            self.read(loader)
        self.assertEqual(loader.calls, 2)

    def test_invalidate_administration_drops_only_that_administration(self):
        first, second = Counter("a"), Counter("b")
        self.read(first, admin="1")
        self.read(second, admin="2")
        reference_cache.invalidate_administration("1")
        self.read(first, admin="1")
        self.read(second, admin="2")
        self.assertEqual((first.calls, second.calls), (2, 1))

    def test_hosted_request_mode_never_caches(self):
        loader = Counter()
        with mock.patch.object(
            reference_cache,
            "caching_enabled",
            return_value=False,
        ):
            self.read(loader)
            self.read(loader)
        self.assertEqual(loader.calls, 2)

    def test_caching_enabled_is_false_in_hosted_request_mode(self):
        with mock.patch(
            "moneybird_mcp.credentials.get_credential_mode",
            return_value=CREDENTIAL_MODE_HOSTED_REQUEST_ONLY,
        ):
            self.assertFalse(reference_cache.caching_enabled())

    def test_entries_are_bounded(self):
        for index in range(reference_cache.MAX_ENTRIES + 20):
            self.read(Counter(), token=f"tok-{index}")
        self.assertLessEqual(
            reference_cache.cache_stats()["entries"],
            reference_cache.MAX_ENTRIES,
        )

    def test_stats_expose_no_cached_values(self):
        self.read(Counter("secret-payload"))
        self.assertNotIn("secret-payload", str(reference_cache.cache_stats()))


class RateBudgetTests(unittest.TestCase):
    def setUp(self):
        rate_budget.clear()
        self.addCleanup(rate_budget.clear)

    def test_reports_have_their_own_bucket(self):
        self.assertEqual(
            rate_budget.bucket_for_operation("123/reports/profit_loss"), "reports"
        )
        self.assertEqual(rate_budget.bucket_for_operation("123/contacts"), "general")

    def test_documented_limits_match_moneybird(self):
        self.assertEqual(rate_budget.DOCUMENTED_LIMITS["general"]["requests"], 150)
        self.assertEqual(rate_budget.DOCUMENTED_LIMITS["reports"]["requests"], 50)

    def test_headers_are_recorded_per_bucket(self):
        rate_budget.record_response_headers(
            "123/contacts",
            {"RateLimit-Remaining": "42", "RateLimit-Limit": "150",
             "RateLimit-Reset": "120"},
        )
        self.assertEqual(rate_budget.remaining("general"), 42)
        self.assertIsNone(rate_budget.remaining("reports"))

    def test_missing_headers_are_ignored(self):
        rate_budget.record_response_headers("123/contacts", {})
        self.assertIsNone(rate_budget.remaining("general"))

    def test_live_moneybird_header_shape_is_read_correctly(self):
        """The exact headers observed on 2026-08-07.

        RateLimit-Remaining is seconds-until-reset there, not a request count:
        it equals reset-minus-now and exceeds RateLimit-Limit. The real count is
        in the undocumented RateLimit-RequestsRemaining.
        """
        now = int(time.time())
        rate_budget.record_response_headers(
            "123456789012345678/tax_rates",
            {
                "RateLimit-Limit": "150",
                "RateLimit-Remaining": "160",
                "RateLimit-Reset": str(now + 160),
                "RateLimit-RequestsRemaining": "143",
            },
        )
        self.assertEqual(rate_budget.remaining("general"), 143)
        left = rate_budget.reset_seconds("general")
        self.assertIsNotNone(left)
        self.assertLessEqual(left, 161)

    def test_a_seconds_valued_remaining_is_not_mistaken_for_a_count(self):
        # Without the plausibility check this would report 160 requests left and
        # let a scan burn the real 143-request budget.
        rate_budget.record_response_headers(
            "123/contacts",
            {"RateLimit-Limit": "150", "RateLimit-Remaining": "160"},
        )
        self.assertIsNone(rate_budget.remaining("general"))

    def test_absolute_epoch_reset_is_converted_to_a_delay(self):
        rate_budget.record_response_headers(
            "123/contacts",
            {"RateLimit-Reset": str(int(time.time()) + 90)},
        )
        left = rate_budget.reset_seconds("general")
        self.assertIsNotNone(left)
        self.assertTrue(80 <= left <= 95, left)

    def test_nonsensical_reset_is_discarded(self):
        rate_budget.record_response_headers(
            "123/contacts", {"RateLimit-Reset": "1"}
        )
        rate_budget.clear()
        rate_budget.record_response_headers(
            "123/contacts", {"RateLimit-Reset": "-5"}
        )
        self.assertIsNone(rate_budget.reset_seconds("general"))

    def test_unknown_budget_never_blocks_a_caller(self):
        self.assertIsNone(rate_budget.affordable_batches("general"))

    def test_affordable_batches_keeps_a_reserve(self):
        rate_budget.record_response_headers(
            "123/contacts",
            {"RateLimit-Remaining": "12", "RateLimit-Reset": "60"},
        )
        self.assertEqual(rate_budget.affordable_batches("general", reserve=10), 2)
        self.assertEqual(rate_budget.affordable_batches("general", reserve=20), 0)

    def test_snapshot_carries_no_paths_or_tokens(self):
        rate_budget.record_response_headers(
            "123456789/reports/tax",
            {"RateLimit-Remaining": "7", "RateLimit-Reset": "30"},
        )
        rendered = str(rate_budget.snapshot())
        self.assertNotIn("123456789", rendered)
        self.assertIn("reports", rendered)


if __name__ == "__main__":
    unittest.main()
