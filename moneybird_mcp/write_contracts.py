"""Reusable, fail-closed preconditions and post-write comparisons.

The Moneybird API adds response-only fields to most records, so a useful write
contract cannot compare whole JSON objects byte-for-byte.  These helpers compare
every caller-controlled field while deliberately ignoring provider metadata.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

from .config import MoneybirdError
from .formatting import money_decimal


@dataclass(frozen=True)
class WriteSpec:
    """Versioned declaration required for every exposed approval executor."""

    schema_version: int
    precondition: str
    verifier: str
    idempotency: str
    reconciliation: str


def _spec(
    precondition: str,
    verifier: str,
    idempotency: str,
    reconciliation: str,
) -> WriteSpec:
    return WriteSpec(1, precondition, verifier, idempotency, reconciliation)


WRITE_SPECS: dict[str, WriteSpec] = {
    "archive_contact": _spec(
        "contact version/updated_at/archive state",
        "independent contact GET proves archived",
        "contact occurrence snapshot",
        "inspect contact id and adopt success or prove no archive",
    ),
    "batch_create_sales_invoices": _spec(
        "duplicate/merge preview for every proposed invoice",
        "independent batch GET compares every controlled header and line",
        "canonical complete batch payload",
        "reconcile every returned or uniquely matched invoice as a child outcome",
    ),
    "batch_schedule_sales_invoices": _spec(
        "version/state/date/total snapshot for every invoice",
        "independent batch GET proves schedule date/state and unchanged total",
        "canonical batch plus source snapshots",
        "reconcile each invoice schedule state independently",
    ),
    "batch_update_sales_invoices": _spec(
        "complete-batch version or targeted-field preflight",
        "independent GET compares every patched header and line field",
        "canonical patches plus source snapshots",
        "reconcile each invoice and retain partial child outcomes",
    ),
    "bookkeeping_correction_batch": _spec(
        "all child action preconditions before first child",
        "all child WriteSpec verifiers must pass",
        "canonical ordered child payloads",
        "reconcile each child before resolving the parent",
    ),
    "create_contact": _spec(
        "provider id must be absent before dispatch and returned after create",
        "independent GET compares every requested contact field",
        "canonical contact payload",
        "search exact requested identity; zero/one/many means absent/adopt/manual",
    ),
    "create_credit_invoice": _spec(
        "original invoice version, total, and line snapshot",
        "independent GET proves draft, negated total, contact, currency, and lines",
        "original invoice occurrence snapshot",
        "match returned id or exact credit signature",
    ),
    "create_general_journal_document": _spec(
        "validated balanced entries and returned provider id",
        "independent GET compares header and every financial line field",
        "canonical complete journal payload",
        "match returned id/reference/date/entries; ambiguous matches stay manual",
    ),
    "create_ledger_account": _spec(
        "same-name preview and returned provider id",
        "independent GET compares every requested field",
        "canonical account and RGS payload",
        "match exact account attributes; ambiguous matches stay manual",
    ),
    "create_sales_invoice_draft": _spec(
        "contact scope validation and returned provider id",
        "independent GET compares every controlled header and line",
        "canonical complete invoice payload",
        "match returned id or exact draft signature",
    ),
    "link_bank_mutation_booking": _spec(
        "mutation occurrence/booking state and exact target occurrence",
        "independent GET proves one new matching booking id/type and amount-open delta",
        "mutation occurrence, target occurrence, and booking payload",
        "inspect mutation booking delta and adopt/absent/manual",
    ),
    "pause_sales_invoice_workflow": _spec(
        "invoice version/state/paused snapshot",
        "independent GET proves paused true",
        "invoice occurrence plus pause intent",
        "inspect current paused state and version history",
    ),
    "reclassify_bank_mutation_bookings": _spec(
        "all mutation versions, totals, and source bookings",
        "immediate plus independent batch GET verifies replacement bookings",
        "canonical child moves plus source versions",
        "retain each child and restoration outcome",
    ),
    "settle_vat_period": _spec(
        "period gross movements, administration lock, and existing settlements "
        "re-read immediately before dispatch and compared with the approved snapshot",
        "independent GET compares the journal payload and proves the period's gross "
        "VAT movements cleared to zero",
        "settled period identity plus canonical journal payload",
        "match returned id/reference/date/entries and inspect the period balance; "
        "ambiguous matches stay manual",
    ),
    "reclassify_document_lines": _spec(
        "complete-batch document versions or targeted-line snapshots",
        "independent GET compares every target ledger plus exact journal lines",
        "canonical updates/journals plus source snapshots",
        "retain each document/journal child result",
    ),
    "reconcile_purchase_invoice": _spec(
        "document version/updated_at and total",
        "independent GET proves total, tax mode, and complete line signature",
        "document occurrence plus exact desired lines",
        "inspect the target document id and exact desired signature",
    ),
    "register_payment": _spec(
        "version, total, open amount, and payment multiset",
        "independent GET proves exactly one requested payment delta",
        "payment intent plus source payment multiset",
        "compare before/after payment ids or multiset delta",
    ),
    "resume_sales_invoice_workflow": _spec(
        "invoice version/state/paused snapshot",
        "independent GET proves paused false",
        "invoice occurrence plus resume intent",
        "inspect current paused state and version history",
    ),
    "send_sales_invoice": _spec(
        "invoice version/state/date/total snapshot",
        "independent GET proves sent event/state or exact schedule",
        "invoice occurrence plus send payload",
        "inspect send events/state without automatically resending",
    ),
    "set_contacts_delivery_method_email": _spec(
        "complete-batch contact version/delivery snapshots",
        "independent GET proves Email for every selected contact",
        "canonical contacts plus source snapshots",
        "reconcile each selected contact independently",
    ),
    "unlink_bank_mutation_booking": _spec(
        "mutation occurrence version and exact booking",
        "independent GET proves that exact booking id is absent",
        "mutation occurrence plus booking id",
        "inspect the mutation and adopt/absent/manual",
    ),
    "update_contact": _spec(
        "contact version/updated_at and targeted fields",
        "independent GET compares every requested field",
        "contact occurrence plus update payload",
        "inspect contact id and requested fields",
    ),
}


def _attribute_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        def sort_key(item: tuple[Any, Any]) -> tuple[int, str]:
            key = str(item[0])
            return (int(key), key) if key.isdigit() else (2**31 - 1, key)

        return [
            dict(row)
            for _key, row in sorted(value.items(), key=sort_key)
            if isinstance(row, dict)
        ]
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, dict)]
    return []


def _actual_rows(record: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = record.get(key)
        if isinstance(value, (list, dict)):
            rows = _attribute_rows(value)
            if rows and all(row.get("row_order") not in (None, "") for row in rows):
                return sorted(rows, key=lambda row: int(row["row_order"]))
            return rows
    return []


def _normalized_decimal(value: Any) -> Decimal | str:
    try:
        return money_decimal(value or 0)
    except Exception:
        return f"<invalid:{value!r}>"


def _normalized_value(field: str, value: Any, *, decimal_fields: set[str]) -> Any:
    if field in decimal_fields:
        return _normalized_decimal(value)
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return ""
    return str(value)


def compare_controlled_fields(
    expected: dict[str, Any],
    actual: dict[str, Any],
    *,
    fields: Iterable[str],
    decimal_fields: Iterable[str] = (),
) -> dict[str, dict[str, Any]]:
    """Return mismatches for expected fields that the caller actually supplied."""

    decimal_field_set = set(decimal_fields)
    mismatches: dict[str, dict[str, Any]] = {}
    for field in fields:
        if field not in expected:
            continue
        expected_value = _normalized_value(
            field,
            expected.get(field),
            decimal_fields=decimal_field_set,
        )
        actual_value = _normalized_value(
            field,
            actual.get(field),
            decimal_fields=decimal_field_set,
        )
        if actual_value != expected_value:
            mismatches[field] = {
                "expected": expected.get(field),
                "actual": actual.get(field),
            }
    return mismatches


def compare_controlled_rows(
    expected_rows: Any,
    actual_rows: Any,
    *,
    fields: Iterable[str],
    decimal_fields: Iterable[str] = (),
    match_by_id: bool = False,
) -> list[dict[str, Any]]:
    """Compare every supplied field on every expected financial line."""

    expected = _attribute_rows(expected_rows)
    actual = _attribute_rows(actual_rows)
    mismatches: list[dict[str, Any]] = []
    actual_by_id = {
        str(row.get("id")): row
        for row in actual
        if row.get("id") not in (None, "")
    }

    if not match_by_id and len(expected) != len(actual):
        mismatches.append(
            {
                "field": "line_count",
                "expected": len(expected),
                "actual": len(actual),
            }
        )

    for index, expected_row in enumerate(expected):
        expected_id = str(expected_row.get("id") or "")
        if match_by_id and expected_id:
            actual_row = actual_by_id.get(expected_id)
        else:
            actual_row = actual[index] if index < len(actual) else None
        if actual_row is None:
            mismatches.append(
                {
                    "line": index,
                    "id": expected_id or None,
                    "field": "line",
                    "expected": expected_row,
                    "actual": None,
                }
            )
            continue
        row_mismatches = compare_controlled_fields(
            expected_row,
            actual_row,
            fields=fields,
            decimal_fields=decimal_fields,
        )
        for field, mismatch in row_mismatches.items():
            mismatches.append(
                {
                    "line": index,
                    "id": expected_id or actual_row.get("id"),
                    "field": field,
                    **mismatch,
                }
            )
    return mismatches


SALES_INVOICE_LINE_FIELDS = (
    "id",
    "description",
    "period",
    "price",
    "amount",
    "tax_rate_id",
    "ledger_account_id",
    "product_id",
)
SALES_INVOICE_LINE_DECIMALS = ("price", "amount")

GENERAL_JOURNAL_LINE_FIELDS = (
    "ledger_account_id",
    "tax_rate_id",
    "description",
    "contact_id",
    "project_id",
    "debit",
    "credit",
)
GENERAL_JOURNAL_LINE_DECIMALS = ("debit", "credit")


def verify_sales_invoice_payload(
    expected: dict[str, Any],
    record: dict[str, Any],
) -> dict[str, Any]:
    comparable_record = dict(record)
    if not comparable_record.get("contact_id"):
        comparable_record["contact_id"] = (
            comparable_record.get("contact") or {}
        ).get("id")
    field_mismatches = compare_controlled_fields(
        expected,
        comparable_record,
        fields=(
            "contact_id",
            "reference",
            "invoice_date",
            "due_date",
            "currency",
            "workflow_id",
            "document_style_id",
            "identity_id",
            "language",
            "prices_are_incl_tax",
        ),
    )
    line_mismatches = compare_controlled_rows(
        expected.get("details_attributes"),
        _actual_rows(record, "details"),
        fields=SALES_INVOICE_LINE_FIELDS,
        decimal_fields=SALES_INVOICE_LINE_DECIMALS,
    )
    return {
        "field_mismatches": field_mismatches,
        "line_mismatches": line_mismatches,
        "fully_verified": not field_mismatches and not line_mismatches,
    }


def verify_general_journal_payload(
    expected: dict[str, Any],
    record: dict[str, Any],
) -> dict[str, Any]:
    field_mismatches = compare_controlled_fields(
        expected,
        record,
        fields=("reference", "date", "description"),
    )
    line_mismatches = compare_controlled_rows(
        expected.get("general_journal_document_entries_attributes"),
        _actual_rows(record, "general_journal_document_entries", "details"),
        fields=GENERAL_JOURNAL_LINE_FIELDS,
        decimal_fields=GENERAL_JOURNAL_LINE_DECIMALS,
    )
    return {
        "field_mismatches": field_mismatches,
        "line_mismatches": line_mismatches,
        "fully_verified": not field_mismatches and not line_mismatches,
    }


def build_patch_precondition(
    record: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Capture a version or the exact targeted values before an update."""

    header_fields = [
        field
        for field in patch
        if field != "details_attributes"
    ]
    patch_details = _attribute_rows(patch.get("details_attributes"))
    actual_details = {
        str(row.get("id")): row
        for row in _actual_rows(record, "details")
        if row.get("id") not in (None, "")
    }
    target_details: dict[str, dict[str, Any] | None] = {}
    for patch_detail in patch_details:
        detail_id = str(patch_detail.get("id") or "")
        if not detail_id:
            continue
        actual_detail = actual_details.get(detail_id)
        target_details[detail_id] = (
            {
                field: actual_detail.get(field)
                for field in patch_detail
                if field != "id"
            }
            if actual_detail is not None
            else None
        )
    return {
        "version": str(record.get("version") or ""),
        "updated_at": str(record.get("updated_at") or ""),
        "target_fields": {
            field: record.get(field)
            for field in header_fields
        },
        "target_details": target_details,
    }


