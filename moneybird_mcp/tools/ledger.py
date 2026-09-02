"""Ledger writes: ledger accounts, general journal documents, document-line reclassification."""
from __future__ import annotations

from datetime import date as date_type
from typing import Annotated, Any

from pydantic import Field

from .. import reference_cache
from ..capabilities import require_write_capability
from ..config import (
    PREPARE_ANNOTATIONS,
    READ_ONLY_ANNOTATIONS,
    MoneybirdError,
    MoneybirdHTTPError,
)
from ..formatting import (
    clean_dict,
    compact_general_journal_summary,
    compact_ledger_account_summary,
    duplicate_fingerprint,
    iso_now,
    money_decimal,
    year_period_for_date,
)
from ..invoicing import (
    details_attributes_payload,
    prepare_general_journal_entries,
    prepare_reclassification_batch,
)
from ..safety import (
    approval_execution_state,
    classify_failed_write,
    make_approval,
    pop_approval,
    record_approval_outcome,
    record_approval_phase,
)
from ..vat_settlement import (
    build_vat_settlement_journal,
    compare_gross_to_reported,
    count_rubrieken,
    find_vat_settlement_journals,
    ledger_movements_from_report,
    month_periods,
    period_end_date,
    reported_vat_totals,
    resolve_vat_accounts,
    settlement_preflight,
    validate_declared_amount,
)
from ..write_contracts import (
    assert_patch_precondition,
    verify_document_reclassification,
    verify_general_journal_payload,
)
from . import _context as ctx
from ._params import ApprovalId, DateString, VatSettlementPeriod
from ._registry import mcp
from ._writes import (
    mark_write_dispatch_started,
    mark_write_verifying,
    run_approved_write,
    stage_write,
)


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_create_ledger_account(
    name: Annotated[str, Field(description="Ledger account (category) name as it should appear in reports.")],
    account_type: Annotated[str, Field(description="Moneybird account_type, e.g. 'expenses', 'revenue', 'direct_costs', 'current_assets', 'non_current_assets', 'current_liabilities', 'equity', 'other_income_expenses'.")],
    rgs_code: Annotated[str, Field(description="Required Dutch RGS 3.5 taxonomy code, for example 'WBedAlkOal'. Use list_ledger_accounts to inspect taxonomy codes already used by this administration.")],
    account_id: Annotated[str, Field(description="Optional ledger account number (grootboeknummer).")] = "",
    active: bool = True,
) -> dict[str, Any]:
    """Use this before creating a Moneybird ledger account. Do not execute the write until the user explicitly confirms."""
    if not name.strip():
        raise MoneybirdError("name is required.")
    if not account_type.strip():
        raise MoneybirdError("account_type is required.")
    if not rgs_code.strip():
        raise MoneybirdError(
            "rgs_code is required by Moneybird. Pass an RGS 3.5 taxonomy code such "
            "as 'WBedAlkOal'; list_ledger_accounts exposes taxonomy codes already "
            "used by this administration."
        )

    client = ctx.get_client()
    payload = clean_dict(
        {
            "name": name.strip(),
            "account_type": account_type.strip(),
            "account_id": account_id.strip(),
            "active": active,
        }
    )
    fingerprint = duplicate_fingerprint(
        "create_ledger_account",
        {"ledger_account": payload, "rgs_code": rgs_code.strip()},
    )
    existing_matches = [
        compact_ledger_account_summary(item)
        for item in client.list_ledger_accounts()
        if str(item.get("name") or "") == name.strip()
    ]
    return stage_write(
        "create_ledger_account",
        summary=f"Create ledger account '{name.strip()}'",
        payload={
            "ledger_account": payload,
            "rgs_code": rgs_code.strip(),
        },
        preview={
            "ledger_account": payload,
            "rgs_code": rgs_code.strip(),
            "existing_name_matches": existing_matches,
        },
        fingerprint=fingerprint,
    )


def _execute_create_ledger_account(client, payload: dict[str, Any]) -> dict[str, Any]:
    mark_write_dispatch_started()
    created = client.create_ledger_account(
        payload["ledger_account"],
        rgs_code=payload.get("rgs_code", ""),
    )
    # The new account must be visible to the very next read, so drop the cached
    # reference list immediately rather than waiting out its TTL. Verification
    # below re-reads the record itself, not the cached collection. This is an
    # optimisation, so it never depends on the client exposing an id.
    reference_cache.invalidate_administration(
        getattr(client, "administration_id", None)
    )
    record_id = str(created.get("id") or "")
    if not record_id:
        raise MoneybirdError(
            "Moneybird did not return a ledger account id; reconcile before retrying."
        )
    mark_write_verifying()
    record = client.get_ledger_account(record_id)
    expected = payload["ledger_account"]
    mismatches = {
        key: {"expected": value, "actual": record.get(key)}
        for key, value in expected.items()
        if str(record.get(key) or "") != str(value or "")
    }
    expected_rgs_code = str(payload["rgs_code"])
    taxonomy_item = record.get("taxonomy_item") or {}
    actual_rgs_code = str(taxonomy_item.get("code") or "")
    if actual_rgs_code != expected_rgs_code:
        mismatches["rgs_code"] = {
            "expected": expected_rgs_code,
            "actual": actual_rgs_code,
        }
    record_id_matches = str(record.get("id") or "") == record_id
    fully_verified = record_id_matches and not mismatches
    return {
        "_status": (
            "created" if fully_verified else "completed_with_verification_errors"
        ),
        "_audit_result": (
            "success" if fully_verified else "verification_failed"
        ),
        "_audit": {
            "ledger_account_id": record_id,
            "name": record.get("name"),
            "fully_verified": fully_verified,
        },
        "ledger_account": compact_ledger_account_summary(record),
        "verification": {
            "independent_post_read": True,
            "record_id_matches": record_id_matches,
            "field_mismatches": mismatches,
            "fully_verified": fully_verified,
        },
    }


