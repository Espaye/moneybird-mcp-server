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
            ({**base}, "a project_id or project_name, or both"),
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

    def _execute(self, client):
        entry = {"document_kind": "purchase_invoice", "document_id": "doc-1", "detail_id": "d1", "project_id": "P-new"}
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

    def __init__(self, *, keep_project=True):
        self.keep_project = keep_project
        self.update_calls = 0
        self.record = {
            "id": "invoice-1",
            "invoice_id": "2026-001",
            "version": 1,
            "state": "draft",
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

    def get_sales_invoice(self, _invoice_id):
        return copy.deepcopy(self.record)

    def update_sales_invoice(self, _invoice_id, patch):
        self.update_calls += 1
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

    def test_older_approval_without_project_expectation_still_executes(self) -> None:
        client = ReconcileClient([_target_with_projects()])
        payload = self._build(client)["payload"]
        payload.pop("expected_line_projects")

        result = self._execute(client, payload)

        self.assertTrue(result["verified_line_projects_match"])
        self.assertEqual(result["_status"], "completed")


if __name__ == "__main__":
    unittest.main()
