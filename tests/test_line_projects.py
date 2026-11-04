"""Projects (cost centres) on purchase and sales document lines."""
from __future__ import annotations

import copy
import os
import tempfile
import unittest
from unittest import mock

from purchase_test_support import (
    LEDGER_PRIV,
    LEDGER_ZAK,
    TAX_21,
    TAX_GEEN,
    FakeClient,
    target_july,
)

from moneybird_mcp.config import MoneybirdError
from moneybird_mcp.credentials import set_active_administration_id
from moneybird_mcp.document_lines import (
    details_attributes_for_lines,
    dropped_line_projects,
    resulting_line_projects,
    validate_explicit_document_lines,
)
from moneybird_mcp.invoicing import prepare_reclassification_batch
from moneybird_mcp.projects import fetch_all_projects, resolve_project_reference
from moneybird_mcp.purchase_reconcile import (
    build_explicit_purchase_invoice_reconcile,
    build_reconcile_purchase_invoice,
)
from moneybird_mcp.tools import ledger, purchases, sales_batches
from moneybird_mcp.write_contracts import verify_document_reclassification

PROJECTS = [
    {"id": "P-new", "name": "Renovatie", "state": "active"},
    {"id": "P-old", "name": "Onderhoud", "state": "active"},
    {"id": "P-arch", "name": "Archief", "state": "archived"},
]


class _ProjectsMixin:
    """A fake list_projects that counts its calls and insists on state=all."""

    project_calls = 0

    def list_projects(self, *, limit=25, page=1, state=""):
        self.project_calls += 1
        if state != "all":
            raise AssertionError(f"projects listed with state={state!r}")
        start = (page - 1) * limit
        return copy.deepcopy(PROJECTS[start : start + limit])


class _WriteTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="moneybird_line_projects_")
        self._env = mock.patch.dict(
            os.environ,
            {
                "MONEYBIRD_MCP_DATA_DIR": self._temp_dir.name,
                "MONEYBIRD_CAPABILITY_MODE": "write_enabled",
            },
        )
        self._env.start()
        set_active_administration_id("contract-admin")

    def tearDown(self) -> None:
        set_active_administration_id(None)
        self._env.stop()
        self._temp_dir.cleanup()

    @staticmethod
    def _patch_client(module, client):
        return mock.patch.object(module.ctx, "get_client", return_value=client)


class ProjectResolutionTests(unittest.TestCase):
    def test_resolves_by_id_or_exact_name(self) -> None:
        self.assertEqual(resolve_project_reference(PROJECTS, project_id="P-new")["name"], "Renovatie")
        self.assertEqual(resolve_project_reference(PROJECTS, project_name="Onderhoud")["id"], "P-old")

    def test_refuses_unknown_ambiguous_missing_and_archived(self) -> None:
        with self.assertRaisesRegex(MoneybirdError, "Unknown project_id P-x"):
            resolve_project_reference(PROJECTS, project_id="P-x")
        twins = PROJECTS + [{"id": "P-twin", "name": "Renovatie", "state": "active"}]
        with self.assertRaisesRegex(MoneybirdError, "exactly one project named"):
            resolve_project_reference(twins, project_name="Renovatie")
        with self.assertRaisesRegex(MoneybirdError, "Provide project_id or project_name"):
            resolve_project_reference(PROJECTS)
        with self.assertRaisesRegex(MoneybirdError, "is archived"):
            resolve_project_reference(PROJECTS, project_id="P-arch")

    def test_fetch_pages_through_every_state(self) -> None:
        class Client(_ProjectsMixin):
            pass

        many = [{"id": f"P{i}", "name": f"Project {i}", "state": "active"} for i in range(150)]
        client = Client()
        with mock.patch(f"{__name__}.PROJECTS", many):
            projects = fetch_all_projects(client)
        self.assertEqual(len(projects), 150)
        self.assertEqual(client.project_calls, 2)


class ReclassifyClient(_ProjectsMixin):
    administration_id = "contract-admin"

    def __init__(self, *, locked_until=None, keep_project=True, total_drift=False):
        self.locked_until = locked_until
        self.keep_project = keep_project
        self.total_drift = total_drift
        self.update_calls = 0
        self.document = {
            "id": "doc-1",
            "version": 1,
            "date": "2026-10-01",
            "reference": "INV-1",
            "contact": {"company_name": "Supplier B.V."},
            "total_price_excl_tax": "100.0",
            "total_price_incl_tax": "121.0",
            "details": [
                {
                    "id": "d1",
                    "description": "Hosting",
                    "price": "60.0",
                    "amount": "1",
                    "ledger_account_id": "L-costs",
                    "tax_rate_id": "T21",
                    "project_id": None,
                },
                {
                    "id": "d2",
                    "description": "Support",
                    "price": "40.0",
                    "amount": "1",
                    "ledger_account_id": "L-costs",
                    "tax_rate_id": "T21",
                    "project_id": "P-old",
                },
            ],
        }

    def list_ledger_accounts(self):
        return [
            {"id": "L-costs", "name": "Kosten", "account_type": "expenses"},
            {"id": "L-other", "name": "Overige kosten", "account_type": "expenses"},
            {"id": "L-bal", "name": "Vooruitbetaald", "account_type": "current_assets"},
        ]

    def require_current_administration_access(self):
        return {"id": self.administration_id, "period_locked_until": self.locked_until}

    def get_document(self, _kind, _document_id):
        return copy.deepcopy(self.document)

    def update_document(self, _kind, _document_id, document):
        self.update_calls += 1
        details = {str(item["id"]): item for item in self.document["details"]}
        for operation in document["details_attributes"].values():
            target = details[str(operation["id"])]
            for field, value in operation.items():
                if field == "project_id" and not self.keep_project:
                    continue
                target[field] = value
        if self.total_drift:
            self.document["total_price_incl_tax"] = "121.5"
        self.document["version"] += 1
        return copy.deepcopy(self.document)