# Not registered as an MCP tool: every approved action executes through the single
# annotated execute_approved_action entry point. Kept as a Python function because
# tools/approvals.py dispatches to it and scripts/tests call it directly.
def create_ledger_account_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared ledger account creation."""
    client = ctx.get_client()
    return run_approved_write(
        client, approval_id, "create_ledger_account", _execute_create_ledger_account
    )


def _ledger_account_occurrence(record: dict[str, Any]) -> dict[str, Any]:
    taxonomy = record.get("taxonomy_item") or {}
    return {
        "id": str(record.get("id") or ""),
        "name": record.get("name"),
        "account_type": record.get("account_type"),
        "account_id": str(record.get("account_id") or ""),
        "parent_id": str(record.get("parent_id") or ""),
        "active": bool(record.get("active", True)),
        "rgs_code": str(taxonomy.get("code") or ""),
        "created_at": str(record.get("created_at") or ""),
        "updated_at": str(record.get("updated_at") or ""),
    }


def _ledger_account_active_months(record: dict[str, Any]) -> list[str]:
    created_at = str(record.get("created_at") or "")
    try:
        cursor = date_type.fromisoformat(created_at[:10]).replace(day=1)
    except ValueError as exc:
        raise MoneybirdError(
            "Ledger account has no valid created_at date; refuse destructive cleanup."
        ) from exc
    end = date_type.today().replace(day=1)
    periods: list[str] = []
    while cursor <= end:
        periods.append(f"{cursor.year:04d}{cursor.month:02d}")
        if len(periods) > 12:
            raise MoneybirdError(
                "Empty-ledger deletion supports at most 12 months of complete booking "
                "evidence; audit this older account separately."
            )
        cursor = (
            cursor.replace(year=cursor.year + 1, month=1)
            if cursor.month == 12
            else cursor.replace(month=cursor.month + 1)
        )
    return periods


def _empty_ledger_account_evidence(
    client: Any, record: dict[str, Any]
) -> dict[str, Any]:
    ledger_account_id = str(record.get("id") or "")
    referencing_assets = [
        {
            "id": str(asset.get("id") or ""),
            "name": asset.get("name"),
        }
        for asset in client.list_all_assets(active=False)
        if str(asset.get("ledger_account_id") or "") == ledger_account_id
    ]
    periods = _ledger_account_active_months(record)
    entries_by_period: dict[str, list[dict[str, Any]]] = {}
    for period in periods:
        entries = client.get_report(
            "journal_entries",
            period=period,
            page=1,
            extra_query={"ledger_account_id": ledger_account_id},
        )
        entries_by_period[period] = list(entries or [])
    entry_count = sum(len(items) for items in entries_by_period.values())
    return {
        "periods_checked": periods,
        "entries_by_period": entries_by_period,
        "entry_count": entry_count,
        "balance": "0.00" if entry_count == 0 else None,
        "balance_is_exact_zero": entry_count == 0,
        "referencing_assets": referencing_assets,
        "referencing_asset_count": len(referencing_assets),
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_delete_empty_ledger_account(
    ledger_account_id: Annotated[
        str, Field(description="Exact Moneybird ledger-account id to remove or deactivate.")
    ],
    expected_name: Annotated[
        str, Field(description="Exact current account name, required as an identity guard.")
    ],
    expected_created_date: DateString,
    test_provenance: Annotated[
        str,
        Field(description="Audit evidence explaining why this account is test-only."),
    ],
) -> dict[str, Any]:
    """Preview provider-supported removal of an empty, recently created test ledger.

    The preflight proves zero journal entries over every month since creation and no
    asset references. Moneybird may physically delete or merely deactivate a ledger;
    either outcome is read back independently and reported exactly.
    """
    client = ctx.get_client()
    record = client.get_ledger_account(ledger_account_id.strip())
    occurrence = _ledger_account_occurrence(record)
    if str(record.get("name") or "") != expected_name.strip():
        raise MoneybirdError("Ledger account name does not match expected_name.")
    if str(record.get("created_at") or "")[:10] != expected_created_date:
        raise MoneybirdError(
            "Ledger account creation date does not match expected_created_date."
        )
    if not test_provenance.strip():
        raise MoneybirdError("test_provenance is required.")
    evidence = _empty_ledger_account_evidence(client, record)
    if evidence["referencing_asset_count"]:
        raise MoneybirdError("Ledger account is still referenced by one or more assets.")
    if evidence["entry_count"]:
        raise MoneybirdError(
            "Ledger account has journal entries and is not eligible for empty cleanup."
        )
    payload = {
        "ledger_account_occurrence": occurrence,
        "evidence": evidence,
        "expected_created_date": expected_created_date,
        "test_provenance": test_provenance.strip(),
    }
    return stage_write(
        "delete_empty_ledger_account",
        summary=(
            f"Remove or deactivate empty test ledger '{record.get('name')}' "
            f"({occurrence['id']})"
        ),
        payload=payload,
        preview={
            "ledger_account": record,
            "eligibility": {
                **evidence,
                "created_date_matches": True,
                "test_provenance": test_provenance.strip(),
            },
            "planned_api_actions": [
                "GET /ledger_accounts/{id} precondition",
                "GET /assets complete reference check",
                "GET /reports/journal_entries for every month since creation",
                "DELETE /ledger_accounts/{id}",
                "GET /ledger_accounts/{id} independent removal/deactivation read-back",
            ],
        },
        fingerprint=duplicate_fingerprint("delete_empty_ledger_account", payload),
    )


def _execute_delete_empty_ledger_account(
    client: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    occurrence = payload["ledger_account_occurrence"]
    ledger_account_id = str(occurrence["id"])
    current = client.get_ledger_account(ledger_account_id)
    if _ledger_account_occurrence(current) != occurrence:
        return {
            "_status": "precondition_failed",
            "_audit_result": "failed_pre_write",
            "_audit": {"ledger_account_id": ledger_account_id},
            "error": "The ledger account changed after preview; no write was sent.",
        }
    evidence = _empty_ledger_account_evidence(client, current)
    if evidence != payload["evidence"]:
        return {
            "_status": "precondition_failed",
            "_audit_result": "failed_pre_write",
            "_audit": {"ledger_account_id": ledger_account_id},
            "error": "Empty-ledger evidence changed after preview; no write was sent.",
        }

    mark_write_dispatch_started()
    client.delete_ledger_account(ledger_account_id)
    reference_cache.invalidate_administration(
        getattr(client, "administration_id", None)
    )
    mark_write_verifying()
    try:
        after = client.get_ledger_account(ledger_account_id)
    except MoneybirdHTTPError as exc:
        if exc.status_code != 404:
            raise
        outcome = "deleted"
        after_summary = None
        fully_verified = True
    else:
        after_occurrence = _ledger_account_occurrence(after)
        invariant_keys = (
            "id",
            "name",
            "account_type",
            "account_id",
            "parent_id",
            "rgs_code",
            "created_at",
        )
        invariants_match = all(
            after_occurrence[key] == occurrence[key] for key in invariant_keys
        )
        fully_verified = invariants_match and not after_occurrence["active"]
        outcome = "deactivated" if fully_verified else "verification_failed"
        after_summary = compact_ledger_account_summary(after)
    return {
        "_status": outcome,
        "_audit_result": "success" if fully_verified else "verification_failed",
        "_audit": {
            "ledger_account_id": ledger_account_id,
            "name": occurrence.get("name"),
            "outcome": outcome,
            "fully_verified": fully_verified,
        },
        "ledger_account": after_summary,
        "verification": {
            "independent_post_read": True,
            "provider_outcome": outcome,
            "asset_reference_count_before_delete": evidence[
                "referencing_asset_count"
            ],
            "journal_entry_count_before_delete": evidence["entry_count"],
            "balance_before_delete": evidence["balance"],
            "fully_verified": fully_verified,
        },
    }


# Not registered as an MCP tool: every approved action executes through the single
# annotated execute_approved_action entry point.
def delete_empty_ledger_account_from_approval(
    approval_id: ApprovalId,
) -> dict[str, Any]:
    """Execute one approved empty test-ledger cleanup."""
    return run_approved_write(
        ctx.get_client(),
        approval_id,
        "delete_empty_ledger_account",
        _execute_delete_empty_ledger_account,
    )


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_update_ledger_account(
    ledger_account_id: Annotated[
        str, Field(description="Exact Moneybird ledger-account id to update.")
    ],
    rgs_code: Annotated[
        str,
        Field(
            description="Existing Dutch RGS 3.5 code to assign, for example BMvaBegVvp."
        ),
    ],
    name: Annotated[
        str, Field(description="Optional replacement name; leave empty to preserve it.")
    ] = "",
) -> dict[str, Any]:
    """Preview a guarded ledger-account taxonomy/name correction."""
    if not ledger_account_id.strip():
        raise MoneybirdError("ledger_account_id is required.")
    if not rgs_code.strip():
        raise MoneybirdError("rgs_code is required.")
    client = ctx.get_client()
    current = client.get_ledger_account(ledger_account_id.strip())
    occurrence = _ledger_account_occurrence(current)
    ledger_patch = {"name": name.strip()} if name.strip() else {}
    payload = {
        "ledger_account_occurrence": occurrence,
        "ledger_account": ledger_patch,
        "rgs_code": rgs_code.strip(),
    }
    return stage_write(
        "update_ledger_account",
        summary=(
            f"Update ledger account '{current.get('name')}' RGS code from "
            f"{occurrence['rgs_code'] or '(missing)'} to {rgs_code.strip()}"
        ),
        payload=payload,
        preview={
            "before": compact_ledger_account_summary(current),
            "changes": {
                "rgs_code": rgs_code.strip(),
                **({"name": name.strip()} if name.strip() else {}),
            },
            "planned_api_actions": [
                "GET /ledger_accounts/{id} precondition",
                "PATCH /ledger_accounts/{id}",
                "GET /ledger_accounts/{id} read-after-write",
            ],
        },
        fingerprint=duplicate_fingerprint("update_ledger_account", payload),
    )


def _execute_update_ledger_account(
    client: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    occurrence = payload["ledger_account_occurrence"]
    ledger_account_id = str(occurrence["id"])
    before = client.get_ledger_account(ledger_account_id)
    if _ledger_account_occurrence(before) != occurrence:
        return {
            "_status": "precondition_failed",
            "_audit_result": "failed_pre_write",
            "_audit": {"ledger_account_id": ledger_account_id},
            "error": "The ledger account changed after preview; no write was sent.",
        }

    mark_write_dispatch_started()
    client.update_ledger_account(
        ledger_account_id,
        payload.get("ledger_account") or None,
        rgs_code=str(payload["rgs_code"]),
    )
    mark_write_verifying()
    record = client.get_ledger_account(ledger_account_id)
    after = _ledger_account_occurrence(record)
    expected_name = (payload.get("ledger_account") or {}).get(
        "name", occurrence["name"]
    )
    controlled_matches = (
        after["id"] == ledger_account_id
        and after["name"] == expected_name
        and after["rgs_code"] == str(payload["rgs_code"])
    )
    unchanged_fields = {
        key: after[key] == occurrence[key]
        for key in ("account_type", "account_id", "parent_id", "active")
    }
    fully_verified = controlled_matches and all(unchanged_fields.values())
    return {
        "_status": "updated" if fully_verified else "verification_failed",
        "_audit_result": "success" if fully_verified else "verification_failed",
        "_audit": {
            "ledger_account_id": ledger_account_id,
            "rgs_code": after["rgs_code"],
            "fully_verified": fully_verified,
        },
        "ledger_account": compact_ledger_account_summary(record),
        "verification": {
            "independent_post_read": True,
            "rgs_code_expected": str(payload["rgs_code"]),
            "rgs_code_actual": after["rgs_code"],
            "name_expected": expected_name,
            "name_actual": after["name"],
            "unchanged_fields": unchanged_fields,
            "fully_verified": fully_verified,
        },
    }


# Not registered as an MCP tool: every approved action executes through the single
# annotated execute_approved_action entry point.
def update_ledger_account_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Execute one explicitly approved ledger-account taxonomy/name correction."""
    return run_approved_write(
        ctx.get_client(),
        approval_id,
        "update_ledger_account",
        _execute_update_ledger_account,
    )


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_create_general_journal_document(
    reference: Annotated[str, Field(description="Short reference/title for the journal entry (memoriaal).")],
    date: DateString,
    entries: Annotated[
        list[dict[str, Any]],
        Field(description="Journal lines. Each dict: ledger_account_id or ledger_account_name, debit and/or credit (decimal strings), and optional description, contact_id, project_id, tax_rate_id. Total debit must equal total credit."),
    ],
    description: Annotated[str, Field(description="Shared description. Moneybird stores no header text on a journal document, so this is applied to every line that has no description of its own.")] = "",
) -> dict[str, Any]:
    """Use this before creating a Moneybird general journal document. Dutch: memoriaalboeking maken or memoriaal boeken. Do not execute the write until the user explicitly confirms."""
    if not reference.strip():
        raise MoneybirdError("reference is required.")
    if not date.strip():
        raise MoneybirdError("date is required.")

    client = ctx.get_client()
    # Moneybird stores no header description on a general journal document: the
    # field is absent from the returned record (live-verified 2026-08-01). Sending
    # one made the post-write verifier fail on every journal that used it, so the
    # text falls through to the lines instead of being silently dropped.
    entries = [
        {**entry, "description": entry.get("description") or description.strip()}
        if description.strip()
        else entry
        for entry in entries
    ]
    prepared = prepare_general_journal_entries(client, entries)
    payload = clean_dict(
        {
            "reference": reference.strip(),
            "date": date.strip(),
            "general_journal_document_entries_attributes": details_attributes_payload(
                prepared["entries"]
            ),
        }
    )
    fingerprint = duplicate_fingerprint(
        "create_general_journal_document",
        {"general_journal_document": payload},
    )
    return stage_write(
        "create_general_journal_document",
        summary=f"Create general journal document '{reference.strip()}'",
        payload={"general_journal_document": payload},
        preview={
            "reference": reference.strip(),
            "date": date.strip(),
            "description_applied_to_lines": description.strip(),
            "entries": prepared["preview_entries"],
            "total_debit": prepared["total_debit"],
            "total_credit": prepared["total_credit"],
        },
        fingerprint=fingerprint,
    )