def assert_patch_precondition(
    current: dict[str, Any],
    expected: dict[str, Any],
    *,
    record_label: str,
) -> None:
    if not expected:
        raise MoneybirdError(
            f"{record_label} approval predates exact update preconditions. Prepare again."
        )
    expected_version = str(expected.get("version") or "")
    current_version = str(current.get("version") or "")
    if expected_version and current_version != expected_version:
        raise MoneybirdError(
            f"{record_label} changed after preview "
            f"(version {expected_version} -> {current_version}). Prepare again."
        )
    expected_updated_at = str(expected.get("updated_at") or "")
    current_updated_at = str(current.get("updated_at") or "")
    if (
        not expected_version
        and expected_updated_at
        and current_updated_at != expected_updated_at
    ):
        raise MoneybirdError(
            f"{record_label} changed after preview "
            f"(updated_at {expected_updated_at} -> {current_updated_at}). "
            "Prepare again."
        )
    if expected_version or expected_updated_at:
        return

    for field, value in (expected.get("target_fields") or {}).items():
        if _normalized_value(field, current.get(field), decimal_fields=set()) != (
            _normalized_value(field, value, decimal_fields=set())
        ):
            raise MoneybirdError(
                f"{record_label} field {field} changed after preview. Prepare again."
            )
    actual_details = {
        str(row.get("id")): row
        for row in _actual_rows(current, "details")
        if row.get("id") not in (None, "")
    }
    for detail_id, expected_detail in (expected.get("target_details") or {}).items():
        actual_detail = actual_details.get(str(detail_id))
        if expected_detail is None or actual_detail is None:
            raise MoneybirdError(
                f"{record_label} detail {detail_id} changed after preview. Prepare again."
            )
        for field, value in expected_detail.items():
            decimal_fields = set(SALES_INVOICE_LINE_DECIMALS)
            if _normalized_value(
                field,
                actual_detail.get(field),
                decimal_fields=decimal_fields,
            ) != _normalized_value(field, value, decimal_fields=decimal_fields):
                raise MoneybirdError(
                    f"{record_label} detail {detail_id} field {field} changed "
                    "after preview. Prepare again."
                )


def verify_sales_invoice_patch(
    patch: dict[str, Any],
    record: dict[str, Any],
) -> dict[str, Any]:
    field_mismatches = compare_controlled_fields(
        patch,
        record,
        fields=("reference", "invoice_date", "due_date"),
    )
    line_mismatches = compare_controlled_rows(
        patch.get("details_attributes"),
        _actual_rows(record, "details"),
        fields=SALES_INVOICE_LINE_FIELDS,
        decimal_fields=SALES_INVOICE_LINE_DECIMALS,
        match_by_id=True,
    )
    return {
        "field_mismatches": field_mismatches,
        "line_mismatches": line_mismatches,
        "fully_verified": not field_mismatches and not line_mismatches,
    }


def verify_document_reclassification(
    expected_details: Any,
    record: dict[str, Any],
) -> dict[str, Any]:
    line_mismatches = compare_controlled_rows(
        expected_details,
        _actual_rows(record, "details"),
        fields=("id", "ledger_account_id"),
        match_by_id=True,
    )
    return {
        "line_mismatches": line_mismatches,
        "fully_verified": not line_mismatches,
    }