class ReclassifyLineProjectTests(_WriteTestCase):
    def test_project_only_entry_sets_the_project_and_keeps_the_ledger(self) -> None:
        client = ReclassifyClient()
        prepared = prepare_reclassification_batch(
            client,
            [{"document_kind": "purchase_invoice", "document_id": "doc-1", "detail_id": "d1", "project_name": "Renovatie"}],
        )

        update = prepared["payload"]["document_updates"][0]
        self.assertEqual(update["details_attributes"], [{"id": "d1", "project_id": "P-new"}])
        self.assertEqual(
            update["expected_totals"],
            {"total_price_excl_tax": "100.0", "total_price_incl_tax": "121.0"},
        )
        change = prepared["preview"]["line_changes"][0]
        self.assertEqual(change["project"]["before"], None)
        self.assertEqual(change["project"]["after"], {"id": "P-new", "name": "Renovatie"})
        self.assertEqual(change["ledger_account"]["before"], change["ledger_account"]["after"])
        self.assertEqual(prepared["preview"]["rows"][0]["status"], "ready")
        self.assertIn("project (none) -> Renovatie", prepared["preview"]["rows"][0]["description"])

    def test_ledger_and_project_change_show_both_before_and_after(self) -> None:
        client = ReclassifyClient()
        prepared = prepare_reclassification_batch(
            client,
            [
                {
                    "document_kind": "purchase_invoice",
                    "document_id": "doc-1",
                    "detail_id": "d2",
                    "ledger_account_id": "L-other",
                    "project_id": "P-new",
                }
            ],
        )

        update = prepared["payload"]["document_updates"][0]
        self.assertEqual(
            update["details_attributes"],
            [{"id": "d2", "ledger_account_id": "L-other", "project_id": "P-new"}],
        )
        self.assertEqual(
            update["precondition"]["target_details"]["d2"],
            {"ledger_account_id": "L-costs", "project_id": "P-old"},
        )
        description = prepared["preview"]["rows"][0]["description"]
        self.assertIn("Kosten -> Overige kosten", description)
        self.assertIn("project Onderhoud -> Renovatie", description)

    def test_project_in_fingerprint(self) -> None:
        entry = {"document_kind": "purchase_invoice", "document_id": "doc-1", "detail_id": "d1"}
        first = prepare_reclassification_batch(ReclassifyClient(), [{**entry, "project_id": "P-new"}])
        second = prepare_reclassification_batch(ReclassifyClient(), [{**entry, "project_id": "P-old"}])
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])

    def test_ledger_only_batch_never_reads_projects(self) -> None:
        client = ReclassifyClient()
        prepared = prepare_reclassification_batch(
            client,
            [{"document_kind": "purchase_invoice", "document_id": "doc-1", "detail_id": "d1", "ledger_account_id": "L-other"}],
        )
        self.assertEqual(client.project_calls, 0)
        self.assertEqual(
            prepared["payload"]["document_updates"][0]["details_attributes"],
            [{"id": "d1", "ledger_account_id": "L-other"}],
        )

    def test_refusals(self) -> None:
        base = {"document_kind": "purchase_invoice", "document_id": "doc-1", "detail_id": "d1"}
        cases = [
            ({**base}, r"a project_id or project_name \(or clear_project\), or both"),
            ({**base, "clear_project": True, "project_id": "P-new"}, "cannot be combined with project_id"),
            (
                {
                    **base,
                    "ledger_account_id": "L-other",
                    "clear_project": True,
                    "balancing_ledger_account_id": "L-bal",
                    "journal_reference": "J-1",
                },
                "cannot be combined with a project change",
            ),
            ({**base, "project_id": "P-arch"}, "is archived"),
            ({**base, "project_id": "P-missing"}, "Unknown project_id"),
            (
                {
                    **base,
                    "ledger_account_id": "L-other",
                    "project_id": "P-new",
                    "balancing_ledger_account_id": "L-bal",
                    "journal_reference": "J-1",
                },
                "cannot be combined with a project change",
            ),
            (
                {**base, "project_id": "P-new", "balancing_ledger_account_id": "L-bal", "journal_reference": "J-1"},
                "balancing journal needs a target ledger",
            ),
        ]
        for entry, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(MoneybirdError, message):
                    prepare_reclassification_batch(ReclassifyClient(), [entry])

    def test_document_inside_a_locked_period_is_refused(self) -> None:
        entry = {"document_kind": "purchase_invoice", "document_id": "doc-1", "detail_id": "d1", "project_id": "P-new"}
        with self.assertRaisesRegex(MoneybirdError, "inside the period locked through 2026-10-31"):
            prepare_reclassification_batch(ReclassifyClient(locked_until="2026-10-31"), [entry])
        prepared = prepare_reclassification_batch(ReclassifyClient(locked_until="2026-09-30"), [entry])
        self.assertEqual(prepared["preview"]["document_count"], 1)

    def test_clear_project_removes_it_without_reading_projects(self) -> None:
        client = ReclassifyClient()
        prepared = prepare_reclassification_batch(
            client,
            [{"document_kind": "purchase_invoice", "document_id": "doc-1", "detail_id": "d2", "clear_project": True}],
        )

        self.assertEqual(client.project_calls, 0)
        update = prepared["payload"]["document_updates"][0]
        self.assertEqual(update["details_attributes"], [{"id": "d2", "project_id": None}])
        self.assertEqual(prepared["preview"]["line_changes"][0]["project"]["after"], None)
        self.assertIn("project P-old -> (none)", prepared["preview"]["rows"][0]["description"])

    def test_execution_verifies_a_cleared_project(self) -> None:
        client = ReclassifyClient()
        entry = {"document_kind": "purchase_invoice", "document_id": "doc-1", "detail_id": "d2", "clear_project": "true"}
        result = self._execute(client, entry)

        self.assertEqual(result["status"], "completed")
        self.assertIsNone(client.document["details"][1]["project_id"])

    def test_execution_flags_a_clear_that_did_not_land(self) -> None:
        entry = {"document_kind": "purchase_invoice", "document_id": "doc-1", "detail_id": "d2", "clear_project": True}
        result = self._execute(ReclassifyClient(keep_project=False), entry)

        self.assertEqual(result["status"], "completed_with_verification_errors")

    def _execute(self, client, entry=None):
        entry = entry or {"document_kind": "purchase_invoice", "document_id": "doc-1", "detail_id": "d1", "project_id": "P-new"}
        with self._patch_client(ledger, client):
            prepared = ledger.prepare_reclassify_document_lines([entry])
            return ledger.reclassify_document_lines_from_approval(prepared["approval_id"])

    def test_execution_verifies_the_project_and_the_totals(self) -> None:
        client = ReclassifyClient()
        result = self._execute(client)

        self.assertEqual(result["status"], "completed")
        check = result["verification"][0]
        self.assertTrue(check["fully_verified"])
        self.assertEqual(client.document["details"][0]["project_id"], "P-new")

    def test_execution_flags_a_project_that_did_not_land(self) -> None:
        result = self._execute(ReclassifyClient(keep_project=False))

        self.assertEqual(result["status"], "completed_with_verification_errors")
        self.assertTrue(result["verification"][0]["line_mismatches"])

    def test_execution_flags_a_total_that_moved(self) -> None:
        result = self._execute(ReclassifyClient(total_drift=True))

        self.assertEqual(result["status"], "completed_with_verification_errors")
        self.assertTrue(result["verification"][0]["field_mismatches"])

    def test_approval_without_expected_totals_still_verifies(self) -> None:
        record = {"total_price_incl_tax": "999", "details": [{"id": "d1", "ledger_account_id": "L-other"}]}
        checked = verify_document_reclassification([{"id": "d1", "ledger_account_id": "L-other"}], record)
        self.assertTrue(checked["fully_verified"])