def _execute_create_general_journal(client, payload: dict[str, Any]) -> dict[str, Any]:
    expected = payload["general_journal_document"]
    mark_write_dispatch_started()
    created = client.create_general_journal_document(expected)
    record_id = str(created.get("id") or "")
    if not record_id:
        raise MoneybirdError(
            "Moneybird did not return a general journal document id; "
            "reconcile before retrying."
        )
    mark_write_verifying()
    record = client.get_document("general_journal_document", record_id)
    verification = verify_general_journal_payload(expected, record)
    record_id_matches = str(record.get("id") or "") == record_id
    fully_verified = record_id_matches and verification["fully_verified"]
    return {
        "_status": (
            "created" if fully_verified else "completed_with_verification_errors"
        ),
        "_audit_result": (
            "success" if fully_verified else "verification_failed"
        ),
        "_audit": {
            "general_journal_document_id": record_id,
            "reference": record.get("reference"),
            "fully_verified": fully_verified,
        },
        "general_journal_document": compact_general_journal_summary(
            record,
            client.administration_id,
        ),
        "verification": {
            "independent_post_read": True,
            "record_id_matches": record_id_matches,
            **verification,
        },
    }


# Not registered as an MCP tool: every approved action executes through the single
# annotated execute_approved_action entry point. Kept as a Python function because
# tools/approvals.py dispatches to it and scripts/tests call it directly.
def create_general_journal_document_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared general journal creation."""
    client = ctx.get_client()
    return run_approved_write(
        client,
        approval_id,
        "create_general_journal_document",
        _execute_create_general_journal,
    )


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_reclassify_document_lines(
    entries: Annotated[
        list[dict[str, Any]],
        Field(description="Line moves. Each dict: document_kind ('purchase_invoice' or 'receipt'), document_id, detail_id (or row_order) to pick the line, and target ledger_account_id or ledger_account_name."),
    ],
) -> dict[str, Any]:
    """Use this before reclassifying purchase invoice or receipt lines to other ledger accounts. It can optionally prepare balancing general journal documents for asset or liability moves."""
    client = ctx.get_client()
    prepared = prepare_reclassification_batch(client, entries)
    approval = make_approval(
        "reclassify_document_lines",
        prepared["payload"],
        f"Reclassify {prepared['preview']['line_count']} document line(s)",
    )
    approval["payload"] = prepared["payload"]
    approval["preview"] = prepared["preview"]
    return approval


# Not registered as an MCP tool: every approved action executes through the single
# annotated execute_approved_action entry point. Kept as a Python function because
# tools/approvals.py dispatches to it and scripts/tests call it directly.
def reclassify_document_lines_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared document line reclassification."""
    client = ctx.get_client()
    require_write_capability(action="reclassify_document_lines")
    pending = pop_approval(approval_id, "reclassify_document_lines", administration_id=client.administration_id)
    payload = pending["payload"]
    fingerprint = payload["fingerprint"]
    if ctx.audit_log_contains_success("reclassify_document_lines", fingerprint):
        record_approval_outcome(
            approval_id,
            "duplicate_suppressed",
            administration_id=client.administration_id,
        )
        raise MoneybirdError(
            "This document reclassification payload already completed successfully according to the local audit log."
        )

    updated_documents: list[dict[str, Any]] = []
    created_general_journal_documents: list[dict[str, Any]] = []
    verification: list[dict[str, Any]] = []
    writes_applied = 0
    try:
        for item in payload["document_updates"]:
            before = client.get_document(
                item["document_kind"],
                item["document_id"],
            )
            assert_patch_precondition(
                before,
                item.get("precondition") or {},
                record_label=(
                    f"{item['document_kind']} document {item['document_id']}"
                ),
            )

        record_approval_phase(
            approval_id,
            "dispatching",
            administration_id=client.administration_id,
        )
        for item in payload["document_updates"]:
            client.update_document(
                item["document_kind"],
                item["document_id"],
                {
                    "details_attributes": details_attributes_payload(
                        item["details_attributes"]
                    )
                },
            )
            writes_applied += 1
            record = client.get_document(
                item["document_kind"],
                item["document_id"],
            )
            checked = verify_document_reclassification(
                item["details_attributes"],
                record,
            )
            updated_documents.append(
                {
                    "document_kind": item["document_kind"],
                    "document_id": str(record.get("id")),
                    "reference": record.get("reference"),
                    "version": record.get("version"),
                }
            )
            verification.append(
                {
                    "operation": "document_update",
                    "document_kind": item["document_kind"],
                    "document_id": item["document_id"],
                    **checked,
                }
            )

        for item in payload["general_journal_documents"]:
            created = client.create_general_journal_document(
                item["general_journal_document"]
            )
            writes_applied += 1
            record_id = str(created.get("id") or "")
            if not record_id:
                raise MoneybirdError(
                    "Moneybird did not return a general journal document id; "
                    "reconcile before retrying."
                )
            record = client.get_document(
                "general_journal_document",
                record_id,
            )
            checked = verify_general_journal_payload(
                item["general_journal_document"],
                record,
            )
            created_general_journal_documents.append(
                {
                    "general_journal_document_id": record_id,
                    "reference": record.get("reference"),
                    "date": record.get("date"),
                }
            )
            verification.append(
                {
                    "operation": "general_journal_create",
                    "general_journal_document_id": record_id,
                    **checked,
                }
            )
        record_approval_phase(
            approval_id,
            "verifying",
            administration_id=client.administration_id,
        )
    except Exception as exc:
        phase = approval_execution_state(
            approval_id,
            administration_id=client.administration_id,
        )["phase"]
        audit_result = (
            "partial_failure"
            if writes_applied
            else classify_failed_write(exc, phase=phase)
        )
        record_approval_outcome(
            approval_id,
            audit_result,
            administration_id=client.administration_id,
            error=str(exc),
        )
        ctx.append_failed_audit_log(
            "reclassify_document_lines",
            fingerprint=fingerprint,
            error=str(exc),
            partial={
                "writes_applied": writes_applied,
                "updated_documents": updated_documents,
                "created_general_journal_documents": created_general_journal_documents,
                "verification": verification,
            },
            result=audit_result,
        )
        raise

    all_verified = (
        len(verification)
        == len(payload["document_updates"]) + len(payload["general_journal_documents"])
        and all(item["fully_verified"] for item in verification)
    )
    audit_result = "success" if all_verified else "verification_failed"
    record_approval_outcome(
        approval_id,
        audit_result,
        administration_id=client.administration_id,
    )
    ctx.append_audit_log(
        {
            "action": "reclassify_document_lines",
            "fingerprint": fingerprint,
            "result": audit_result,
            "updated_documents": updated_documents,
            "created_general_journal_documents": created_general_journal_documents,
            "verification": verification,
        }
    )
    return {
        "status": (
            "completed"
            if all_verified
            else "completed_with_verification_errors"
        ),
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "updated_documents": updated_documents,
        "created_general_journal_documents": created_general_journal_documents,
        "verification": verification,
        "all_verified": all_verified,
        "fingerprint": fingerprint,
    }


