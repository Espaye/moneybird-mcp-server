"""What a failed write proved, and what it only leaves unknown.

``ambiguous`` is expensive on purpose: it leaves an unresolved entry in the
durable audit trail that a human has to close. Spending it on errors that prove
the opposite — a 422 naming the field it rejected — teaches people to ignore the
state, which is exactly what makes it worthless for the timeouts where the write
really may have landed. These tests hold both halves of that line.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault(
    "MONEYBIRD_MCP_DATA_DIR",
    tempfile.mkdtemp(prefix="moneybird_classification_"),
)

import httpx

from moneybird import safety
from moneybird.config import MoneybirdError, MoneybirdHTTPError
from moneybird.formatting import format_reported_error, parse_reported_error
from moneybird.safety import (
    UNRESOLVED_APPROVAL_STATES,
    classify_failed_write,
    record_applied_write,
    reset_applied_writes,
)

VALIDATION_BODY = json.dumps(
    {
        "error": {
            "send_invoices_to_email": [
                "includes a domain which cannot receive emails"
            ]
        },
        "details": {"send_invoices_to_email": [{"error": "email_domain_unreachable"}]},
    }
)


class ReportedErrorTests(unittest.TestCase):
    """HTTP 422 alone is not something a user or an agent can correct."""

    def test_field_level_reason_survives_into_the_message(self) -> None:
        reported = parse_reported_error(VALIDATION_BODY)
        rendered = format_reported_error(reported)
        self.assertIn("send_invoices_to_email", rendered)
        self.assertIn("includes a domain which cannot receive emails", rendered)

    def test_machine_detail_codes_are_left_out(self) -> None:
        # "details" restates "error" as codes; it lengthens the message without
        # telling a reader anything new.
        self.assertNotIn(
            "email_domain_unreachable", format_reported_error(parse_reported_error(VALIDATION_BODY))
        )

    def test_empty_body_adds_nothing(self) -> None:
        for body in (None, "", "   "):
            self.assertEqual(format_reported_error(parse_reported_error(body)), "")

    def test_unparseable_body_is_still_quoted(self) -> None:
        rendered = format_reported_error(parse_reported_error("<html>Gateway</html>"))
        self.assertIn("Gateway", rendered)

    def test_a_huge_body_cannot_flood_the_message_or_audit_log(self) -> None:
        rendered = format_reported_error(parse_reported_error(json.dumps("x" * 50_000)))
        self.assertLess(len(rendered), 1_000)
        self.assertIn("[truncated]", rendered)


class ClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_applied_writes()
        self.addCleanup(reset_applied_writes)

    def test_a_refusal_with_nothing_applied_is_a_closed_failure(self) -> None:
        exc = MoneybirdHTTPError("rejected", status_code=422)
        self.assertEqual(classify_failed_write(exc, phase="dispatching"), "failed")

    def test_every_definitive_status_closes(self) -> None:
        for status in (400, 401, 403, 404, 405, 406, 410, 415, 422):
            with self.subTest(status=status):
                exc = MoneybirdHTTPError("no", status_code=status)
                self.assertEqual(
                    classify_failed_write(exc, phase="dispatching"), "failed"
                )

    def test_a_timeout_still_means_unknown(self) -> None:
        # The whole point of the unresolved state: no answer came back, so the
        # write may or may not have landed.
        exc = MoneybirdError(
            "Could not reach Moneybird for operation /:id/contacts.json. The write "
            "result is ambiguous; reconcile Moneybird before retrying."
        )
        self.assertEqual(classify_failed_write(exc, phase="dispatching"), "ambiguous")

    def test_a_server_error_still_means_unknown(self) -> None:
        for status in (500, 502, 503, 504, 429, 408):
            with self.subTest(status=status):
                exc = MoneybirdHTTPError("upstream", status_code=status)
                self.assertEqual(
                    classify_failed_write(exc, phase="dispatching"), "ambiguous"
                )

    def test_conflict_is_deliberately_not_treated_as_proof(self) -> None:
        # 409 can mean the record already exists, which is precisely the case
        # where something may have been created.
        exc = MoneybirdHTTPError("conflict", status_code=409)
        self.assertEqual(classify_failed_write(exc, phase="dispatching"), "ambiguous")

    def test_a_refusal_after_an_accepted_write_stays_unresolved(self) -> None:
        # A rejection only proves that *this* request applied nothing. An earlier
        # accepted write in the same execution still needs reconciling.
        record_applied_write()
        exc = MoneybirdHTTPError("rejected", status_code=422)
        self.assertEqual(classify_failed_write(exc, phase="dispatching"), "ambiguous")

    def test_a_failed_read_after_an_accepted_write_stays_unresolved(self) -> None:
        # The verification GET can 404 while the write itself succeeded; the
        # status alone must not close that as "nothing happened".
        record_applied_write()
        exc = MoneybirdHTTPError("not found", status_code=404)
        self.assertEqual(classify_failed_write(exc, phase="dispatching"), "ambiguous")

    def test_preflight_failures_are_unchanged(self) -> None:
        exc = MoneybirdHTTPError("rejected", status_code=422)
        self.assertEqual(
            classify_failed_write(exc, phase="preflight"), "failed_pre_write"
        )

    def test_a_plain_error_after_dispatch_is_still_unresolved(self) -> None:
        # Anything without a status code proves nothing about what Moneybird did.
        self.assertEqual(
            classify_failed_write(RuntimeError("boom"), phase="dispatching"),
            "ambiguous",
        )


class AppliedWriteLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_applied_writes()
        self.addCleanup(reset_applied_writes)

    def test_only_mutating_requests_count(self) -> None:
        from moneybird.client import MoneybirdClient

        client = MoneybirdClient(token="t", administration_id="1")

        class _Response:
            status_code = 200
            text = "{}"
            headers: dict = {}

        with mock.patch("moneybird.client.get_shared_http_client") as pool:
            pool.return_value.request.return_value = _Response()
            client._request("GET", "/1/contacts.json")
            self.assertEqual(safety.applied_write_count(), 0)
            client._request("POST", "/1/contacts.json", body={"contact": {}})
            self.assertEqual(safety.applied_write_count(), 1)

    def test_a_rejected_mutation_does_not_count(self) -> None:
        from moneybird.client import MoneybirdClient

        client = MoneybirdClient(token="t", administration_id="1")

        class _Rejected:
            status_code = 422
            text = VALIDATION_BODY
            headers: dict = {}

        with mock.patch("moneybird.client.get_shared_http_client") as pool:
            pool.return_value.request.return_value = _Rejected()
            with self.assertRaises(MoneybirdHTTPError):
                client._request("POST", "/1/contacts.json", body={"contact": {}})
        self.assertEqual(safety.applied_write_count(), 0)

    def test_batch_readers_are_not_counted_as_writes(self) -> None:
        """Moneybird's sync readers are POSTs, and must not look like mutations.

        ``fetch_*_by_ids`` posts a list of ids to .../synchronization.json to
        *read* records in bulk. Counting one as an applied write would make a
        later validation rejection in the same execution stay unresolved even
        though only a read had succeeded — reintroducing exactly the audit-trail
        noise this classification exists to remove.
        """
        from moneybird.client import MoneybirdClient

        client = MoneybirdClient(token="t", administration_id="1")

        class _Response:
            status_code = 200
            text = "[]"
            headers: dict = {}

        readers = (
            client.fetch_contacts_by_ids,
            client.fetch_sales_invoices_by_ids,
            client.fetch_financial_mutations_by_ids,
            client.fetch_recurring_sales_invoices_by_ids,
        )
        with mock.patch("moneybird.client.get_shared_http_client") as pool:
            pool.return_value.request.return_value = _Response()
            for reader in readers:
                with self.subTest(reader=reader.__name__):
                    reader(["1"])
                    self.assertEqual(safety.applied_write_count(), 0)
            client.fetch_documents_by_ids("purchase_invoice", ["1"])
            self.assertEqual(safety.applied_write_count(), 0)

    def test_a_real_write_after_a_batch_read_is_still_counted(self) -> None:
        # The exclusion must not swallow genuine mutations that follow a read.
        from moneybird.client import MoneybirdClient

        client = MoneybirdClient(token="t", administration_id="1")

        class _Response:
            status_code = 200
            text = "{}"
            headers: dict = {}

        with mock.patch("moneybird.client.get_shared_http_client") as pool:
            pool.return_value.request.return_value = _Response()
            client.fetch_contacts_by_ids(["1"])
            client._request("PATCH", "/1/contacts/1.json", body={"contact": {}})
        self.assertEqual(safety.applied_write_count(), 1)

    def test_a_transport_failure_does_not_count(self) -> None:
        from moneybird.client import MoneybirdClient

        client = MoneybirdClient(token="t", administration_id="1")
        with mock.patch("moneybird.client.get_shared_http_client") as pool:
            pool.return_value.request.side_effect = httpx.ConnectError("down")
            with self.assertRaises(MoneybirdError):
                client._request("POST", "/1/contacts.json", body={"contact": {}})
        self.assertEqual(safety.applied_write_count(), 0)


class EndToEndApprovalOutcomeTests(unittest.TestCase):
    """The reported scenario: a typo'd email address must not burn the audit log."""

    def setUp(self) -> None:
        self.administration_id = "9001"
        # A private state directory per test. An 'ambiguous' outcome is an
        # unresolved state that keeps its duplicate fingerprint locked, by
        # design — so without isolation the second run of this suite against a
        # persistent MONEYBIRD_MCP_DATA_DIR would be refused before reaching the
        # classification under test.
        state = tempfile.TemporaryDirectory(prefix="moneybird_classification_state_")
        self.addCleanup(state.cleanup)
        patcher = mock.patch.dict(
            os.environ,
            {
                "MONEYBIRD_MCP_DATA_DIR": state.name,
                "MONEYBIRD_ACCESS_TOKEN": "dummy",
                "MONEYBIRD_ADMINISTRATION_ID": self.administration_id,
                "MONEYBIRD_CAPABILITY_MODE": "write_enabled",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        from moneybird.credentials import set_active_administration_id

        set_active_administration_id(self.administration_id)
        self.addCleanup(set_active_administration_id, None)

    def _run_create(self, response: object, company_name: str) -> str:
        # Each test needs its own payload: an unresolved approval keeps its
        # fingerprint locked, so a second identical write would be refused
        # before it ever reaches the classification under test.
        from moneybird.client import MoneybirdClient
        from moneybird.tools import _context as ctx
        from moneybird.tools import contacts as contact_tools

        client = MoneybirdClient(
            token="dummy", administration_id=self.administration_id
        )
        with mock.patch.object(ctx, "get_client", return_value=client):
            prepared = contact_tools.prepare_create_contact(
                company_name=company_name, email="nobody@example.com"
            )
            approval_id = prepared["approval_id"]
            with mock.patch("moneybird.client.get_shared_http_client") as pool:
                pool.return_value.request.return_value = response
                with self.assertRaises(MoneybirdError) as caught:
                    contact_tools.create_contact_from_approval(approval_id)
        self.raised = caught.exception
        return approval_id

    def test_validation_rejection_closes_the_approval_as_failed(self) -> None:
        class _Rejected:
            status_code = 422
            text = VALIDATION_BODY
            headers: dict = {}

        approval_id = self._run_create(_Rejected(), "Rejected Payload BV")
        state = safety.approval_execution_state(
            approval_id, administration_id=self.administration_id
        )
        self.assertEqual(state["outcome"], "failed")
        self.assertNotIn(
            state["state"],
            UNRESOLVED_APPROVAL_STATES,
            "a rejected write must not leave an entry a human has to reconcile",
        )
        self.assertIn("send_invoices_to_email", str(self.raised))

    def test_a_server_error_still_leaves_it_for_reconciliation(self) -> None:
        class _Broken:
            status_code = 503
            text = ""
            headers: dict = {}

        approval_id = self._run_create(_Broken(), "Unreachable Upstream BV")
        state = safety.approval_execution_state(
            approval_id, administration_id=self.administration_id
        )
        self.assertEqual(state["outcome"], "ambiguous")
        self.assertIn(state["state"], UNRESOLVED_APPROVAL_STATES)


if __name__ == "__main__":
    unittest.main()