class SalesInvoiceClient(_ProjectsMixin):
    administration_id = "contract-admin"

    def __init__(self, *, keep_project=True, state="draft", invoice_date=None, locked_until=""):
        self.keep_project = keep_project
        self.locked_until = locked_until
        self.update_calls = 0
        self.lock_reads = 0
        self.record = {
            "id": "invoice-1",
            "invoice_id": "2026-001",
            "version": 1,
            "state": state,
            "invoice_date": invoice_date,
            "contact": {"customer_id": "C1"},
            "details": [
                {
                    "id": "line-1",
                    "row_order": 0,
                    "description": "Consultancy",
                    "ledger_account_id": "L-revenue",
                    "project_id": None,
                }
            ],
        }

    def require_current_administration_access(self):
        self.lock_reads += 1
        return {"id": self.administration_id, "period_locked_until": self.locked_until}

    def get_sales_invoice(self, _invoice_id):
        return copy.deepcopy(self.record)

    def update_sales_invoice(self, _invoice_id, patch):
        self.update_calls += 1
        for field in ("invoice_date", "due_date", "reference"):
            if field in patch:
                self.record[field] = patch[field]
        details = {str(item["id"]): item for item in self.record["details"]}
        for operation in patch.get("details_attributes") or []:
            target = details[str(operation["id"])]
            for field, value in operation.items():
                if field == "project_id" and not self.keep_project:
                    continue
                target[field] = value
        self.record["version"] += 1
        return copy.deepcopy(self.record)


class SalesInvoiceLineProjectTests(_WriteTestCase):
    @staticmethod
    def _entries(project_id):
        return [
            {
                "sales_invoice_id": "invoice-1",
                "detail_updates": [{"row_order": 0, "project_id": project_id}],
            }
        ]

    def test_project_update_is_previewed_executed_and_verified(self) -> None:
        client = SalesInvoiceClient()
        with self._patch_client(sales_batches, client):
            prepared = sales_batches.prepare_batch_update_sales_invoices(self._entries("P-new"))
            change = prepared["preview"]["line_changes"][0]
            self.assertEqual(change["project_id"]["before"], None)
            self.assertEqual(change["project_id"]["after"], "P-new")
            self.assertEqual(change["project_id"]["after_name"], "Renovatie")
            self.assertEqual(
                prepared["payload"]["items"][0]["patch"]["details_attributes"],
                [{"id": "line-1", "project_id": "P-new"}],
            )
            result = sales_batches.batch_update_sales_invoices_from_approval(prepared["approval_id"])

        self.assertEqual(result["status"], "completed")
        self.assertEqual(client.record["details"][0]["project_id"], "P-new")

    def test_project_that_did_not_land_fails_verification(self) -> None:
        client = SalesInvoiceClient(keep_project=False)
        with self._patch_client(sales_batches, client):
            prepared = sales_batches.prepare_batch_update_sales_invoices(self._entries("P-new"))
            result = sales_batches.batch_update_sales_invoices_from_approval(prepared["approval_id"])

        self.assertEqual(result["status"], "completed_with_verification_errors")

    def test_archived_or_unknown_project_is_refused_before_staging(self) -> None:
        client = SalesInvoiceClient()
        with self._patch_client(sales_batches, client):
            with self.assertRaisesRegex(MoneybirdError, "is archived"):
                sales_batches.prepare_batch_update_sales_invoices(self._entries("P-arch"))
            with self.assertRaisesRegex(MoneybirdError, "Unknown project_id"):
                sales_batches.prepare_batch_update_sales_invoices(self._entries("P-missing"))
        self.assertEqual(client.update_calls, 0)

    def test_description_only_update_does_not_read_projects(self) -> None:
        client = SalesInvoiceClient()
        with self._patch_client(sales_batches, client):
            prepared = sales_batches.prepare_batch_update_sales_invoices(
                [{"sales_invoice_id": "invoice-1", "detail_updates": [{"row_order": 0, "description": "New"}]}]
            )
        self.assertEqual(client.project_calls, 0)
        self.assertNotIn("line_changes", prepared["preview"])

    def test_project_by_name(self) -> None:
        client = SalesInvoiceClient()
        with self._patch_client(sales_batches, client):
            prepared = sales_batches.prepare_batch_update_sales_invoices(
                [{"sales_invoice_id": "invoice-1", "detail_updates": [{"row_order": 0, "project_name": "Renovatie"}]}]
            )
        self.assertEqual(
            prepared["payload"]["items"][0]["patch"]["details_attributes"],
            [{"id": "line-1", "project_id": "P-new"}],
        )

    def test_clear_project_is_previewed_executed_and_verified(self) -> None:
        client = SalesInvoiceClient()
        client.record["details"][0]["project_id"] = "P-old"
        with self._patch_client(sales_batches, client):
            prepared = sales_batches.prepare_batch_update_sales_invoices(
                [{"sales_invoice_id": "invoice-1", "detail_updates": [{"row_order": 0, "clear_project": True}]}]
            )
            self.assertEqual(client.project_calls, 0)
            self.assertEqual(
                prepared["payload"]["items"][0]["patch"]["details_attributes"],
                [{"id": "line-1", "project_id": None}],
            )
            change = prepared["preview"]["line_changes"][0]["project_id"]
            self.assertEqual((change["before"], change["after"], change["after_name"]), ("P-old", None, None))
            result = sales_batches.batch_update_sales_invoices_from_approval(prepared["approval_id"])

        self.assertEqual(result["status"], "completed")
        self.assertIsNone(client.record["details"][0]["project_id"])

    def test_clear_project_that_did_not_land_fails_verification(self) -> None:
        client = SalesInvoiceClient(keep_project=False)
        client.record["details"][0]["project_id"] = "P-old"
        with self._patch_client(sales_batches, client):
            prepared = sales_batches.prepare_batch_update_sales_invoices(
                [{"sales_invoice_id": "invoice-1", "detail_updates": [{"row_order": 0, "clear_project": True}]}]
            )
            result = sales_batches.batch_update_sales_invoices_from_approval(prepared["approval_id"])

        self.assertEqual(result["status"], "completed_with_verification_errors")

    def test_clear_with_a_project_is_refused(self) -> None:
        client = SalesInvoiceClient()
        with self._patch_client(sales_batches, client):
            with self.assertRaisesRegex(MoneybirdError, "cannot be combined"):
                sales_batches.prepare_batch_update_sales_invoices(
                    [
                        {
                            "sales_invoice_id": "invoice-1",
                            "detail_updates": [{"row_order": 0, "clear_project": True, "project_id": "P-new"}],
                        }
                    ]
                )