def _vat_period_general_journals(client, period: str) -> list[dict[str, Any]]:
    """Load journals inside the exact VAT period, including their account lines."""

    months = month_periods(period)
    years = sorted({month[:4] for month in months})
    by_id: dict[str, dict[str, Any]] = {}
    for year in years:
        page = 1
        while True:
            journals = client.list_documents(
                "general_journal_document",
                limit=100,
                page=page,
                filter=f"period:{year}0101..{year}1231",
            )
            new_records = 0
            for journal in journals:
                journal_id = str(journal.get("id") or "")
                key = journal_id or f"{journal.get('date')}:{journal.get('reference')}"
                if key not in by_id:
                    new_records += 1
                by_id[key] = journal
            if len(journals) < 100 or new_records == 0:
                break
            page += 1

    start_text, end_text = str(period).strip().split("..", 1)
    start = f"{start_text[:4]}-{start_text[4:6]}-{start_text[6:8]}"
    end = f"{end_text[:4]}-{end_text[4:6]}-{end_text[6:8]}"
    in_period: list[dict[str, Any]] = []
    for journal in by_id.values():
        journal_date = str(journal.get("date") or "")
        if not start <= journal_date <= end:
            continue
        entries = (
            journal.get("general_journal_document_entries")
            or journal.get("details")
            or journal.get("entries")
            or []
        )
        journal_id = str(journal.get("id") or "")
        if not entries and journal_id and hasattr(client, "get_document"):
            journal = client.get_document("general_journal_document", journal_id)
        in_period.append(journal)
    return in_period


