from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from moneybird_mcp import safety
from moneybird_mcp.config import MoneybirdError
from moneybird_mcp.credentials import set_active_administration_id
from moneybird_mcp.tools._writes import (
    mark_write_dispatch_started,
    mark_write_verifying,
    run_approved_write,
    stage_write,
)
from moneybird_mcp.tools.purchases import _execute_reconcile


def _verified_executor_result(
    *,
    audit_result: str = "success",
    status: str = "done",
) -> dict[str, str]:
    mark_write_dispatch_started()
    mark_write_verifying()
    return {"_audit_result": audit_result, "_status": status}


def _process_pop_approval(
    state_dir: str,
    approval_id: str,
    start_event,
    result_queue,
) -> None:
    """Spawn-safe worker used by the cross-process claim regression."""
    os.environ["MONEYBIRD_MCP_DATA_DIR"] = state_dir
    start_event.wait(10)
    try:
        safety.pop_approval(
            approval_id,
            "process_demo",
            administration_id="safety-admin",
        )
    except Exception as exc:  # pragma: no cover - asserted through the queue
        result_queue.put(("error", type(exc).__name__, str(exc)))
    else:
        result_queue.put(("claimed",))


class SafetyKernelTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="moneybird_safety_kernel_")
        self._env = mock.patch.dict(
            os.environ,
            {
                "MONEYBIRD_MCP_DATA_DIR": self._temp_dir.name,
                "MONEYBIRD_CAPABILITY_MODE": "write_enabled",
            },
        )
        self._env.start()
        set_active_administration_id("safety-admin")

    def tearDown(self) -> None:
        set_active_administration_id(None)
        self._env.stop()
        self._temp_dir.cleanup()

    @staticmethod
    def _approval_state(approval_id: str) -> tuple[str, str | None]:
        with safety._approvals_connection() as connection:
            row = connection.execute(
                "SELECT state, outcome FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        if row is None:
            raise AssertionError("approval row was deleted")
        return str(row[0]), row[1]

    def test_legacy_approval_table_migrates_without_deleting_pending_row(self) -> None:
        path = Path(self._temp_dir.name) / safety.APPROVALS_DB_BASENAME
        with sqlite3.connect(path) as connection:
            connection.execute(
                """
                CREATE TABLE approvals (
                    approval_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    administration_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy-id",
                    "legacy_demo",
                    json.dumps({"fingerprint": "legacy-fingerprint", "value": 1}),
                    "legacy",
                    "safety-admin",
                    "2026-07-30T00:00:00+00:00",
                    "2099-07-30T00:00:00+00:00",
                ),
            )
        connection.close()

        claimed = safety.pop_approval(
            "legacy-id",
            "legacy_demo",
            administration_id="safety-admin",
        )

        self.assertEqual(claimed["payload"]["value"], 1)
        self.assertEqual(self._approval_state("legacy-id"), ("claimed", None))
        with safety._approvals_connection() as connection:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(approvals)").fetchall()
            }
            fingerprint = connection.execute(
                "SELECT fingerprint FROM approvals WHERE approval_id = 'legacy-id'"
            ).fetchone()[0]
        self.assertIn("state", columns)
        self.assertIn("outcome", columns)
        self.assertEqual(fingerprint, "legacy-fingerprint")

    def test_newer_approval_schema_is_never_downgraded(self) -> None:
        path = Path(self._temp_dir.name) / safety.APPROVALS_DB_BASENAME
        future_version = safety.APPROVALS_SCHEMA_VERSION + 1
        connection = sqlite3.connect(path)
        try:
            connection.execute(f"PRAGMA user_version = {future_version}")
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(MoneybirdError, "newer Moneybird MCP schema"):
            safety._approvals_connection()

        connection = sqlite3.connect(path)
        try:
            (actual_version,) = connection.execute("PRAGMA user_version").fetchone()
        finally:
            connection.close()
        self.assertEqual(actual_version, future_version)

    def test_same_approval_produces_one_upstream_write_across_100_threads(
        self,
    ) -> None:
        class Client:
            administration_id = "safety-admin"

        approval = safety.make_approval("thread_demo", {"value": 7}, "thread race")
        worker_count = 100
        start = threading.Barrier(worker_count)
        results: list[str] = []
        result_lock = threading.Lock()
        upstream_calls = 0

        def executor(client, payload):
            nonlocal upstream_calls
            with result_lock:
                upstream_calls += 1
            return _verified_executor_result()

        def worker() -> None:
            start.wait()
            try:
                run_approved_write(
                    Client(),
                    approval["approval_id"],
                    "thread_demo",
                    executor,
                )
            except MoneybirdError:
                result = "error"
            else:
                result = "claimed"
            with result_lock:
                results.append(result)

        threads = [threading.Thread(target=worker) for _ in range(worker_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(results.count("claimed"), 1)
        self.assertEqual(results.count("error"), worker_count - 1)
        self.assertEqual(upstream_calls, 1)
        self.assertEqual(
            self._approval_state(approval["approval_id"]),
            ("succeeded", "success"),
        )

    def test_same_approval_is_claimed_once_across_processes(self) -> None:
        approval = safety.make_approval(
            "process_demo",
            {"value": 8},
            "process race",
        )
        context = multiprocessing.get_context("spawn")
        start_event = context.Event()
        result_queue = context.Queue()
        process_count = 8
        processes = [
            context.Process(
                target=_process_pop_approval,
                args=(
                    self._temp_dir.name,
                    approval["approval_id"],
                    start_event,
                    result_queue,
                ),
            )
            for _ in range(process_count)
        ]
        for process in processes:
            process.start()
        start_event.set()
        results = [result_queue.get(timeout=20) for _ in range(process_count)]
        for process in processes:
            process.join(20)

        self.assertTrue(all(process.exitcode == 0 for process in processes))
        self.assertEqual(sum(result[0] == "claimed" for result in results), 1)
        self.assertEqual(sum(result[0] == "error" for result in results), 7)
        self.assertEqual(
            self._approval_state(approval["approval_id"]),
            ("claimed", None),
        )

    def test_executor_without_explicit_outcome_is_verification_failure(self) -> None:
        class Client:
            administration_id = "safety-admin"

        fingerprint = "missing-outcome"
        approval = safety.make_approval(
            "missing_outcome",
            {"fingerprint": fingerprint},
            "missing outcome",
        )

        with self.assertRaisesRegex(MoneybirdError, "explicitly return _audit_result"):
            run_approved_write(
                Client(),
                approval["approval_id"],
                "missing_outcome",
                lambda client, payload: {"_status": "done"},
            )

        self.assertEqual(
            self._approval_state(approval["approval_id"]),
            ("verification_failed", "verification_failed"),
        )
        self.assertFalse(
            safety.audit_log_contains_success(
                "missing_outcome",
                fingerprint,
                administration_id="safety-admin",
            )
        )

    def test_stage_write_always_adds_a_semantic_fingerprint(self) -> None:
        approval = stage_write(
            "fingerprinted_demo",
            summary="fingerprint me",
            payload={"value": 7},
            preview={"value": 7},
        )

        fingerprint = approval["payload"]["fingerprint"]
        self.assertEqual(len(fingerprint), 64)
        claimed = safety.pop_approval(
            approval["approval_id"],
            "fingerprinted_demo",
            administration_id="safety-admin",
        )
        self.assertEqual(claimed["payload"]["fingerprint"], fingerprint)

    def test_post_write_verification_timeout_remains_ambiguous(self) -> None:
        class Client:
            administration_id = "safety-admin"

        fingerprint = "apply-then-verification-timeout"
        first = safety.make_approval(
            "verification_timeout_demo",
            {"fingerprint": fingerprint},
            "first",
        )
        upstream_writes = 0

        def apply_then_timeout(client, payload):
            nonlocal upstream_writes
            mark_write_dispatch_started()
            upstream_writes += 1
            mark_write_verifying()
            raise MoneybirdError(
                "Could not reach Moneybird for operation GET verification."
            )

        with self.assertRaises(MoneybirdError) as caught:
            run_approved_write(
                Client(),
                first["approval_id"],
                "verification_timeout_demo",
                apply_then_timeout,
            )
        message = str(caught.exception)
        self.assertIn("Could not reach", message)
        self.assertIn("may already have been applied", message)
        self.assertIn("Verify the administration before retrying", message)

        self.assertEqual(upstream_writes, 1)
        self.assertEqual(
            self._approval_state(first["approval_id"]),
            ("ambiguous", "ambiguous"),
        )

        second = safety.make_approval(
            "verification_timeout_demo",
            {"fingerprint": fingerprint},
            "second",
        )
        with self.assertRaisesRegex(MoneybirdError, "requires reconciliation"):
            run_approved_write(
                Client(),
                second["approval_id"],
                "verification_timeout_demo",
                lambda client, payload: {"_audit_result": "success"},
            )
        self.assertEqual(upstream_writes, 1)

    def test_read_only_policy_rejects_before_claiming_approval(self) -> None:
        class Client:
            administration_id = "safety-admin"

        approval = safety.make_approval(
            "policy_demo",
            {"fingerprint": "policy-fingerprint"},
            "policy gate",
        )
        executor_calls = 0

        def executor(client, payload):
            nonlocal executor_calls
            executor_calls += 1
            return _verified_executor_result()

        with mock.patch.dict(
            os.environ,
            {"MONEYBIRD_CAPABILITY_MODE": "read_only"},
        ):
            with self.assertRaisesRegex(MoneybirdError, "writes are disabled"):
                run_approved_write(
                    Client(),
                    approval["approval_id"],
                    "policy_demo",
                    executor,
                )

        self.assertEqual(executor_calls, 0)
        self.assertEqual(
            self._approval_state(approval["approval_id"]),
            ("pending", None),
        )
        audit_entries = [
            json.loads(line)
            for line in safety.audit_log_path("safety-admin").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertEqual(audit_entries[-1]["action"], "policy_demo")
        self.assertEqual(audit_entries[-1]["result"], "policy_blocked")

        result = run_approved_write(
            Client(),
            approval["approval_id"],
            "policy_demo",
            executor,
        )
        self.assertEqual(result["status"], "done")
        self.assertEqual(executor_calls, 1)

    def test_read_only_prepare_exposes_that_execution_is_unavailable(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"MONEYBIRD_CAPABILITY_MODE": "read_only"},
        ):
            prepared = stage_write(
                "policy_preview_demo",
                summary="policy preview",
                payload={"value": "unchanged"},
                preview={"value": "unchanged"},
            )

        self.assertEqual(prepared["capability_mode"], "read_only")
        self.assertFalse(prepared["execution_available"])
        self.assertIn("execution will be rejected", prepared["warning"])

    def test_read_only_policy_preserves_manual_executor_approvals(self) -> None:
        from moneybird_mcp.tools import contacts, ledger, sales_batches, workflows

        class Client:
            administration_id = "safety-admin"

        cases = [
            (
                "set_contacts_delivery_method_email",
                contacts.set_contacts_delivery_method_email_from_approval,
            ),
            (
                "reclassify_document_lines",
                ledger.reclassify_document_lines_from_approval,
            ),
            (
                "batch_create_sales_invoices",
                sales_batches.batch_create_sales_invoices_from_approval,
            ),
            (
                "batch_update_sales_invoices",
                sales_batches.batch_update_sales_invoices_from_approval,
            ),
            (
                "batch_schedule_sales_invoices",
                sales_batches.batch_schedule_sales_invoices_from_approval,
            ),
            (
                "bookkeeping_correction_batch",
                workflows.bookkeeping_correction_batch_from_approval,
            ),
        ]
        approvals = {
            action: safety.make_approval(
                action,
                {"fingerprint": f"policy-{action}"},
                f"policy gate for {action}",
            )
            for action, _executor in cases
        }

        with (
            mock.patch(
                "moneybird_mcp.tools._context.get_client",
                return_value=Client(),
            ),
            mock.patch.dict(
                os.environ,
                {"MONEYBIRD_CAPABILITY_MODE": "read_only"},
            ),
        ):
            for action, executor in cases:
                with self.subTest(action=action):
                    with self.assertRaisesRegex(
                        MoneybirdError,
                        "writes are disabled",
                    ):
                        executor(approvals[action]["approval_id"])
                    self.assertEqual(
                        self._approval_state(approvals[action]["approval_id"]),
                        ("pending", None),
                    )

    def test_legacy_audit_success_is_tenant_scoped_and_malformed_lines_are_skipped(
        self,
    ) -> None:
        legacy_path = Path(self._temp_dir.name) / "legacy-audit.jsonl"
        action = "tenant_scoped_demo"
        fingerprint = "tenant-scoped-truncated-line"
        legacy_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "timestamp": "2026-07-30T10:00:00+00:00",
                            "administration_id": "tenant-a",
                            "action": action,
                            "fingerprint": fingerprint,
                            "result": "success",
                        }
                    ),
                    json.dumps(
                        {
                            "timestamp": "2026-07-30T10:01:00+00:00",
                            "action": action,
                            "fingerprint": fingerprint,
                            "result": "success",
                        }
                    ),
                    '{"timestamp":"2026-07-30T10:02:00+00:00"',
                ]
            ),
            encoding="utf-8",
        )

        with mock.patch.object(
            safety,
            "LEGACY_AUDIT_LOG_PATH",
            legacy_path,
        ):
            self.assertTrue(
                safety.audit_log_contains_success(
                    action,
                    fingerprint,
                    administration_id="tenant-a",
                )
            )
            self.assertFalse(
                safety.audit_log_contains_success(
                    action,
                    fingerprint,
                    administration_id="tenant-b",
                )
            )

    def test_purchase_verification_failure_is_not_successful_duplicate(self) -> None:
        class Client:
            administration_id = "safety-admin"

            def __init__(self) -> None:
                self.fetch_count = 0

            def get_document(self, kind, document_id):
                self.fetch_count += 1
                if self.fetch_count == 1:
                    return {
                        "id": document_id,
                        "version": 1,
                        "updated_at": "before",
                        "total_price_incl_tax": "10.00",
                    }
                return {
                    "id": document_id,
                    "version": 2,
                    "updated_at": "after",
                    "total_price_incl_tax": "10.00",
                    "prices_are_incl_tax": True,
                    "details": [
                        {
                            "id": "line-1",
                            "description": "WRONG",
                            "price": "10.00",
                            "ledger_account_id": "ledger",
                            "tax_rate_id": "tax",
                        }
                    ],
                }

            def update_document(self, kind, document_id, patch):
                return {"id": document_id}

        fingerprint = "reconcile-verification-failed"
        payload = {
            "document_kind": "purchase_invoice",
            "document_id": "document-1",
            "expected_version": "1",
            "expected_updated_at": "before",
            "expected_total_before": "10.00",
            "expected_total_incl_tax": "10.00",
            "prices_are_incl_tax": True,
            "details_attributes": [],
            "expected_lines": [
                {
                    "id": "line-1",
                    "description": "EXPECTED",
                    "price": "10.00",
                    "ledger_account_id": "ledger",
                    "tax_rate_id": "tax",
                }
            ],
            "fingerprint": fingerprint,
        }
        approval = safety.make_approval(
            "reconcile_purchase_invoice",
            payload,
            "reconcile",
        )

        result = run_approved_write(
            Client(),
            approval["approval_id"],
            "reconcile_purchase_invoice",
            _execute_reconcile,
        )

        self.assertEqual(result["status"], "completed_with_verification_errors")
        self.assertFalse(result["verified_lines_match"])
        self.assertEqual(
            self._approval_state(approval["approval_id"]),
            ("verification_failed", "verification_failed"),
        )
        self.assertFalse(
            safety.audit_log_contains_success(
                "reconcile_purchase_invoice",
                fingerprint,
                administration_id="safety-admin",
            )
        )
        audit_entry = json.loads(
            safety.audit_log_path("safety-admin").read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(audit_entry["result"], "verification_failed")

    def test_unresolved_fingerprint_blocks_a_second_approval_without_success(self) -> None:
        class Client:
            administration_id = "safety-admin"

        fingerprint = "ambiguous-fingerprint"
        first = safety.make_approval(
            "ambiguous_demo",
            {"fingerprint": fingerprint},
            "first",
        )
        with self.assertRaisesRegex(MoneybirdError, "ambiguous"):
            run_approved_write(
                Client(),
                first["approval_id"],
                "ambiguous_demo",
                lambda client, payload: (_ for _ in ()).throw(
                    MoneybirdError("The write result is ambiguous.")
                ),
            )

        self.assertEqual(
            self._approval_state(first["approval_id"]),
            ("ambiguous", "ambiguous"),
        )
        self.assertFalse(
            safety.audit_log_contains_success(
                "ambiguous_demo",
                fingerprint,
                administration_id="safety-admin",
            )
        )

        second = safety.make_approval(
            "ambiguous_demo",
            {"fingerprint": fingerprint},
            "second",
        )
        executor_called = False

        def executor(client, payload):
            nonlocal executor_called
            executor_called = True
            return {"_audit_result": "success"}

        with self.assertRaisesRegex(MoneybirdError, "requires reconciliation"):
            run_approved_write(
                Client(),
                second["approval_id"],
                "ambiguous_demo",
                executor,
            )
        self.assertFalse(executor_called)
        self.assertEqual(
            self._approval_state(second["approval_id"]),
            ("pending", None),
        )

    def test_executor_cannot_reclassify_a_dispatched_write_as_pre_write(self) -> None:
        class Client:
            administration_id = "safety-admin"

        approval = safety.make_approval(
            "phase_contract_demo",
            {"fingerprint": "phase-contract"},
            "phase contract",
        )

        def contradictory_executor(_client, _payload):
            mark_write_dispatch_started()
            mark_write_verifying()
            return {"_audit_result": "failed_pre_write"}

        with self.assertRaisesRegex(MoneybirdError, "requires reconciliation"):
            run_approved_write(
                Client(),
                approval["approval_id"],
                "phase_contract_demo",
                contradictory_executor,
            )
        state = safety.approval_execution_state(
            approval["approval_id"],
            administration_id=Client.administration_id,
        )
        self.assertEqual(state["state"], "ambiguous")
        self.assertEqual(state["phase"], "verifying")

    def test_partial_failure_is_not_success_and_requires_reconciliation(self) -> None:
        class Client:
            administration_id = "safety-admin"

        fingerprint = "partial-fingerprint"
        first = safety.make_approval(
            "partial_demo",
            {"fingerprint": fingerprint},
            "partial first",
        )
        result = run_approved_write(
            Client(),
            first["approval_id"],
            "partial_demo",
            lambda client, payload: _verified_executor_result(
                audit_result="partial_failure",
                status="completed_with_errors",
            ),
        )

        self.assertEqual(result["status"], "completed_with_errors")
        self.assertEqual(
            self._approval_state(first["approval_id"]),
            ("partial_failure", "partial_failure"),
        )
        self.assertFalse(
            safety.audit_log_contains_success(
                "partial_demo",
                fingerprint,
                administration_id="safety-admin",
            )
        )

        second = safety.make_approval(
            "partial_demo",
            {"fingerprint": fingerprint},
            "partial second",
        )
        with self.assertRaisesRegex(MoneybirdError, "requires reconciliation"):
            run_approved_write(
                Client(),
                second["approval_id"],
                "partial_demo",
                lambda client, payload: {"_audit_result": "success"},
            )
        self.assertEqual(
            self._approval_state(second["approval_id"]),
            ("pending", None),
        )

    def test_same_fingerprint_cannot_overlap_across_two_approvals(self) -> None:
        class Client:
            administration_id = "safety-admin"

        fingerprint = "overlapping-fingerprint"
        approvals = [
            safety.make_approval(
                "overlap_demo",
                {"fingerprint": fingerprint},
                f"overlap {index}",
            )
            for index in range(2)
        ]
        first_entered = threading.Event()
        release_first = threading.Event()
        upstream_calls = 0
        outcomes: list[str] = []

        def slow_executor(client, payload):
            nonlocal upstream_calls
            mark_write_dispatch_started()
            upstream_calls += 1
            first_entered.set()
            release_first.wait(10)
            mark_write_verifying()
            return {"_audit_result": "success", "_status": "done"}

        def execute_first() -> None:
            run_approved_write(
                Client(),
                approvals[0]["approval_id"],
                "overlap_demo",
                slow_executor,
            )

        first_thread = threading.Thread(target=execute_first)
        first_thread.start()
        self.assertTrue(first_entered.wait(10))
        try:
            with self.assertRaisesRegex(MoneybirdError, "already active"):
                run_approved_write(
                    Client(),
                    approvals[1]["approval_id"],
                    "overlap_demo",
                    lambda client, payload: outcomes.append("second")
                    or {"_audit_result": "success"},
                )
        finally:
            release_first.set()
            first_thread.join(10)

        self.assertFalse(first_thread.is_alive())
        self.assertEqual(upstream_calls, 1)
        self.assertEqual(outcomes, [])
        self.assertEqual(
            self._approval_state(approvals[0]["approval_id"]),
            ("succeeded", "success"),
        )


if __name__ == "__main__":
    unittest.main()


class PendingApprovalCollisionTests(unittest.TestCase):
    """Two pending approvals on one record cannot both apply.

    Every preview pins the record's current version, so executing either one
    makes the other stale and it aborts. That is correct but arrives as a
    surprise at execution time, after the user has already approved both, so
    the overlap is reported while there is still time to sequence them.
    """

    def setUp(self) -> None:
        set_active_administration_id("469360474352256236")
        safety.clear_pending_approvals()
        self.addCleanup(safety.clear_pending_approvals)

    def test_reclassify_and_payment_link_on_one_document_collide(self) -> None:
        safety.make_approval(
            "reclassify_document_lines",
            {"document_updates": [{"document_id": "495626291993642938"}]},
            "Reclassify 1 document line(s)",
        )
        second = safety.make_approval(
            "link_bank_mutation_booking",
            {
                "financial_mutation_id": "494564174389577429",
                "booking": {
                    "booking_type": "Document",
                    "booking_id": "495626291993642938",
                },
            },
            "Link mutation to document",
        )
        self.assertIn("collides_with", second)
        self.assertEqual(
            second["collides_with"][0]["shared_targets"],
            ["495626291993642938"],
        )
        self.assertIn("stale", second["collision_warning"])

    def test_two_mutations_sharing_a_ledger_account_do_not_collide(self) -> None:
        safety.make_approval(
            "link_bank_mutation_booking",
            {
                "financial_mutation_id": "494519090817270843",
                "booking": {
                    "booking_type": "LedgerAccount",
                    "booking_id": "469401605624562861",
                },
            },
            "Book bank charge",
        )
        second = safety.make_approval(
            "link_bank_mutation_booking",
            {
                "financial_mutation_id": "494790389687912139",
                "booking": {
                    "booking_type": "LedgerAccount",
                    "booking_id": "469401605624562861",
                },
            },
            "Book another bank charge",
        )
        self.assertNotIn("collides_with", second)

    def test_an_unrelated_approval_is_not_flagged(self) -> None:
        safety.make_approval(
            "reclassify_document_lines",
            {"document_updates": [{"document_id": "111111111111111111"}]},
            "Reclassify",
        )
        second = safety.make_approval(
            "reclassify_document_lines",
            {"document_updates": [{"document_id": "222222222222222222"}]},
            "Reclassify",
        )
        self.assertNotIn("collides_with", second)

    def test_the_first_approval_never_collides_with_itself(self) -> None:
        first = safety.make_approval(
            "reclassify_document_lines",
            {"document_updates": [{"document_id": "333333333333333333"}]},
            "Reclassify",
        )
        self.assertNotIn("collides_with", first)


class ApprovalTargetScopeTests(unittest.TestCase):
    """A referenced contact is not a changed contact."""

    def setUp(self) -> None:
        set_active_administration_id("469360474352256236")
        safety.clear_pending_approvals()
        self.addCleanup(safety.clear_pending_approvals)

    def test_an_invoice_naming_a_contact_does_not_collide_with_a_contact_edit(
        self,
    ) -> None:
        safety.make_approval(
            "update_contact",
            {"contact_id": "470987057279271952", "fields": {"email": "a@b.c"}},
            "Update contact",
        )
        second = safety.make_approval(
            "create_sales_invoice_draft",
            {"contact_id": "470987057279271952", "details": []},
            "Create draft",
        )
        self.assertNotIn("collides_with", second)

    def test_two_edits_of_the_same_contact_still_collide(self) -> None:
        safety.make_approval(
            "update_contact",
            {"contact_id": "470987057279271952"},
            "Update contact",
        )
        second = safety.make_approval(
            "archive_contact",
            {"contact_id": "470987057279271952"},
            "Archive contact",
        )
        self.assertIn("collides_with", second)
        self.assertEqual(
            second["collides_with"][0]["shared_targets"],
            ["470987057279271952"],
        )