class SalesInvoiceLockTests(_WriteTestCase):
    @staticmethod
    def _update(client, **entry):
        with mock.patch.object(sales_batches.ctx, "get_client", return_value=client):
            return sales_batches.prepare_batch_update_sales_invoices(
                [{"sales_invoice_id": "invoice-1", **entry}]
            )

    def test_sent_invoice_inside_the_lock_is_refused(self) -> None:
        client = SalesInvoiceClient(state="open", invoice_date="2026-09-15", locked_until="2026-09-30")
        with self.assertRaisesRegex(MoneybirdError, "Sales invoice 2026-001 is dated 2026-09-15, inside the period locked through 2026-09-30"):
            self._update(client, detail_updates=[{"row_order": 0, "project_id": "P-new"}])

    def test_new_invoice_date_inside_the_lock_is_refused_even_for_a_draft(self) -> None:
        client = SalesInvoiceClient(locked_until="2026-09-30")
        with self.assertRaisesRegex(MoneybirdError, "with the new invoice_date is dated 2026-09-01"):
            self._update(client, invoice_date="2026-09-01")
        prepared = self._update(client, invoice_date="2026-10-15")
        self.assertEqual(prepared["payload"]["items"][0]["patch"], {"invoice_date": "2026-10-15"})

    def test_sent_invoice_after_the_lock_and_drafts_pass(self) -> None:
        sent = SalesInvoiceClient(state="open", invoice_date="2026-10-15", locked_until="2026-09-30")
        self._update(sent, detail_updates=[{"row_order": 0, "description": "New"}])
        self.assertEqual(sent.lock_reads, 1)

        draft = SalesInvoiceClient(invoice_date="2026-09-15", locked_until="2026-09-30")
        self._update(draft, detail_updates=[{"row_order": 0, "description": "New"}])
        self.assertEqual(draft.lock_reads, 0)


class ReconcileClient(_ProjectsMixin, FakeClient):
    pass


def _target_with_projects():
    target = target_july()
    target["details"][0]["project_id"] = "P-old"
    target["details"][1]["project_id"] = "P-new"
    return target


def _desired(**extra):
    return [
        {
            "description": "Termijnnota juli 2026",
            "price": "825.00",
            "ledger_account_id": LEDGER_ZAK,
            "tax_rate_id": TAX_21,
            **extra,
        }
    ]


class LineMatchingProjectTests(unittest.TestCase):
    current = [
        {"id": "a", "ledger_account_id": "L1", "tax_rate_id": "T1", "project_id": "P1"},
        {"id": "b", "ledger_account_id": "L2", "tax_rate_id": "T2", "project_id": "P2"},
    ]

    def test_reused_line_keeps_its_project_without_resending_it(self) -> None:
        desired = [{"description": "x", "price": 1, "ledger_account_id": "L1", "tax_rate_id": "T1"}]
        ops = details_attributes_for_lines(self.current, desired)
        self.assertEqual(ops[0]["id"], "a")
        self.assertNotIn("project_id", ops[0])
        self.assertEqual(ops[1], {"id": "b", "_destroy": "true"})
        self.assertEqual(resulting_line_projects(self.current, desired), ["P1"])
        self.assertEqual(dropped_line_projects(self.current, desired), ["P2"])

    def test_stated_project_is_sent_and_saves_the_drop(self) -> None:
        desired = [
            {"description": "x", "price": 1, "ledger_account_id": "L1", "tax_rate_id": "T1"},
            {"description": "y", "price": 1, "ledger_account_id": "L3", "tax_rate_id": "T3", "project_id": "P2"},
        ]
        ops = details_attributes_for_lines(self.current, desired)
        self.assertEqual(ops[1]["project_id"], "P2")
        self.assertNotIn("id", ops[1])
        self.assertEqual(dropped_line_projects(self.current, desired), [])

    def test_clear_project_removes_a_reused_lines_project_only(self) -> None:
        desired = [
            {"description": "x", "price": 1, "ledger_account_id": "L1", "tax_rate_id": "T1", "clear_project": True},
            {"description": "y", "price": 1, "ledger_account_id": "L3", "tax_rate_id": "T3", "clear_project": True},
        ]
        ops = details_attributes_for_lines(self.current, desired)
        self.assertEqual(ops[0]["id"], "a")
        self.assertIsNone(ops[0]["project_id"])
        self.assertNotIn("project_id", ops[1])
        self.assertEqual(resulting_line_projects(self.current, desired), ["", ""])
        # P1 is removed on purpose; P2 left with its removed line.
        self.assertEqual(dropped_line_projects(self.current, desired), ["P2"])

    def test_validator_marks_a_clear_and_refuses_a_contradiction(self) -> None:
        client = ReconcileClient([target_july()])
        validated = validate_explicit_document_lines(
            client,
            document_kind="purchase_invoice",
            lines=_desired(clear_project=True),
            prices_are_incl_tax=True,
        )
        self.assertIs(validated.lines[0]["clear_project"], True)
        self.assertNotIn("project_id", validated.lines[0])
        self.assertEqual(client.project_calls, 0)
        with self.assertRaisesRegex(MoneybirdError, r"desired_lines\[1\] clear_project cannot be combined"):
            validate_explicit_document_lines(
                client,
                document_kind="purchase_invoice",
                lines=_desired(clear_project=True, project_id="P-new"),
                prices_are_incl_tax=True,
            )

    def test_validator_checks_the_project_and_reads_projects_only_when_named(self) -> None:
        client = ReconcileClient([target_july()])
        validated = validate_explicit_document_lines(
            client,
            document_kind="purchase_invoice",
            lines=_desired(),
            prices_are_incl_tax=True,
        )
        self.assertNotIn("project_id", validated.lines[0])
        self.assertEqual(client.project_calls, 0)

        validated = validate_explicit_document_lines(
            client,
            document_kind="purchase_invoice",
            lines=_desired(project_id="P-new"),
            prices_are_incl_tax=True,
        )
        self.assertEqual(validated.lines[0]["project_id"], "P-new")
        with self.assertRaisesRegex(MoneybirdError, r"desired_lines\[1\] Project .* is archived"):
            validate_explicit_document_lines(
                client,
                document_kind="purchase_invoice",
                lines=_desired(project_id="P-arch"),
                prices_are_incl_tax=True,
            )