def _vat_settlement_context(
    client,
    period: str,
    overrides: dict[str, str],
    *,
    roles: tuple[str, ...] = ("payable", "receivable", "settlement"),
) -> dict[str, Any]:
    """Gather everything a settlement needs: accounts, gross movements, reported totals."""
    # Validate locally before the first API call. A symbolic or partial period
    # must not be hidden behind an unrelated ledger-account lookup failure.
    report_months = month_periods(period)
    ledger_accounts = client.list_ledger_accounts()
    accounts = resolve_vat_accounts(
        ledger_accounts,
        overrides=overrides,
        roles=roles,
    )
    account_ids = [str(account["id"]) for account in accounts.values()]
    movements = ledger_movements_from_report(
        client.get_report("general_ledger", period=period),
        account_ids,
    )
    payable = movements[str(accounts["payable"]["id"])]
    receivable = movements[str(accounts["receivable"]["id"])]
    general_journals = _vat_period_general_journals(client, period)
    settlement_journals = find_vat_settlement_journals(
        general_journals,
        accounts=accounts,
        period=period,
    )
    # The tax report caps at one month, so a quarter is fetched month by month.
    reported = reported_vat_totals(
        client.get_report("tax", period=month) for month in report_months
    )
    # Gross ledger turnover and the reported rubrieken are compared, never merged:
    # reverse-charge VAT moves both accounts while reporting a zero tax amount.
    comparison_payable = payable.net_credit + sum(
        (
            money_decimal(journal["payable_restore"])
            for journal in settlement_journals
        ),
        start=money_decimal("0"),
    )
    comparison_receivable = receivable.net_debit + sum(
        (
            money_decimal(journal["receivable_restore"])
            for journal in settlement_journals
        ),
        start=money_decimal("0"),
    )
    comparison = compare_gross_to_reported(
        gross_payable=comparison_payable,
        gross_deductible=comparison_receivable,
        reported_payable=reported["payable"],
        reported_deductible=reported["deductible"],
    )
    comparison["basis"] = (
        "reconstructed_before_existing_settlement_journals"
        if settlement_journals
        else "current_period_ledger_movements"
    )
    return {
        "accounts": accounts,
        "movements": movements,
        "payable": payable,
        "receivable": receivable,
        "reported": reported,
        "comparison": comparison,
        "comparison_payable": comparison_payable,
        "comparison_receivable": comparison_receivable,
        "general_journals": general_journals,
        "settlement_journals": settlement_journals,
        "ledger_accounts": ledger_accounts,
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def analyze_vat_settlement(
    period: VatSettlementPeriod,
    payable_ledger_account_id: Annotated[str, Field(description="Override for the output-VAT account (default: the one named 'Te betalen btw').")] = "",
    receivable_ledger_account_id: Annotated[str, Field(description="Override for the input-VAT account (default: the one named 'Te vorderen btw').")] = "",
    settlement_ledger_account_id: Annotated[str, Field(description="Override for the tax-authority settlement account (default: 'Betaalde en/of ontvangen btw').")] = "",
) -> dict[str, Any]:
    """Use this to inspect the VAT position of a filed period before settling it: gross ledger movements, reported rubrieken, and whether any gap between them is explained by reverse-charge VAT."""
    client = ctx.get_client()
    context = _vat_settlement_context(
        client,
        period,
        {
            "payable": payable_ledger_account_id,
            "receivable": receivable_ledger_account_id,
            "settlement": settlement_ledger_account_id,
        },
    )
    payable = context["payable"]
    receivable = context["receivable"]
    already_settled = bool(context["settlement_journals"])
    return {
        "period": period,
        "accounts": {
            role: {
                "id": str(account["id"]),
                "name": account.get("name"),
                "account_id": account.get("account_id"),
            }
            for role, account in context["accounts"].items()
        },
        # Gross balances are what a settlement journal must clear.
        "gross_movements": {
            "payable_net_credit": str(context["comparison_payable"]),
            "receivable_net_debit": str(context["comparison_receivable"]),
            "net_position": str(
                context["comparison_payable"] - context["comparison_receivable"]
            ),
            "basis": context["comparison"]["basis"],
            "current_period_net_after_journals": {
                "payable_net_credit": str(payable.net_credit),
                "receivable_net_debit": str(receivable.net_debit),
            },
        },
        # The reported view is the separate cross-check, not the clearing basis.
        "reported": {
            "payable": str(context["reported"]["payable"]),
            "deductible": str(context["reported"]["deductible"]),
            "net": str(context["reported"]["net"]),
            "rows": context["reported"]["rows"],
        },
        "gross_vs_reported": context["comparison"],
        "settlement_status": {
            "already_settled": already_settled,
            "settlement_journals": context["settlement_journals"],
            "message": (
                "This period already contains a settlement-like general journal "
                "touching the VAT accounts. Do not settle it again."
                if already_settled
                else "No settlement-like general journal was found inside this period."
            ),
        },
        "next_step": (
            "Do not prepare another VAT settlement for this period. Review the listed "
            "general journal(s) if the period should be reopened or corrected."
            if already_settled
            else (
                "The filed amount is never derived from these figures: a Dutch return is "
                "filed in whole euros and may be rounded in the taxpayer's favour. Ask the "
                "user for the amount actually filed and paid (or read it from Moneybird's "
                "VAT overview), then call prepare_vat_settlement_journal with it."
            )
        ),
    }


def _vat_settlement_snapshot(
    client,
    period: str,
    context: dict[str, Any],
    reference: str,
    journal_year_period: str,
) -> dict[str, Any]:
    """State the settlement depends on, captured so execution can re-prove it."""
    administration = client.require_current_administration_access()
    return {
        "period": period,
        "reference": reference,
        "journal_year_period": journal_year_period,
        "gross_payable": str(context["payable"].net_credit),
        "gross_receivable": str(context["receivable"].net_debit),
        "period_locked_until": str(administration.get("period_locked_until") or ""),
        "account_ids": {
            role: str(account["id"]) for role, account in context["accounts"].items()
        },
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_vat_settlement_journal(
    period: VatSettlementPeriod,
    reference: Annotated[str, Field(description="Reference for the settlement journal, e.g. 'BTW-2026-Q2'. Prior settlements are detected independently by period and VAT-account lines, so changing this text cannot bypass the guard.")],
    declared_amount: Annotated[str, Field(description="The amount actually filed and settled, as a decimal string in whole euros. Positive = payable to the tax authority, negative = refund. Take this from the filed return or Moneybird's VAT overview; never derive it from the ledger, because filing rounds to whole euros in the taxpayer's favour.")],
    date: Annotated[str, Field(description="Journal date. Leave empty to use the period's closing date, which is what keeps the journal inside the period it clears.")] = "",
    description: str = "",
    payable_ledger_account_id: Annotated[str, Field(description="Override for the output-VAT account (default: the one named 'Te betalen btw').")] = "",
    receivable_ledger_account_id: Annotated[str, Field(description="Override for the input-VAT account (default: the one named 'Te vorderen btw').")] = "",
    settlement_ledger_account_id: Annotated[str, Field(description="Override for the tax-authority settlement account (default: 'Betaalde en/of ontvangen btw').")] = "",
    rounding_ledger_account_id: Annotated[str, Field(description="Rounding-difference account override. Needed only when declared_amount leaves a non-zero rounding line; the default lookup is 'Afrondingsverschillen'. If no suitable account exists, create one through prepare_create_ledger_account first.")] = "",
    allow_unexplained_difference: Annotated[bool, Field(description="Deliberately settle a period whose gross ledger movements do not reconcile with the reported rubrieken. Records the exception in the approval; only use after establishing the cause.")] = False,
    allow_date_outside_period: Annotated[bool, Field(description="Deliberately date the journal outside the period it clears. Leaves that period's movements standing in the period report; only use for a corrective booking.")] = False,
) -> dict[str, Any]:
    """Use this before booking the memoriaal that clears a filed VAT period: it clears the gross payable and deductible movements, credits the settled amount, and books the rounding advantage. Do not execute the write until the user explicitly confirms."""
    if not reference.strip():
        raise MoneybirdError("reference is required.")
    if not str(declared_amount).strip():
        raise MoneybirdError(
            "declared_amount is required. Read it from the filed return or "
            "Moneybird's VAT overview; it cannot be derived from the ledger."
        )

    client = ctx.get_client()
    context = _vat_settlement_context(
        client,
        period,
        {
            "payable": payable_ledger_account_id,
            "receivable": receivable_ledger_account_id,
            "settlement": settlement_ledger_account_id,
        },
    )
    accounts = context["accounts"]
    # The journal belongs at the close of the period it clears; a free date is an
    # opt-in, not the default.
    closing_date = period_end_date(period)
    journal_date = date.strip() or closing_date

    net_position = context["payable"].net_credit - context["receivable"].net_debit
    amount_check = validate_declared_amount(
        declared_amount=money_decimal(declared_amount),
        net_position=net_position,
        rubriek_count=count_rubrieken(context["reported"]),
    )
    administration = client.require_current_administration_access()
    preflight = settlement_preflight(
        movements=context["movements"],
        accounts=accounts,
        existing_journals=context["general_journals"],
        reference=reference.strip(),
        period=period,
        journal_date=journal_date,
        period_locked_until=str(administration.get("period_locked_until") or ""),
        period_end=closing_date,
        comparison=context["comparison"],
        declared_amount_check=amount_check,
        allow_unexplained_difference=allow_unexplained_difference,
        allow_date_outside_period=allow_date_outside_period,
    )
    if not preflight["clear_to_prepare"]:
        raise MoneybirdError(
            "VAT settlement preflight failed: " + " ".join(preflight["blocking_findings"])
        )
    if money_decimal(amount_check["rounding_difference"]) != 0:
        accounts.update(
            resolve_vat_accounts(
                context["ledger_accounts"],
                overrides={"rounding": rounding_ledger_account_id},
                roles=("rounding",),
            )
        )

    journal = build_vat_settlement_journal(
        accounts=accounts,
        payable_movement=context["payable"].net_credit,
        receivable_movement=context["receivable"].net_debit,
        declared_amount=money_decimal(declared_amount),
        description=description.strip(),
    )
    prepared = prepare_general_journal_entries(client, journal["entries"])
    # Moneybird stores no header description on a general journal document -- the
    # field is absent from the returned record (live-verified 2026-08-01). Sending
    # it would fail the post-write verifier on every settlement, so the text is
    # carried by the line descriptions instead.
    payload = clean_dict(
        {
            "reference": reference.strip(),
            "date": journal_date,
            "general_journal_document_entries_attributes": details_attributes_payload(
                prepared["entries"]
            ),
        }
    )
    # The fingerprint identifies the *settled period*, not the journal text, so a
    # second settlement of the same period is suppressed even under a different
    # reference, date or wording.
    fingerprint = duplicate_fingerprint(
        "settle_vat_period",
        {
            "period": period,
            "administration_id": str(client.administration_id),
            "accounts": {
                role: str(account["id"]) for role, account in accounts.items()
            },
        },
    )
    snapshot = _vat_settlement_snapshot(
        client,
        period,
        context,
        reference.strip(),
        year_period_for_date(journal_date),
    )
    return stage_write(
        "settle_vat_period",
        summary=(
            f"Settle VAT period {period} as '{reference.strip()}' "
            f"(declared {journal['declared_amount']})"
        ),
        payload={
            "general_journal_document": payload,
            "snapshot": snapshot,
            "verify_period_cleared": not allow_date_outside_period,
        },
        preview={
            "reference": reference.strip(),
            "date": journal_date,
            "description": description.strip(),
            "period": period,
            "period_end": closing_date,
            "entries": prepared["preview_entries"],
            "total_debit": prepared["total_debit"],
            "total_credit": prepared["total_credit"],
            "vat_settlement": {
                "gross_payable_cleared": journal["gross_payable"],
                "gross_receivable_cleared": journal["gross_receivable"],
                "net_position": journal["net_position"],
                "declared_amount": journal["declared_amount"],
                "rounding_difference": journal["rounding_difference"],
                "accounts": journal["accounts"],
            },
            "declared_amount_check": amount_check,
            "gross_vs_reported": context["comparison"],
            "preflight": preflight,
        },
        fingerprint=fingerprint,
    )


def _execute_vat_settlement(client, payload: dict[str, Any]) -> dict[str, Any]:
    """Re-prove the settlement's preconditions, then book and verify it.

    Approval and execution are separated in time. Between them a colleague can
    file another settlement, new VAT mutations can land, or the administration can
    be locked. The generic journal executor would happily post the approved lines
    anyway and report full verification, because it only compares the document
    with the payload. This re-reads the state the amounts were derived from.
    """
    snapshot = payload["snapshot"]
    expected = payload["general_journal_document"]
    period = snapshot["period"]

    accounts = {
        role: {"id": account_id}
        for role, account_id in snapshot["account_ids"].items()
    }
    movements = ledger_movements_from_report(
        client.get_report("general_ledger", period=period),
        snapshot["account_ids"].values(),
    )
    payable = movements[snapshot["account_ids"]["payable"]]
    receivable = movements[snapshot["account_ids"]["receivable"]]
    administration = client.require_current_administration_access()
    drift: list[str] = []
    if str(payable.net_credit) != snapshot["gross_payable"]:
        drift.append(
            f"payable movement changed from {snapshot['gross_payable']} to {payable.net_credit}"
        )
    if str(receivable.net_debit) != snapshot["gross_receivable"]:
        drift.append(
            f"receivable movement changed from {snapshot['gross_receivable']} to {receivable.net_debit}"
        )
    locked_until = str(administration.get("period_locked_until") or "")
    if locked_until != snapshot["period_locked_until"]:
        drift.append(
            f"administration lock changed from '{snapshot['period_locked_until']}' to '{locked_until}'"
        )
    current_journals = _vat_period_general_journals(client, period)
    settlement_journals = find_vat_settlement_journals(
        current_journals,
        accounts=accounts,
        period=period,
    )
    existing = [
        journal
        for journal in current_journals
        if str(journal.get("reference") or "").casefold()
        == str(snapshot["reference"]).casefold()
    ]
    if settlement_journals:
        drift.append(
            f"VAT period {period} now contains {len(settlement_journals)} "
            "settlement-like journal(s) touching the VAT accounts"
        )
    elif existing:
        drift.append(
            f"a journal with reference '{snapshot['reference']}' now exists "
            f"({len(existing)} match(es))"
        )
    if drift:
        raise MoneybirdError(
            "VAT settlement aborted before dispatch because the administration "
            "changed since approval: " + "; ".join(drift) + ". Re-prepare it."
        )

    mark_write_dispatch_started()
    created = client.create_general_journal_document(expected)
    record_id = str(created.get("id") or "")
    if not record_id:
        raise MoneybirdError(
            "Moneybird did not return a general journal document id; "
            "reconcile before retrying."
        )
    mark_write_verifying()
    record = client.get_document("general_journal_document", record_id)
    verification = verify_general_journal_payload(expected, record)
    record_id_matches = str(record.get("id") or "") == record_id

    # Independent of the document itself: the period it closes must now be flat.
    period_cleared = None
    if payload.get("verify_period_cleared"):
        after = ledger_movements_from_report(
            client.get_report("general_ledger", period=period),
            snapshot["account_ids"].values(),
        )
        period_cleared = (
            after[snapshot["account_ids"]["payable"]].net_credit == 0
            and after[snapshot["account_ids"]["receivable"]].net_debit == 0
        )

    fully_verified = (
        record_id_matches
        and verification["fully_verified"]
        and period_cleared is not False
    )
    return {
        "_status": "created" if fully_verified else "completed_with_verification_errors",
        "_audit_result": "success" if fully_verified else "verification_failed",
        "_audit": {
            "general_journal_document_id": record_id,
            "period": period,
            "reference": record.get("reference"),
            "fully_verified": fully_verified,
        },
        "general_journal_document": compact_general_journal_summary(
            record,
            client.administration_id,
        ),
        "verification": {
            "independent_post_read": True,
            "record_id_matches": record_id_matches,
            "preconditions_reproved_before_dispatch": True,
            **verification,
            # Spread last on purpose: the payload comparison carries its own
            # fully_verified, and a journal that matches its payload while leaving
            # the period dirty must not be reported as fully verified.
            "payload_fully_verified": verification["fully_verified"],
            "period_vat_accounts_cleared": period_cleared,
            "fully_verified": fully_verified,
        },
        "accounts": accounts,
    }


# Not registered as an MCP tool: every approved action executes through the single
# annotated execute_approved_action entry point. Kept as a Python function because
# tools/approvals.py dispatches to it and scripts/tests call it directly.
def vat_settlement_journal_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared VAT settlement."""
    client = ctx.get_client()
    return run_approved_write(
        client, approval_id, "settle_vat_period", _execute_vat_settlement
    )


