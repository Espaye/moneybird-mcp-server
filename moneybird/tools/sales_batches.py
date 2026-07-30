"""Batch sales-invoice flows: batch create/update/schedule and the meter-usage run."""
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
    chunked,
    duplicate_fingerprint,
    iso_now,
    money_decimal,
    render_preview_table,
)
from ..safety import (
    approval_execution_state,
    make_approval,
    pop_approval,
    record_approval_phase,
    record_approval_outcome,
)
from ..invoicing import (
    apply_batch_group_merge_checks,
    build_batch_invoice_payload,
    build_meter_usage_entries,
    build_merge_snapshot_from_invoice,
    evaluate_merge_compatibility,
    list_scheduled_merge_candidates,
    summarize_batch_preview,
)
from ..write_contracts import (
    assert_patch_precondition,
    build_patch_precondition,
    verify_sales_invoice_payload,
    verify_sales_invoice_patch,
)
from ._params import ApprovalId, DateString, OptionalDateString
from ._registry import mcp
from . import _context as ctx


def _money_values_equal(left: Any, right: Any) -> bool:
    """Compare Moneybird monetary values at the supported cent precision."""
    try:
        left_value = money_decimal(left)
        right_value = money_decimal(right)
    except Exception:
        return False
    return left_value.is_finite() and right_value.is_finite() and left_value == right_value