class ReconcileLineProjectTests(unittest.TestCase):
    def _build(self, client, **extra):
        return build_explicit_purchase_invoice_reconcile(
            client,
            document_id="tgt",
            document_kind="purchase_invoice",
            desired_lines=_desired(**extra),
            prices_are_incl_tax=True,
            source_note="PDF",
        )

    def test_explicit_rewrite_warns_about_a_project_it_drops(self) -> None:
        built = self._build(ReconcileClient([_target_with_projects()]))
        preview = built["preview"]

        self.assertEqual(preview["projects_dropped"], ["P-new"])
        self.assertTrue(any("P-new" in warning for warning in preview["warnings"]))
        self.assertEqual(preview["after_lines"][0]["project_id"], "P-old")
        self.assertEqual(
            [line["project_id"] for line in preview["before_lines"]],
            ["P-old", "P-new"],
        )
        self.assertEqual(built["payload"]["expected_line_projects"][0]["project_id"], "P-old")
        self.assertNotIn("project_id", built["payload"]["expected_lines"][0])

    def test_explicit_project_is_sent_and_moves_the_line(self) -> None:
        built = self._build(ReconcileClient([_target_with_projects()]), project_id="P-new")

        reused = built["payload"]["details_attributes"][0]
        self.assertEqual(reused["project_id"], "P-new")
        # P-old leaves on purpose (its line is reused) and P-new survives the
        # removed line, so nothing is dropped.
        self.assertEqual(built["preview"]["projects_dropped"], [])
        self.assertFalse(built["preview"]["already_consistent"])

    def test_project_change_alone_is_not_already_consistent(self) -> None:
        target = target_july()
        target["prices_are_incl_tax"] = True
        target["details"] = [
            {
                "id": "L1",
                "description": "Termijnnota juli 2026",
                "price": "825.00",
                "amount": "1",
                "ledger_account_id": LEDGER_ZAK,
                "tax_rate_id": TAX_21,
                "project_id": "P-old",
            }
        ]
        same = self._build(ReconcileClient([copy.deepcopy(target)]))
        moved = self._build(ReconcileClient([copy.deepcopy(target)]), project_id="P-new")

        self.assertTrue(same["preview"]["already_consistent"])
        self.assertFalse(moved["preview"]["already_consistent"])

    def test_template_rewrite_keeps_reused_line_projects(self) -> None:
        from purchase_test_support import reference_june

        target = _target_with_projects()
        built = build_reconcile_purchase_invoice(
            ReconcileClient([reference_june(), target]),
            document_id="tgt",
            reference_document_id="ref",
        )
        projects = {
            (line["ledger_account_id"], line["tax_rate_id"], line["project_id"])
            for line in built["preview"]["after_lines"]
        }
        self.assertIn((LEDGER_ZAK, TAX_21, "P-old"), projects)
        self.assertIn((LEDGER_PRIV, TAX_GEEN, "P-new"), projects)
        self.assertEqual(built["preview"]["projects_dropped"], [])

    def _execute(self, client, payload):
        with (
            mock.patch("moneybird_mcp.tools.purchases.mark_write_dispatch_started"),
            mock.patch("moneybird_mcp.tools.purchases.mark_write_verifying"),
        ):
            return purchases._execute_reconcile(client, payload)

    def test_execution_verifies_line_projects(self) -> None:
        client = ReconcileClient([_target_with_projects()])
        payload = self._build(client, project_id="P-new")["payload"]

        result = self._execute(client, payload)

        self.assertEqual(result["_status"], "completed")
        self.assertTrue(result["verified_line_projects_match"])
        self.assertEqual(result["lines"][0]["project_id"], "P-new")

    def test_execution_flags_a_project_that_did_not_land(self) -> None:
        client = ReconcileClient([_target_with_projects()])
        payload = self._build(client, project_id="P-new")["payload"]
        original_update = client.update_document

        def update_without_projects(kind, document_id, document):
            for operation in document["details_attributes"].values():
                operation.pop("project_id", None)
            return original_update(kind, document_id, document)

        client.update_document = update_without_projects
        result = self._execute(client, payload)

        self.assertTrue(result["verified_lines_match"])
        self.assertFalse(result["verified_line_projects_match"])
        self.assertEqual(result["_status"], "completed_with_verification_errors")

    def test_explicit_clear_is_sent_executed_and_verified(self) -> None:
        client = ReconcileClient([_target_with_projects()])
        built = self._build(client, clear_project=True)

        self.assertIsNone(built["payload"]["details_attributes"][0]["project_id"])
        self.assertEqual(built["preview"]["after_lines"][0]["project_id"], "")
        result = self._execute(client, built["payload"])

        self.assertEqual(result["_status"], "completed")
        self.assertTrue(result["verified_line_projects_match"])
        self.assertEqual(result["lines"][0]["project_id"], "")

    def test_document_inside_a_locked_period_is_refused(self) -> None:
        from purchase_test_support import reference_june

        client = ReconcileClient([reference_june(), _target_with_projects()])
        client.period_locked_until = "2026-07-31"
        with self.assertRaisesRegex(MoneybirdError, "dated 2026-07-19, inside the period locked through 2026-07-31"):
            self._build(client)
        with self.assertRaisesRegex(MoneybirdError, "inside the period locked"):
            build_reconcile_purchase_invoice(client, document_id="tgt", reference_document_id="ref")
        client.period_locked_until = "2026-06-30"
        self.assertEqual(self._build(client)["payload"]["document_id"], "tgt")

    def test_older_approval_without_project_expectation_still_executes(self) -> None:
        client = ReconcileClient([_target_with_projects()])
        payload = self._build(client)["payload"]
        payload.pop("expected_line_projects")

        result = self._execute(client, payload)

        self.assertTrue(result["verified_line_projects_match"])
        self.assertEqual(result["_status"], "completed")


