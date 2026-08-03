"""Ledger writes: ledger accounts, general journal documents, document-line reclassification."""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ..capabilities import require_write_capability
from ..config import (
    PREPARE_ANNOTATIONS,
    READ_ONLY_ANNOTATIONS,
    WRITE_ANNOTATIONS,
    MoneybirdError,
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
from ._params import ApprovalId, DateString, Period
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
    description: Annotated[str, Field(description="Shared description. Moneybird stores no header text on a journal document, so this is applied to every line that has no description of its own.")] = "",
) -> dict[str, Any]:
    """Use this before creating a Moneybird general journal document. Do not execute the write until the user explicitly confirms."""
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


def _vat_settlement_context(
    client,
    period: str,
    overrides: dict[str, str],
) -> dict[str, Any]:
    """Gather everything a settlement needs: accounts, gross movements, reported totals."""
    accounts = resolve_vat_accounts(
        client.list_ledger_accounts(),
        overrides=overrides,
    )
    account_ids = [str(account["id"]) for account in accounts.values()]
    movements = ledger_movements_from_report(
        client.get_report("general_ledger", period=period),
        account_ids,
    )
    payable = movements[str(accounts["payable"]["id"])]
    receivable = movements[str(accounts["receivable"]["id"])]
    # The tax report caps at one month, so a quarter is fetched month by month.
    reported = reported_vat_totals(
        client.get_report("tax", period=month) for month in month_periods(period)
    )
    # Gross ledger turnover and the reported rubrieken are compared, never merged:
    # reverse-charge VAT moves both accounts while reporting a zero tax amount.
    comparison = compare_gross_to_reported(
        gross_payable=payable.net_credit,
        gross_deductible=receivable.net_debit,
        reported_payable=reported["payable"],
        reported_deductible=reported["deductible"],
    )
    return {
        "accounts": accounts,
        "movements": movements,
        "payable": payable,
        "receivable": receivable,
        "reported": reported,
        "comparison": comparison,
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def analyze_vat_settlement(
    period: Period,
    payable_ledger_account_id: Annotated[str, Field(description="Override for the output-VAT account (default: the one named 'Te betalen btw').")] = "",
    receivable_ledger_account_id: Annotated[str, Field(description="Override for the input-VAT account (default: the one named 'Te vorderen btw').")] = "",
    settlement_ledger_account_id: Annotated[str, Field(description="Override for the tax-authority settlement account (default: 'Betaalde en/of ontvangen btw').")] = "",
    rounding_ledger_account_id: Annotated[str, Field(description="Override for the rounding-difference account (default: 'Afrondingsverschillen').")] = "",
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
            "rounding": rounding_ledger_account_id,
        },
    )
    payable = context["payable"]
    receivable = context["receivable"]
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
            "payable_net_credit": str(payable.net_credit),
            "receivable_net_debit": str(receivable.net_debit),
            "net_position": str(payable.net_credit - receivable.net_debit),
        },
        # The reported view is the separate cross-check, not the clearing basis.
        "reported": {
            "payable": str(context["reported"]["payable"]),
            "deductible": str(context["reported"]["deductible"]),
            "net": str(context["reported"]["net"]),
            "rows": context["reported"]["rows"],
        },
        "gross_vs_reported": context["comparison"],
        "next_step": (
            "The filed amount is never derived from these figures: a Dutch return is "
            "filed in whole euros and may be rounded in the taxpayer's favour. Ask the "
            "user for the amount actually filed and paid (or read it from Moneybird's "
            "VAT overview), then call prepare_vat_settlement_journal with it."
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
    period: Period,
    reference: Annotated[str, Field(description="Reference for the settlement journal, e.g. 'BTW-2026-Q2'. Also used to detect an already-settled period.")],
    declared_amount: Annotated[str, Field(description="The amount actually filed and settled, as a decimal string in whole euros. Positive = payable to the tax authority, negative = refund. Take this from the filed return or Moneybird's VAT overview; never derive it from the ledger, because filing rounds to whole euros in the taxpayer's favour.")],
    date: Annotated[str, Field(description="Journal date. Leave empty to use the period's closing date, which is what keeps the journal inside the period it clears.")] = "",
    description: str = "",
    payable_ledger_account_id: Annotated[str, Field(description="Override for the output-VAT account (default: the one named 'Te betalen btw').")] = "",
    receivable_ledger_account_id: Annotated[str, Field(description="Override for the input-VAT account (default: the one named 'Te vorderen btw').")] = "",
    settlement_ledger_account_id: Annotated[str, Field(description="Override for the tax-authority settlement account (default: 'Betaalde en/of ontvangen btw').")] = "",
    rounding_ledger_account_id: Annotated[str, Field(description="Override for the rounding-difference account (default: 'Afrondingsverschillen').")] = "",
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
            "rounding": rounding_ledger_account_id,
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
        existing_journals=client.list_documents(
            "general_journal_document",
            limit=100,
            filter=f"period:{year_period_for_date(journal_date)}",
        ),
        reference=reference.strip(),
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
    existing = [
        journal
        for journal in client.list_documents(
            "general_journal_document",
            limit=100,
            filter=f"period:{snapshot['journal_year_period']}",
        )
        if str(journal.get("reference") or "").casefold()
        == str(snapshot["reference"]).casefold()
    ]
    if existing:
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


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def vat_settlement_journal_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared VAT settlement."""
    client = ctx.get_client()
    return run_approved_write(
        client, approval_id, "settle_vat_period", _execute_vat_settlement
    )