def _prepare_batch_create_sales_invoices(
    client: Any,
    entries: list[dict[str, Any]],
    skip_if_duplicate: bool = True,
    fail_on_duplicate: bool = False,
) -> dict[str, Any]:
    if not entries:
        raise MoneybirdError("Provide at least one batch entry.")
    batch_items = [build_batch_invoice_payload(client, entry) for entry in entries]
    apply_batch_group_merge_checks(batch_items)
    preview = summarize_batch_preview(batch_items)
    if fail_on_duplicate and preview["duplicate_count"]:
        raise MoneybirdError(
            "Potential duplicates found. Review the preview and rerun with fail_on_duplicate false if you want to continue."
        )

    payload = {
        "items": batch_items,
        "skip_if_duplicate": skip_if_duplicate,
        "fail_on_duplicate": fail_on_duplicate,
    }
    fingerprint = duplicate_fingerprint("batch_create_sales_invoices", payload)
    approval = make_approval(
        "batch_create_sales_invoices",
        {**payload, "fingerprint": fingerprint},
        f"Create {len(batch_items)} sales invoice(s) in batch",
    )
    approval["preview"] = preview
    approval["payload"] = {**payload, "fingerprint": fingerprint}
    return approval


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_batch_create_sales_invoices(
    entries: Annotated[
        list[dict[str, Any]],
        Field(description="One dict per invoice: contact_id or customer_id, details (invoice lines), and optional reference, invoice_date, scheduled_send_on."),
    ],
    skip_if_duplicate: Annotated[bool, Field(description="Silently skip entries whose fingerprint already succeeded per the audit log.")] = True,
    fail_on_duplicate: Annotated[bool, Field(description="Raise instead of skipping when a duplicate is detected.")] = False,
) -> dict[str, Any]:
    """Use this before creating multiple sales invoices in one batch. It returns a preview table, duplicate warnings, and an automatic merge-compatibility check before any write happens."""
    return _prepare_batch_create_sales_invoices(
        ctx.get_client(),
        entries,
        skip_if_duplicate=skip_if_duplicate,
        fail_on_duplicate=fail_on_duplicate,
    )


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def batch_create_sales_invoices_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared batch invoice creation."""
    client = ctx.get_client()
    require_write_capability(action="batch_create_sales_invoices")
    pending = pop_approval(approval_id, "batch_create_sales_invoices", administration_id=client.administration_id)
    payload = pending["payload"]
    fingerprint = payload["fingerprint"]
    if ctx.audit_log_contains_success("batch_create_sales_invoices", fingerprint):
        record_approval_outcome(
            approval_id,
            "duplicate_suppressed",
            administration_id=client.administration_id,
        )
        raise MoneybirdError(
            "This batch payload already completed successfully according to the local audit log."
        )

    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    expected_by_created_id: dict[str, dict[str, Any]] = {}
    writes_applied = 0
    dispatch_started = False
    try:
        for item in payload["items"]:
            if item["duplicates"] and payload["skip_if_duplicate"]:
                skipped.append(
                    {
                        "customer_id": item["contact"].get("customer_id"),
                        "reason": "potential_duplicate",
                        "duplicates": item["duplicates"],
                    }
                )
                continue

            if not dispatch_started:
                record_approval_phase(
                    approval_id,
                    "dispatching",
                    administration_id=client.administration_id,
                )
                dispatch_started = True
            record = client.create_sales_invoice(item["sales_invoice"])
            writes_applied += 1
            result_row = {
                "customer_id": item["contact"].get("customer_id"),
                "sales_invoice_id": str(record.get("id")),
                "invoice_id": record.get("invoice_id"),
                "state": record.get("state"),
                "reference": record.get("reference"),
                "expected_total_incl_tax": item.get("expected_total_incl_tax"),
                "expected_state": "scheduled" if item["schedule_send_on"] else "draft",
                "expected_invoice_date": item["schedule_send_on"]
                or item["sales_invoice"].get("invoice_date"),
            }
            expected_by_created_id[str(record.get("id"))] = item["sales_invoice"]
            if item["schedule_send_on"]:
                record = client.send_sales_invoice(str(record["id"]), item["send_payload"])
                writes_applied += 1
                result_row.update(
                    {
                        "state": record.get("state"),
                        "invoice_date": record.get("invoice_date"),
                        "sent_at": record.get("sent_at"),
                    }
                )
            created.append(result_row)
        if dispatch_started:
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
            "batch_create_sales_invoices",
            fingerprint=fingerprint,
            error=str(exc),
            partial={
                "writes_applied": writes_applied,
                "created": created,
                "skipped": skipped,
            },
            result=audit_result,
        )
        raise

    fetched: list[dict[str, Any]] = []
    created_ids = [row["sales_invoice_id"] for row in created]
    for id_batch in chunked(created_ids, 100):
        fetched.extend(client.fetch_sales_invoices_by_ids(id_batch))
    fetched_by_id = {str(item.get("id")): item for item in fetched}
    verification: list[dict[str, Any]] = []
    for row in created:
        invoice = fetched_by_id.get(row["sales_invoice_id"], {})
        payload_verification = verify_sales_invoice_payload(
            expected_by_created_id.get(row["sales_invoice_id"], {}),
            invoice,
        )
        checks = {
            "total_matches": _money_values_equal(
                invoice.get("total_price_incl_tax"),
                row.get("expected_total_incl_tax"),
            ),
            "state_matches": str(invoice.get("state")) == str(row.get("expected_state")),
            "invoice_date_matches": (
                not row.get("expected_invoice_date")
                or str(invoice.get("invoice_date")) == str(row.get("expected_invoice_date"))
            ),
            "not_sent_yet": invoice.get("sent_at") in (None, ""),
            "payload_fields_and_lines_match": payload_verification[
                "fully_verified"
            ],
        }
        verification.append(
            {
                "customer_id": row.get("customer_id"),
                "sales_invoice_id": row["sales_invoice_id"],
                "state": invoice.get("state"),
                "invoice_date": invoice.get("invoice_date"),
                "sent_at": invoice.get("sent_at"),
                "total_price_incl_tax": invoice.get("total_price_incl_tax"),
                "expected_total_incl_tax": row.get("expected_total_incl_tax"),
                "checks": checks,
                "payload_verification": payload_verification,
                "verified": all(checks.values()),
            }
        )
    all_verified = all(row["verified"] for row in verification)
    audit_result = "success" if all_verified else "verification_failed"

    record_approval_outcome(
        approval_id,
        audit_result,
        administration_id=client.administration_id,
    )
    ctx.append_audit_log(
        {
            "action": "batch_create_sales_invoices",
            "fingerprint": fingerprint,
            "result": audit_result,
            "created": created,
            "skipped": skipped,
            "verification": verification,
        }
    )
    return {
        "status": "completed" if all_verified else "completed_with_verification_errors",
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "created": created,
        "skipped": skipped,
        "verification": verification,
        "all_verified": all_verified,
        "fingerprint": fingerprint,
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_batch_update_sales_invoices(
    entries: Annotated[
        list[dict[str, Any]],
        Field(description="One dict per update: sales_invoice_id (or customer_id plus filters) and the fields to change, e.g. details_attributes line edits."),
    ],
) -> dict[str, Any]:
    """Use this before updating one or more existing sales invoices, either by explicit invoice id or by customer lookup plus filters."""
    if not entries:
        raise MoneybirdError("Provide at least one batch update entry.")

    client = ctx.get_client()
    prepared_items: list[dict[str, Any]] = []
    preview_rows: list[dict[str, Any]] = []
    seen_invoice_ids: set[str] = set()

    for entry in entries:
        sales_invoice_id = str(entry.get("sales_invoice_id", "")).strip()
        if sales_invoice_id:
            invoice = client.get_sales_invoice(sales_invoice_id)
        else:
            customer_id = str(entry.get("customer_id", "")).strip()
            if not customer_id:
                raise MoneybirdError("Each update entry needs sales_invoice_id or customer_id.")
            contact = client.get_contact_by_customer_id(customer_id)
            matches = client.list_sales_invoices(
                limit=10,
                page=1,
                state=entry.get("state", "all"),
                reference=str(entry.get("reference", "")),
                contact_id=str(contact["id"]),
                period=str(entry.get("period_filter", "this_year")),
            )
            if len(matches) != 1:
                raise MoneybirdError(
                    f"Expected exactly one invoice for customer_id {customer_id}, got {len(matches)}."
                )
            invoice = client.get_sales_invoice(str(matches[0]["id"]))

        resolved_invoice_id = str(invoice["id"])
        if resolved_invoice_id in seen_invoice_ids:
            raise MoneybirdError(
                f"Sales invoice {resolved_invoice_id} is listed more than once."
            )
        seen_invoice_ids.add(resolved_invoice_id)

        details_patch = []
        for detail_update in entry.get("detail_updates", []):
            row_order = int(detail_update.get("row_order", 0))
            details = invoice.get("details") or []
            matching = next((detail for detail in details if int(detail.get("row_order", 0)) == row_order), None)
            if not matching:
                raise MoneybirdError(
                    f"Could not find detail row_order {row_order} on invoice {invoice.get('id')}."
                )
            details_patch.append(
                clean_dict(
                    {
                        "id": matching["id"],
                        "description": detail_update.get("description", ""),
                        "period": detail_update.get("period", ""),
                        "price": detail_update.get("price", ""),
                        "amount": detail_update.get("amount", ""),
                        "tax_rate_id": detail_update.get("tax_rate_id"),
                        "ledger_account_id": detail_update.get("ledger_account_id"),
                    }
                )
            )

        sales_invoice_patch = clean_dict(
            {
                "reference": entry.get("new_reference", None),
                "invoice_date": entry.get("invoice_date", ""),
                "due_date": entry.get("due_date", ""),
                "details_attributes": details_patch,
            }
        )
        prepared_items.append(
            {
                "sales_invoice_id": resolved_invoice_id,
                "invoice_id": invoice.get("invoice_id"),
                "customer_id": invoice.get("contact", {}).get("customer_id"),
                "patch": sales_invoice_patch,
                "precondition": build_patch_precondition(
                    invoice,
                    sales_invoice_patch,
                ),
            }
        )
        preview_rows.append(
            {
                "customer_id": invoice.get("contact", {}).get("customer_id"),
                "description": ", ".join(
                    detail.get("description", "")
                    for detail in details_patch
                    if detail.get("description")
                )
                or "invoice update",
                "amount_excl_tax": "",
                "amount_tax": "",
                "amount_incl_tax": "",
                "status": "ready",
            }
        )

    fingerprint = duplicate_fingerprint(
        "batch_update_sales_invoices",
        {"items": prepared_items},
    )
    approval = make_approval(
        "batch_update_sales_invoices",
        {"items": prepared_items, "fingerprint": fingerprint},
        f"Update {len(prepared_items)} sales invoice(s) in batch",
    )
    approval["preview"] = {
        "preview_table": render_preview_table(preview_rows),
        "item_count": len(prepared_items),
    }
    approval["payload"] = {"items": prepared_items, "fingerprint": fingerprint}
    return approval


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def batch_update_sales_invoices_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared batch invoice update."""
    client = ctx.get_client()
    require_write_capability(action="batch_update_sales_invoices")
    pending = pop_approval(approval_id, "batch_update_sales_invoices", administration_id=client.administration_id)
    payload = pending["payload"]
    fingerprint = payload["fingerprint"]
    if ctx.audit_log_contains_success("batch_update_sales_invoices", fingerprint):
        record_approval_outcome(
            approval_id,
            "duplicate_suppressed",
            administration_id=client.administration_id,
        )
        raise MoneybirdError(
            "This batch update payload already completed successfully according to the local audit log."
        )

    updated: list[dict[str, Any]] = []
    verification: list[dict[str, Any]] = []
    writes_applied = 0
    try:
        # Validate the complete batch before the first mutation. A stale later
        # row must never turn an otherwise avoidable batch into a partial write.
        for item in payload["items"]:
            current = client.get_sales_invoice(item["sales_invoice_id"])
            if str(current.get("id") or "") != str(item["sales_invoice_id"]):
                raise MoneybirdError(
                    f"Sales invoice {item['sales_invoice_id']} lookup returned a "
                    "different record. Prepare again."
                )
            assert_patch_precondition(
                current,
                item.get("precondition") or {},
                record_label=f"Sales invoice {item['sales_invoice_id']}",
            )

        record_approval_phase(
            approval_id,
            "dispatching",
            administration_id=client.administration_id,
        )
        for item in payload["items"]:
            record = client.update_sales_invoice(item["sales_invoice_id"], item["patch"])
            writes_applied += 1
            updated.append(
                {
                    "sales_invoice_id": str(record.get("id")),
                    "invoice_id": record.get("invoice_id"),
                    "customer_id": record.get("contact", {}).get("customer_id"),
                    "state": record.get("state"),
                }
            )

        record_approval_phase(
            approval_id,
            "verifying",
            administration_id=client.administration_id,
        )
        for item in payload["items"]:
            record = client.get_sales_invoice(item["sales_invoice_id"])
            checked = verify_sales_invoice_patch(item["patch"], record)
            verification.append(
                {
                    "sales_invoice_id": item["sales_invoice_id"],
                    **checked,
                }
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
            "batch_update_sales_invoices",
            fingerprint=fingerprint,
            error=str(exc),
            partial={
                "writes_applied": writes_applied,
                "updated": updated,
                "verification": verification,
            },
            result=audit_result,
        )
        raise

    all_verified = (
        len(verification) == len(payload["items"])
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
            "action": "batch_update_sales_invoices",
            "fingerprint": fingerprint,
            "result": audit_result,
            "updated": updated,
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
        "updated": updated,
        "verification": verification,
        "all_verified": all_verified,
        "fingerprint": fingerprint,
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_batch_schedule_sales_invoices(
    entries: Annotated[
        list[dict[str, Any]],
        Field(description="One dict per invoice: sales_invoice_id and invoice_date (the future send date), plus optional delivery_method, email_address, email_message."),
    ],
) -> dict[str, Any]:
    """Prepare multiple existing draft invoices for future sending in one approval.

    Each entry needs ``sales_invoice_id`` and ``invoice_date``. Optional delivery_method,
    email_address and email_message override the contact/workflow defaults.
    """
    if not entries:
        raise MoneybirdError("Provide at least one invoice to schedule.")

    client = ctx.get_client()
    ids = [str(entry.get("sales_invoice_id") or "").strip() for entry in entries]
    if any(not item_id for item_id in ids):
        raise MoneybirdError("Each schedule entry needs sales_invoice_id.")
    if len(set(ids)) != len(ids):
        raise MoneybirdError("sales_invoice_id values must be unique within the batch.")

    invoices: list[dict[str, Any]] = []
    for id_batch in chunked(ids, 100):
        invoices.extend(client.fetch_sales_invoices_by_ids(id_batch))
    invoices_by_id = {str(invoice.get("id")): invoice for invoice in invoices}
    if set(invoices_by_id) != set(ids):
        missing = sorted(set(ids) - set(invoices_by_id))
        raise MoneybirdError(f"Could not fetch sales invoice(s): {', '.join(missing)}.")

    prepared_items: list[dict[str, Any]] = []
    preview_rows: list[dict[str, Any]] = []
    merge_checks: list[dict[str, Any]] = []
    for entry, sales_invoice_id in zip(entries, ids):
        invoice = invoices_by_id[sales_invoice_id]
        invoice_date = str(entry.get("invoice_date") or "").strip()
        if not invoice_date:
            raise MoneybirdError(
                f"invoice_date is required for sales invoice {sales_invoice_id}."
            )
        state = str(invoice.get("state") or "")
        already_scheduled = state == "scheduled" and str(invoice.get("invoice_date")) == invoice_date
        if state not in {"draft", "scheduled"}:
            raise MoneybirdError(
                f"Sales invoice {sales_invoice_id} has state {state}; only draft or scheduled invoices can be prepared."
            )
        if state == "scheduled" and not already_scheduled:
            raise MoneybirdError(
                f"Sales invoice {sales_invoice_id} is already scheduled for {invoice.get('invoice_date')}."
            )

        send_payload = clean_dict(
            {
                "sending_scheduled": True,
                "invoice_date": invoice_date,
                "delivery_method": entry.get("delivery_method", ""),
                "email_address": entry.get("email_address", ""),
                "email_message": entry.get("email_message", ""),
            }
        )
        candidates = list_scheduled_merge_candidates(
            client,
            contact_id=str(invoice.get("contact_id") or (invoice.get("contact") or {}).get("id") or ""),
            scheduled_send_on=invoice_date,
            exclude_sales_invoice_id=sales_invoice_id,
        )
        merge_check = evaluate_merge_compatibility(
            build_merge_snapshot_from_invoice(invoice, scheduled_send_on=invoice_date),
            candidates,
        )
        merge_checks.append(
            {
                "customer_id": (invoice.get("contact") or {}).get("customer_id"),
                "sales_invoice_id": sales_invoice_id,
                **merge_check,
            }
        )
        prepared_items.append(
            {
                "sales_invoice_id": sales_invoice_id,
                "customer_id": (invoice.get("contact") or {}).get("customer_id"),
                "before_total_price_incl_tax": invoice.get("total_price_incl_tax"),
                "expected_record": {
                    key: invoice.get(key)
                    for key in (
                        "version",
                        "updated_at",
                        "state",
                        "invoice_date",
                        "sent_at",
                        "total_price_incl_tax",
                    )
                    if invoice.get(key) is not None
                },
                "already_scheduled": already_scheduled,
                "sales_invoice_sending": send_payload,
            }
        )
        preview_rows.append(
            {
                "customer_id": (invoice.get("contact") or {}).get("customer_id") or sales_invoice_id,
                "description": ", ".join(
                    str(detail.get("description") or "") for detail in (invoice.get("details") or [])
                ),
                "amount_excl_tax": invoice.get("total_price_excl_tax") or "",
                "amount_tax": "",
                "amount_incl_tax": invoice.get("total_price_incl_tax") or "",
                "status": "already-scheduled" if already_scheduled else "ready",
            }
        )

    payload = {"items": prepared_items}
    fingerprint = duplicate_fingerprint("batch_schedule_sales_invoices", payload)
    approval = make_approval(
        "batch_schedule_sales_invoices",
        {**payload, "fingerprint": fingerprint},
        f"Schedule {len(prepared_items)} sales invoice(s)",
    )
    approval["payload"] = {**payload, "fingerprint": fingerprint}
    approval["preview"] = {
        "preview_table": render_preview_table(preview_rows),
        "item_count": len(prepared_items),
        "merge_checks": merge_checks,
    }
    return approval


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def batch_schedule_sales_invoices_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Schedule a prepared invoice batch and verify every resulting invoice."""
    client = ctx.get_client()
    require_write_capability(action="batch_schedule_sales_invoices")
    pending = pop_approval(
        approval_id,
        "batch_schedule_sales_invoices",
        administration_id=client.administration_id,
    )
    payload = pending["payload"]
    fingerprint = payload["fingerprint"]
    if ctx.audit_log_contains_success("batch_schedule_sales_invoices", fingerprint):
        record_approval_outcome(
            approval_id,
            "duplicate_suppressed",
            administration_id=client.administration_id,
        )
        raise MoneybirdError(
            "This schedule batch already completed successfully according to the local audit log."
        )

    scheduled: list[dict[str, Any]] = []
    writes_applied = 0
    dispatch_started = False
    try:
        for item in payload["items"]:
            current = client.get_sales_invoice(item["sales_invoice_id"])
            changed = {
                key: {"expected": value, "actual": current.get(key)}
                for key, value in (item.get("expected_record") or {}).items()
                if (
                    not _money_values_equal(current.get(key), value)
                    if key == "total_price_incl_tax"
                    else str(current.get(key) or "") != str(value or "")
                )
            }
            if str(current.get("id") or "") != str(item["sales_invoice_id"]):
                changed["id"] = {
                    "expected": item["sales_invoice_id"],
                    "actual": current.get("id"),
                }
            if changed:
                raise MoneybirdError(
                    f"Sales invoice {item['sales_invoice_id']} changed after "
                    f"preview: {changed}. Prepare again."
                )
        for item in payload["items"]:
            if item.get("already_scheduled"):
                scheduled.append(
                    {
                        "sales_invoice_id": item["sales_invoice_id"],
                        "customer_id": item.get("customer_id"),
                        "action": "already_scheduled",
                    }
                )
                continue
            if not dispatch_started:
                record_approval_phase(
                    approval_id,
                    "dispatching",
                    administration_id=client.administration_id,
                )
                dispatch_started = True
            record = client.send_sales_invoice(
                item["sales_invoice_id"],
                item["sales_invoice_sending"],
            )
            writes_applied += 1
            scheduled.append(
                {
                    "sales_invoice_id": str(record.get("id")),
                    "customer_id": item.get("customer_id"),
                    "action": "scheduled",
                }
            )
        if dispatch_started:
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
            "batch_schedule_sales_invoices",
            fingerprint=fingerprint,
            error=str(exc),
            partial={
                "writes_applied": writes_applied,
                "scheduled": scheduled,
            },
            result=audit_result,
        )
        raise

    ids = [item["sales_invoice_id"] for item in payload["items"]]
    fetched: list[dict[str, Any]] = []
    for id_batch in chunked(ids, 100):
        fetched.extend(client.fetch_sales_invoices_by_ids(id_batch))
    fetched_by_id = {str(invoice.get("id")): invoice for invoice in fetched}

    verification: list[dict[str, Any]] = []
    for item in payload["items"]:
        invoice = fetched_by_id.get(item["sales_invoice_id"], {})
        expected_date = item["sales_invoice_sending"]["invoice_date"]
        checks = {
            "total_unchanged": _money_values_equal(
                invoice.get("total_price_incl_tax"),
                item.get("before_total_price_incl_tax"),
            ),
            "state_scheduled": invoice.get("state") == "scheduled",
            "invoice_date_matches": invoice.get("invoice_date") == expected_date,
            "not_sent_yet": invoice.get("sent_at") in (None, ""),
        }
        verification.append(
            {
                "customer_id": item.get("customer_id"),
                "sales_invoice_id": item["sales_invoice_id"],
                "state": invoice.get("state"),
                "invoice_date": invoice.get("invoice_date"),
                "sent_at": invoice.get("sent_at"),
                "total_price_incl_tax": invoice.get("total_price_incl_tax"),
                "checks": checks,
                "verified": all(checks.values()),
            }
        )
    all_verified = all(item["verified"] for item in verification)
    audit_result = "success" if all_verified else "verification_failed"
    record_approval_outcome(
        approval_id,
        audit_result,
        administration_id=client.administration_id,
    )
    ctx.append_audit_log(
        {
            "action": "batch_schedule_sales_invoices",
            "fingerprint": fingerprint,
            "result": audit_result,
            "scheduled": scheduled,
            "verification": verification,
        }
    )
    return {
        "status": "completed" if all_verified else "completed_with_verification_errors",
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "scheduled": scheduled,
        "verification": verification,
        "all_verified": all_verified,
        "fingerprint": fingerprint,
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_meter_usage_sales_invoices(
    rows: Annotated[
        list[dict[str, Any]],
        Field(description="One dict per meter: meter (name), optional customer_id, and either usage_kwh or begin_reading + end_reading; optional action ('skip', 'draft', 'schedule', 'merge', 'separate') and per-row price/tax/ledger overrides."),
    ],
    period_label: Annotated[str, Field(description="Human-readable usage period for the line description, e.g. 'juni 2026'.")],
    invoice_date: DateString,
    schedule_send_on: OptionalDateString = "",
    minimum_usage_kwh: Annotated[str, Field(description="Meters at or below this usage are skipped (decimal string).")] = "0",
    description_prefix: Annotated[str, Field(description="Line description prefix; also used to find the previous matching meter line for defaults.")] = "Elektra",
    default_unit_price: Annotated[str, Field(description="Fallback price per kWh when no previous meter line supplies one.")] = "",
    default_tax_rate_id: Annotated[str, Field(description="Fallback tax rate id when no previous meter line supplies one.")] = "",
    default_ledger_account_id: Annotated[str, Field(description="Fallback ledger account id when no previous meter line supplies one.")] = "",
    skip_meters: Annotated[list[str] | None, Field(description="Meter names to exclude from the run.")] = None,
) -> dict[str, Any]:
    """Prepare a complete metered-usage invoice run from readings or supplied usage.

    Each row accepts ``meter``, optional ``customer_id``, and either ``usage_kwh`` or
    ``begin_reading`` + ``end_reading``. ``action`` may be ``skip``, ``draft``,
    ``schedule``, ``merge`` or ``separate``. When price/tax/ledger are omitted, the
    newest matching invoice line (for example ``Elektra B5``) supplies those defaults.
    """
    client = ctx.get_client()
    prepared_usage = build_meter_usage_entries(
        client,
        rows=rows,
        period_label=period_label,
        invoice_date=invoice_date,
        schedule_send_on=schedule_send_on,
        minimum_usage_kwh=minimum_usage_kwh,
        description_prefix=description_prefix,
        default_unit_price=default_unit_price,
        default_tax_rate_id=default_tax_rate_id,
        default_ledger_account_id=default_ledger_account_id,
        skip_meters=skip_meters,
    )
    approval = _prepare_batch_create_sales_invoices(
        client,
        prepared_usage["entries"],
        skip_if_duplicate=True,
        fail_on_duplicate=True,
    )
    merge_checks = {
        str(item.get("customer_id") or ""): item
        for item in (approval.get("preview") or {}).get("merge_checks", [])
    }
    intent_warnings: list[dict[str, Any]] = []
    for decision in prepared_usage["decisions"]:
        intent = decision.get("merge_intent")
        check = merge_checks.get(str(decision.get("customer_id") or ""), {})
        if intent == "merge" and check.get("status") != "compatible":
            intent_warnings.append(
                {
                    "customer_id": decision.get("customer_id"),
                    "intent": "merge",
                    "warning": (
                        "No currently scheduled compatible invoice was found; "
                        "merging may only become verifiable when the recurring invoice exists."
                    ),
                }
            )
        if intent == "separate" and check.get("status") == "compatible":
            intent_warnings.append(
                {
                    "customer_id": decision.get("customer_id"),
                    "intent": "separate",
                    "warning": "A compatible scheduled invoice exists, so Moneybird may merge them.",
                }
            )
    approval["meter_usage_preview"] = {
        "period_label": period_label,
        "invoice_date": invoice_date,
        "schedule_send_on": schedule_send_on,
        "minimum_usage_kwh": minimum_usage_kwh,
        "decisions": prepared_usage["decisions"],
        "intent_warnings": intent_warnings,
        "invoice_preview": approval.get("preview"),
    }
    return approval


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def meter_usage_sales_invoices_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Execute an approved metered-usage run and return automatic verification."""
    return batch_create_sales_invoices_from_approval(approval_id)