class BankBookingClient(_ProjectsMixin):
    """A bank mutation with one direct booking, honouring project and description on link."""

    administration_id = "contract-admin"

    def __init__(self, *, store_project=True, reject_project_links=False, fail_link_to=""):
        self.store_project = store_project
        self.reject_project_links = reject_project_links
        self.fail_link_to = fail_link_to
        self.links: list[dict] = []
        self.next_booking = 2
        self.ledgers = [
            {"id": "L-rent", "account_id": "4300", "name": "Huur", "account_type": "expenses", "active": True},
            {"id": "L-other", "account_id": "4900", "name": "Overige kosten", "account_type": "expenses", "active": True},
        ]
        self.mutation = {
            "id": "mut-1",
            "date": "2026-10-03",
            "version": 3,
            "state": "processed",
            "amount": "-500.00",
            "amount_open": "0.00",
            "message": "Huur oktober",
            "contra_account_name": "Verhuurder B.V.",
            "payments": [],
            "ledger_account_bookings": [
                {
                    "id": "booking-1",
                    "ledger_account_id": "L-rent",
                    "price": "-500.00",
                    "description": "Huur oktober",
                    "project_id": "P-old",
                }
            ],
        }

    def list_ledger_accounts(self):
        return copy.deepcopy(self.ledgers)

    def get_ledger_account(self, ledger_id):
        return copy.deepcopy(next(item for item in self.ledgers if item["id"] == ledger_id))

    def get_financial_mutation(self, _mutation_id):
        return copy.deepcopy(self.mutation)

    def unlink_financial_mutation_booking(self, _mutation_id, *, booking_type, booking_id):
        self.mutation["ledger_account_bookings"] = [
            item for item in self.mutation["ledger_account_bookings"] if item["id"] != booking_id
        ]
        self.mutation["amount_open"] = self.mutation["amount"]
        self.mutation["state"] = "unprocessed"
        self.mutation["version"] += 1
        return copy.deepcopy(self.mutation)

    def link_financial_mutation_booking(self, _mutation_id, booking):
        self.links.append(dict(booking))
        if booking["booking_id"] == self.fail_link_to:
            raise MoneybirdError("simulated link failure")
        if self.reject_project_links and booking.get("project_id"):
            raise MoneybirdError("simulated project refusal")
        self.mutation["ledger_account_bookings"].append(
            {
                "id": f"booking-{self.next_booking}",
                "ledger_account_id": booking["booking_id"],
                "price": booking["price"],
                "description": booking.get("description"),
                "project_id": booking.get("project_id") if self.store_project else None,
            }
        )
        self.next_booking += 1
        self.mutation["amount_open"] = "0.00"
        self.mutation["state"] = "processed"
        self.mutation["version"] += 1
        return copy.deepcopy(self.mutation)


