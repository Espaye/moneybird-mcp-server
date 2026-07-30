"""Ledger writes: ledger accounts, general journal documents, document-line reclassification."""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ..capabilities import require_write_capability
from ..config import (
    MoneybirdError,
    PREPARE_ANNOTATIONS,
    WRITE_ANNOTATIONS,
)
from ..formatting import (
    clean_dict,
    compact_general_journal_summary,
    compact_ledger_account_summary,
    duplicate_fingerprint,
    iso_now,
)
from ..safety import (
    approval_execution_state,
    make_approval,
    pop_approval,
    record_approval_phase,
    record_approval_outcome,
)
from ..invoicing import (
    details_attributes_payload,
    prepare_general_journal_entries,
    prepare_reclassification_batch,
)
from ..write_contracts import (
    assert_patch_precondition,
    verify_document_reclassification,
    verify_general_journal_payload,
)
from ._params import ApprovalId, DateString
from ._registry import mcp
from ._writes import (
    mark_write_dispatch_started,
    mark_write_verifying,
    run_approved_write,
    stage_write,
)
from . import _context as ctx


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_create_ledger_account(
    name: Annotated[str, Field(description="Ledger account (category) name as it should appear in reports.")],
    account_type: Annotated[str, Field(description="Moneybird account_type, e.g. 'expenses', 'revenue', 'direct_costs', 'current_assets', 'non_current_assets', 'current_liabilities', 'equity', 'other_income_expenses'.")],
    account_id: Annotated[str, Field(description="Optional ledger account number (grootboeknummer).")] = "",
    rgs_code: Annotated[str, Field(description="Optional Dutch RGS reference code.")] = "",
    active: bool = True,
) -> dict[str, Any]:
    """Use this before creating a Moneybird ledger account. Do not execute the write until the user explicitly confirms."""
    if not name.strip():
        raise MoneybirdError("name is required.")
    if not account_type.strip():
        raise MoneybirdError("account_type is required.")

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


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_ledger_account_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared ledger account creation."""
    client = ctx.get_client()
    return run_approved_write(
        client, approval_id, "create_ledger_account", _execute_create_ledger_account
    )


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_create_general_journal_document(
    reference: Annotated[str, Field(description="Short reference/title for the journal entry (memoriaal).")],
    date: DateString,
    entries: Annotated[
        list[dict[str, Any]],
        Field(description="Journal lines. Each dict: ledger_account_id or ledger_account_name, debit and/or credit (decimal strings), and optional description, contact_id, project_id, tax_rate_id. Total debit must equal total credit."),
    ],
    description: str = "",
) -> dict[str, Any]:
    """Use this before creating a Moneybird general journal document. Do not execute the write until the user explicitly confirms."""
    if not reference.strip():
        raise MoneybirdError("reference is required.")
    if not date.strip():
        raise MoneybirdError("date is required.")

    client = ctx.get_client()
    prepared = prepare_general_journal_entries(client, entries)
    payload = clean_dict(
        {
            "reference": reference.strip(),
            "date": date.strip(),
            "description": description.strip(),
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
            "description": description.strip(),
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


@mcp.tool(annotations=WRITE_ANNOTATIONS)
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


@mcp.tool(annotations=WRITE_ANNOTATIONS)
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
            else ("failed_pre_write" if phase == "preflight" else "ambiguous")
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


