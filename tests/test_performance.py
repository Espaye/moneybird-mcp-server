from __future__ import annotations

import unittest

from moneybird.task_context import MoneybirdTaskContext
from moneybird.telemetry import (
    begin_tool_trace,
    clear_performance_metrics,
    end_tool_trace,
    normalize_endpoint,
    performance_snapshot,
    record_api_call,
    record_tool_call,
    set_current_tenant_scope,
)


class TaskContextTests(unittest.TestCase):
    class FakeClient:
        def __init__(self) -> None:
            self.batch_calls: list[list[str]] = []
            self.single_calls: list[str] = []

        def fetch_financial_mutations_by_ids(self, ids):
            self.batch_calls.append(list(ids))
            return [{"id": item_id, "version": 1} for item_id in ids]

        def get_financial_mutation(self, mutation_id):
            self.single_calls.append(mutation_id)
            return {"id": mutation_id, "version": 1}

    def test_financial_mutations_are_batched_and_cached(self) -> None:
        fake = self.FakeClient()
        task = MoneybirdTaskContext(fake)
        ids = [str(index) for index in range(205)]

        first = task.financial_mutations(ids)
        second = task.financial_mutations(ids)

        self.assertEqual(len(first), 205)
        self.assertEqual(len(second), 205)
        self.assertEqual([len(group) for group in fake.batch_calls], [100, 100, 5])
        self.assertEqual(fake.single_calls, [])

    def test_refresh_uses_batch_endpoint_again(self) -> None:
        fake = self.FakeClient()
        task = MoneybirdTaskContext(fake)
        task.financial_mutations(["1", "2"])
        task.financial_mutations(["1", "2"], refresh=True)
        self.assertEqual(fake.batch_calls, [["1", "2"], ["1", "2"]])


class TelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_performance_metrics()

    def tearDown(self) -> None:
        clear_performance_metrics()

    def test_endpoint_normalization_removes_record_ids(self) -> None:
        self.assertEqual(
            normalize_endpoint(
                "/463484440785454158/financial_mutations/"
                "466090469904877383/link_booking.json"
            ),
            "/:id/financial_mutations/:id/link_booking.json",
        )
        self.assertEqual(
            normalize_endpoint(
                "/463484440785454158/financial_mutations/"
                "466090469904877383.json"
            ),
            "/:id/financial_mutations/:id.json",
        )
        self.assertEqual(
            normalize_endpoint("/42/contacts/7.json"),
            "/:id/contacts/:id.json",
        )

    def test_tool_metric_includes_grouped_api_call_count(self) -> None:
        trace_id, trace_token, tool_token = begin_tool_trace("demo_tool")
        try:
            record_api_call(
                method="GET",
                path="/123456/ledger_accounts.json",
                status=200,
                duration_seconds=0.125,
                retry=0,
            )
            record_tool_call(
                trace_id=trace_id,
                tool_name="demo_tool",
                status="success",
                duration_seconds=0.2,
            )
        finally:
            end_tool_trace(trace_token, tool_token)

        snapshot = performance_snapshot(recent_tools=5)
        self.assertEqual(snapshot["api"]["retained_calls"], 1)
        self.assertEqual(snapshot["recent_tools"][0]["api_calls"], 1)
        self.assertEqual(
            snapshot["api"]["top_endpoints"][0]["endpoint"],
            "GET /:id/ledger_accounts.json",
        )

    def test_snapshots_are_isolated_by_tenant_scope(self) -> None:
        def record(scope: str, tool_name: str) -> None:
            trace_id, trace_token, tool_token = begin_tool_trace(tool_name)
            try:
                set_current_tenant_scope(scope)
                record_api_call(
                    method="GET",
                    path="/123456/ledger_accounts.json",
                    status=200,
                    duration_seconds=0.01,
                    retry=0,
                )
                record_tool_call(
                    trace_id=trace_id,
                    tool_name=tool_name,
                    status="success",
                    duration_seconds=0.02,
                )
            finally:
                end_tool_trace(trace_token, tool_token)

        record("tenant-a", "tenant_a_tool")
        record("tenant-b", "tenant_b_tool")

        tenant_a = performance_snapshot(
            recent_tools=10,
            tenant_scope="tenant-a",
        )
        tenant_b = performance_snapshot(
            recent_tools=10,
            tenant_scope="tenant-b",
        )
        self.assertEqual(tenant_a["api"]["retained_calls"], 1)
        self.assertEqual(tenant_b["api"]["retained_calls"], 1)
        self.assertEqual(
            [item["tool_name"] for item in tenant_a["recent_tools"]],
            ["tenant_a_tool"],
        )
        self.assertEqual(
            [item["tool_name"] for item in tenant_b["recent_tools"]],
            ["tenant_b_tool"],
        )
        self.assertNotIn("tenant_scope", tenant_a["recent_tools"][0])


if __name__ == "__main__":
    unittest.main()