class BankReclassifyProjectTests(_WriteTestCase):
    base = {"financial_mutation_id": "mut-1", "ledger_account_booking_id": "booking-1"}

    def _prepare(self, client, **extra):
        from moneybird_mcp.tools import bank

        with self._patch_client(bank, client):
            return bank.prepare_reclassify_bank_mutation_bookings([{**self.base, **extra}])

    def _run(self, client, **extra):
        from moneybird_mcp.tools import bank

        with self._patch_client(bank, client):
            prepared = bank.prepare_reclassify_bank_mutation_bookings([{**self.base, **extra}])
            return prepared, bank.reclassify_bank_mutation_bookings_from_approval(prepared["approval_id"])

    @staticmethod
    def _booking(client):
        (booking,) = client.mutation["ledger_account_bookings"]
        return booking

    def test_ledger_move_keeps_project_and_description_without_reading_projects(self) -> None:
        client = BankBookingClient()
        prepared, result = self._run(client, target_ledger_account_id="L-other")

        self.assertEqual(client.project_calls, 0)
        self.assertEqual(prepared["preview"]["rows"][0]["project"], "P-old (kept)")
        self.assertEqual(client.links[0]["project_id"], "P-old")
        self.assertEqual(client.links[0]["description"], "Huur oktober")
        self.assertEqual(result["status"], "completed")
        completed = result["completed"][0]
        self.assertTrue(completed["verification"]["target_project_matches"])
        self.assertTrue(completed["description_kept"])
        self.assertEqual(
            {key: self._booking(client)[key] for key in ("ledger_account_id", "project_id", "description")},
            {"ledger_account_id": "L-other", "project_id": "P-old", "description": "Huur oktober"},
        )

    def test_project_only_move_keeps_the_ledger(self) -> None:
        client = BankBookingClient()
        prepared, result = self._run(client, project_name="Renovatie")

        row = prepared["preview"]["rows"][0]
        self.assertEqual(row["project"], "Onderhoud -> Renovatie")
        self.assertEqual(row["target_ledger_account_id"], "L-rent")
        self.assertIn("project", prepared["preview"]["preview_table"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self._booking(client)["project_id"], "P-new")
        self.assertEqual(self._booking(client)["ledger_account_id"], "L-rent")

    def test_clear_project_relinks_without_one(self) -> None:
        client = BankBookingClient()
        prepared, result = self._run(client, target_ledger_account_name="Overige kosten", clear_project=True)

        self.assertEqual(client.project_calls, 0)
        self.assertEqual(prepared["preview"]["rows"][0]["project"], "P-old -> (none)")
        self.assertNotIn("project_id", client.links[0])
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(self._booking(client)["project_id"])

    def test_refusals(self) -> None:
        cases = [
            ({}, "or clear_project"),
            ({"target_ledger_account_id": "L-rent"}, "already booked to Huur."),
            ({"project_id": "P-old"}, "already booked to Huur with that project"),
            ({"project_id": "P-new", "clear_project": True}, "cannot be combined"),
            ({"project_id": "P-arch"}, "is archived"),
        ]
        for extra, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(MoneybirdError, message):
                    self._prepare(BankBookingClient(), **extra)

    def test_project_that_did_not_land_fails_and_restores(self) -> None:
        client = BankBookingClient(store_project=False)
        _prepared, result = self._run(client, project_id="P-new")

        self.assertEqual(result["status"], "completed_with_errors")
        self.assertFalse(result["fully_verified"])
        restore = result["failures"][0]["restore"]
        self.assertTrue(restore["source_restored"])
        self.assertTrue(restore["new_target_removed"])
        self.assertFalse(restore["project_restored"])

    def test_restore_falls_back_to_ledger_and_amount(self) -> None:
        client = BankBookingClient(reject_project_links=True)
        _prepared, result = self._run(client, target_ledger_account_id="L-other")

        restore = result["failures"][0]["restore"]
        self.assertTrue(restore["source_restored"])
        self.assertFalse(restore["project_restored"])
        self.assertEqual(self._booking(client)["ledger_account_id"], "L-rent")
        self.assertEqual([link["booking_id"] for link in client.links], ["L-other", "L-rent", "L-rent"])
        self.assertNotIn("project_id", client.links[-1])

    def test_restore_keeps_the_project_when_the_link_accepts_it(self) -> None:
        client = BankBookingClient(fail_link_to="L-other")
        _prepared, result = self._run(client, target_ledger_account_id="L-other")

        restore = result["failures"][0]["restore"]
        self.assertTrue(restore["source_restored"])
        self.assertTrue(restore["project_restored"])
        self.assertTrue(restore["description_restored"])
        self.assertEqual(self._booking(client)["project_id"], "P-old")

    def test_project_changed_after_preview_blocks_execution(self) -> None:
        from moneybird_mcp.tools import bank

        client = BankBookingClient()
        with self._patch_client(bank, client):
            prepared = bank.prepare_reclassify_bank_mutation_bookings(
                [{**self.base, "target_ledger_account_id": "L-other"}]
            )
            client.mutation["ledger_account_bookings"][0]["project_id"] = "P-new"
            with self.assertRaisesRegex(MoneybirdError, "changed after the preview"):
                bank.reclassify_bank_mutation_bookings_from_approval(prepared["approval_id"])
        self.assertEqual(client.links, [])


class LinkBookingProjectTests(_WriteTestCase):
    def _client(self, **kwargs):
        client = BankBookingClient(**kwargs)
        client.mutation.update(
            {"state": "unprocessed", "amount_open": "-500.00", "ledger_account_bookings": []}
        )
        return client

    def _link(self, client, **kwargs):
        from moneybird_mcp.tools import bank

        with self._patch_client(bank, client):
            prepared = bank.prepare_link_bank_mutation_booking(
                financial_mutation_id="mut-1", booking_type="LedgerAccount", booking_id="L-rent", **kwargs
            )
            return prepared, bank.link_bank_mutation_booking_from_approval(prepared["approval_id"])

    def test_ledger_booking_gets_the_named_project(self) -> None:
        client = self._client()
        prepared, result = self._link(client, project_name="Renovatie")

        self.assertEqual(prepared["preview"]["project"], {"id": "P-new", "name": "Renovatie"})
        self.assertEqual(client.links[0]["project_id"], "P-new")
        self.assertTrue(result["verification"]["new_link_project_matches"])
        self.assertTrue(result["verification"]["fully_verified"])

    def test_project_that_did_not_land_fails_verification(self) -> None:
        _prepared, result = self._link(self._client(store_project=False), project_id="P-new")

        self.assertFalse(result["verification"]["new_link_project_matches"])
        self.assertFalse(result["verification"]["fully_verified"])

    def test_no_project_reads_no_projects(self) -> None:
        client = self._client()
        prepared, result = self._link(client)

        self.assertEqual(client.project_calls, 0)
        self.assertIsNone(prepared["preview"]["project"])
        self.assertNotIn("project_id", client.links[0])
        self.assertNotIn("new_link_project_matches", result["verification"])

    def test_refusals(self) -> None:
        from moneybird_mcp.tools import bank

        client = self._client()
        with self._patch_client(bank, client):
            with self.assertRaisesRegex(MoneybirdError, "only be set on a LedgerAccount booking"):
                bank.prepare_link_bank_mutation_booking(
                    financial_mutation_id="mut-1", booking_type="SalesInvoice", booking_id="inv-1", project_id="P-new"
                )
            with self.assertRaisesRegex(MoneybirdError, "is archived"):
                bank.prepare_link_bank_mutation_booking(
                    financial_mutation_id="mut-1", booking_type="LedgerAccount", booking_id="L-rent", project_id="P-arch"
                )


class SalesCreateClient(_ProjectsMixin):
    """Contacts with one earlier invoice whose line carries a project."""

    administration_id = "contract-admin"

    def __init__(self, *, keep_project=True):
        self.keep_project = keep_project
        self.created: list[dict] = []

    def get_contact_by_customer_id(self, customer_id):
        return {"id": f"contact-{customer_id}", "customer_id": customer_id, "company_name": customer_id}

    def get_contact(self, contact_id):
        return {"id": contact_id, "customer_id": contact_id.replace("contact-", "")}

    def list_sales_invoices(self, **kwargs):
        customer_id = str(kwargs.get("contact_id") or "").replace("contact-", "")
        return [
            {
                "id": f"invoice-{customer_id}",
                "invoice_id": "2026-0001",
                "invoice_date": "2026-04-16",
                "state": "paid",
                "contact_id": f"contact-{customer_id}",
                "contact": {"id": f"contact-{customer_id}", "customer_id": customer_id},
                "currency": "EUR",
                "prices_are_incl_tax": False,
                "details": [
                    {
                        "description": f"Elektra {customer_id} - 10,00 kWh",
                        "price": "0.34",
                        "tax_rate_id": "tax-21",
                        "ledger_account_id": "ledger-electricity",
                        "project_id": "P-old",
                    }
                ],
            }
        ]

    def list_tax_rates(self):
        return [{"id": "tax-21", "percentage": "21.0"}]

    def create_sales_invoice(self, payload):
        record = copy.deepcopy(payload)
        record["id"] = f"created-{len(self.created) + 1}"
        record["details"] = [
            {**line, "project_id": line.get("project_id") if self.keep_project else None}
            for line in record.pop("details_attributes")
        ]
        self.created.append(record)
        return copy.deepcopy(record)

    def get_sales_invoice(self, invoice_id):
        return copy.deepcopy(next(item for item in self.created if item["id"] == invoice_id))


def _invoice_line(**extra):
    return {"description": "Advies", "price": "100", "tax_rate_id": "tax-21", "ledger_account_id": "L-revenue", **extra}


class SalesCreateProjectTests(_WriteTestCase):
    def test_batch_reads_projects_once_and_sets_them_per_line(self) -> None:
        client = SalesCreateClient()
        prepared = sales_batches._prepare_batch_create_sales_invoices(
            client,
            [
                {"customer_id": "A", "details": [_invoice_line(project_name="Renovatie"), _invoice_line()]},
                {"customer_id": "B", "details": [_invoice_line(project_id="P-old")]},
            ],
        )

        self.assertEqual(client.project_calls, 1)
        items = prepared["payload"]["items"]
        self.assertEqual(
            [line.get("project_id") for line in items[0]["sales_invoice"]["details_attributes"]],
            ["P-new", None],
        )
        self.assertEqual(items[1]["sales_invoice"]["details_attributes"][0]["project_id"], "P-old")
        self.assertEqual(items[0]["preview_rows"][0]["project"], {"id": "P-new", "name": "Renovatie"})
        self.assertNotIn("project", items[0]["preview_rows"][1])

    def test_lines_without_a_project_read_no_projects(self) -> None:
        client = SalesCreateClient()
        prepared = sales_batches._prepare_batch_create_sales_invoices(
            client, [{"customer_id": "A", "details": [_invoice_line()]}]
        )
        self.assertEqual(client.project_calls, 0)
        self.assertNotIn("project_id", prepared["payload"]["items"][0]["sales_invoice"]["details_attributes"][0])

    def test_archived_project_is_refused_with_the_line(self) -> None:
        with self.assertRaisesRegex(MoneybirdError, r"entry\[0\].*Invoice line 'Advies': Project 'Archief' \(P-arch\) is archived"):
            sales_batches._prepare_batch_create_sales_invoices(
                SalesCreateClient(), [{"customer_id": "A", "details": [_invoice_line(project_id="P-arch")]}]
            )

    def _draft(self, client):
        from moneybird_mcp.tools import sales

        with self._patch_client(sales, client):
            prepared = sales.prepare_create_sales_invoice_draft("contact-A", [_invoice_line(project_name="Renovatie")])
            return prepared, sales.create_sales_invoice_draft_from_approval(prepared["approval_id"])

    def test_draft_sets_and_verifies_the_project(self) -> None:
        prepared, result = self._draft(SalesCreateClient())

        self.assertEqual(prepared["payload"]["details_attributes"][0]["project_id"], "P-new")
        self.assertEqual(prepared["preview"]["line_items"][0]["project"]["name"], "Renovatie")
        self.assertTrue(result["verification"]["fully_verified"])

    def test_draft_flags_a_project_that_did_not_land(self) -> None:
        _prepared, result = self._draft(SalesCreateClient(keep_project=False))

        self.assertFalse(result["verification"]["fully_verified"])
        self.assertTrue(result["verification"]["line_mismatches"])


class MeterUsageProjectTests(_WriteTestCase):
    rows = [
        {"meter": "B5", "usage_kwh": "100"},
        {"meter": "B8", "usage_kwh": "50", "project_id": "P-old"},
    ]

    def _run(self, client, **kwargs):
        with self._patch_client(sales_batches, client):
            return sales_batches.prepare_meter_usage_sales_invoices(
                rows=self.rows, period_label="2026-K3", invoice_date="2026-10-16", **kwargs
            )

    @staticmethod
    def _projects(prepared):
        return [
            item["sales_invoice"]["details_attributes"][0].get("project_id")
            for item in prepared["payload"]["items"]
        ]

    def test_project_is_never_inherited_from_the_previous_invoice(self) -> None:
        client = SalesCreateClient()
        with self._patch_client(sales_batches, client):
            prepared = sales_batches.prepare_meter_usage_sales_invoices(
                rows=[{"meter": "B5", "usage_kwh": "100"}], period_label="2026-K3", invoice_date="2026-10-16"
            )
        self.assertEqual(self._projects(prepared), [None])
        self.assertEqual(client.project_calls, 0)

    def test_default_project_applies_unless_the_row_names_one(self) -> None:
        client = SalesCreateClient()
        prepared = self._run(client, default_project_name="Renovatie")

        self.assertEqual(self._projects(prepared), ["P-new", "P-old"])
        self.assertEqual(client.project_calls, 1)
        decisions = prepared["meter_usage_preview"]["decisions"]
        self.assertEqual(decisions[0]["project_name"], "Renovatie")
        self.assertEqual(decisions[1]["project_id"], "P-old")

    def test_default_project_refusals(self) -> None:
        with self.assertRaisesRegex(MoneybirdError, "not both"):
            self._run(SalesCreateClient(), default_project_id="P-new", default_project_name="Renovatie")
        with self.assertRaisesRegex(MoneybirdError, "is archived"):
            self._run(SalesCreateClient(), default_project_id="P-arch")


if __name__ == "__main__":
    unittest.main()
