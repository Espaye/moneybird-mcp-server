"""Ledger writes: ledger accounts, general journal documents, document-line reclassification."""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

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
from ..safety import make_approval, pop_approval
from ..invoicing import (
    details_attributes_payload,
    prepare_general_journal_entries,
    prepare_reclassification_batch,
)
from ._params import ApprovalId, DateString
from ._registry import mcp
from ._writes import run_approved_write, stage_write
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
    record = client.create_ledger_account(
        payload["ledger_account"],
        rgs_code=payload.get("rgs_code", ""),
    )
    return {
        "_status": "created",
        "_audit": {
            "ledger_account_id": str(record.get("id")),
            "name": record.get("name"),
        },
        "ledger_account": compact_ledger_account_summary(record),
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
    record = client.create_general_journal_document(payload["general_journal_document"])
    return {
        "_status": "created",
        "_audit": {
            "general_journal_document_id": str(record.get("id")),
            "reference": record.get("reference"),
        },
        "general_journal_document": compact_general_journal_summary(
            record,
            client.administration_id,
        ),
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
    pending = pop_approval(approval_id, "reclassify_document_lines", administration_id=client.administration_id)
    payload = pending["payload"]
    fingerprint = payload["fingerprint"]
    if ctx.audit_log_contains_success("reclassify_document_lines", fingerprint):
        raise MoneybirdError(
            "This document reclassification payload already completed successfully according to the local audit log."
        )

    updated_documents: list[dict[str, Any]] = []
    created_general_journal_documents: list[dict[str, Any]] = []
    try:
        for item in payload["document_updates"]:
            record = client.update_document(
                item["document_kind"],
                item["document_id"],
                {
                    "details_attributes": details_attributes_payload(
                        item["details_attributes"]
                    )
                },
            )
            updated_documents.append(
                {
                    "document_kind": item["document_kind"],
                    "document_id": str(record.get("id")),
                    "reference": record.get("reference"),
                    "version": record.get("version"),
                }
            )

        for item in payload["general_journal_documents"]:
            record = client.create_general_journal_document(
                item["general_journal_document"]
            )
            created_general_journal_documents.append(
                {
                    "general_journal_document_id": str(record.get("id")),
                    "reference": record.get("reference"),
                    "date": record.get("date"),
                }
            )
    except Exception as exc:
        ctx.append_failed_audit_log(
            "reclassify_document_lines",
            fingerprint=fingerprint,
            error=str(exc),
            partial={
                "updated_documents": updated_documents,
                "created_general_journal_documents": created_general_journal_documents,
            },
        )
        raise

    ctx.append_audit_log(
        {
            "action": "reclassify_document_lines",
            "fingerprint": fingerprint,
            "result": "success",
            "updated_documents": updated_documents,
            "created_general_journal_documents": created_general_journal_documents,
        }
    )
    return {
        "status": "completed",
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "updated_documents": updated_documents,
        "created_general_journal_documents": created_general_journal_documents,
        "fingerprint": fingerprint,
    }


