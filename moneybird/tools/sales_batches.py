"""Batch sales-invoice flows: batch create/update/schedule and the meter-usage run."""
from __future__ import annotations

from typing import Any

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
    render_preview_table,
)
from ..safety import make_approval, pop_approval
from ..invoicing import (
    apply_batch_group_merge_checks,
    build_batch_invoice_payload,
    build_meter_usage_entries,
    build_merge_snapshot_from_invoice,
    evaluate_merge_compatibility,
    list_scheduled_merge_candidates,
    summarize_batch_preview,
)
from ._registry import mcp
from . import _context as ctx


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
    entries: list[dict[str, Any]],
    skip_if_duplicate: bool = True,
    fail_on_duplicate: bool = False,
) -> dict[str, Any]:
    """Use this before creating multiple sales invoices in one batch. It returns a preview table, duplicate warnings, and an automatic merge-compatibility check before any write happens."""
    return _prepare_batch_create_sales_invoices(
        ctx.get_client(),
        entries,
        skip_if_duplicate=skip_if_duplicate,
        fail_on_duplicate=fail_on_duplicate,
    )


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def batch_create_sales_invoices_from_approval(approval_id: str) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared batch invoice creation."""
    client = ctx.get_client()
    pending = pop_approval(approval_id, "batch_create_sales_invoices", administration_id=client.administration_id)
    payload = pending["payload"]
    fingerprint = payload["fingerprint"]
    if ctx.audit_log_contains_success("batch_create_sales_invoices", fingerprint):
        raise MoneybirdError(
            "This batch payload already completed successfully according to the local audit log."
        )

    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
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

            record = client.create_sales_invoice(item["sales_invoice"])
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
            if item["schedule_send_on"]:
                record = client.send_sales_invoice(str(record["id"]), item["send_payload"])
                result_row.update(
                    {
                        "state": record.get("state"),
                        "invoice_date": record.get("invoice_date"),
                        "sent_at": record.get("sent_at"),
                    }
                )
            created.append(result_row)
    except Exception as exc:
        ctx.append_failed_audit_log(
            "batch_create_sales_invoices",
            fingerprint=fingerprint,
            error=str(exc),
            partial={"created": created, "skipped": skipped},
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
        checks = {
            "total_matches": str(invoice.get("total_price_incl_tax"))
            == str(row.get("expected_total_incl_tax")),
            "state_matches": str(invoice.get("state")) == str(row.get("expected_state")),
            "invoice_date_matches": (
                not row.get("expected_invoice_date")
                or str(invoice.get("invoice_date")) == str(row.get("expected_invoice_date"))
            ),
            "not_sent_yet": invoice.get("sent_at") in (None, ""),
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
                "verified": all(checks.values()),
            }
        )
    all_verified = all(row["verified"] for row in verification)

    ctx.append_audit_log(
        {
            "action": "batch_create_sales_invoices",
            "fingerprint": fingerprint,
            "result": "success",
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
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Use this before updating one or more existing sales invoices, either by explicit invoice id or by customer lookup plus filters."""
    if not entries:
        raise MoneybirdError("Provide at least one batch update entry.")

    client = ctx.get_client()
    prepared_items: list[dict[str, Any]] = []
    preview_rows: list[dict[str, Any]] = []

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
                "sales_invoice_id": str(invoice["id"]),
                "invoice_id": invoice.get("invoice_id"),
                "customer_id": invoice.get("contact", {}).get("customer_id"),
                "patch": sales_invoice_patch,
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
def batch_update_sales_invoices_from_approval(approval_id: str) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared batch invoice update."""
    client = ctx.get_client()
    pending = pop_approval(approval_id, "batch_update_sales_invoices", administration_id=client.administration_id)
    payload = pending["payload"]
    fingerprint = payload["fingerprint"]
    if ctx.audit_log_contains_success("batch_update_sales_invoices", fingerprint):
        raise MoneybirdError(
            "This batch update payload already completed successfully according to the local audit log."
        )

    updated: list[dict[str, Any]] = []
    try:
        for item in payload["items"]:
            record = client.update_sales_invoice(item["sales_invoice_id"], item["patch"])
            updated.append(
                {
                    "sales_invoice_id": str(record.get("id")),
                    "invoice_id": record.get("invoice_id"),
                    "customer_id": record.get("contact", {}).get("customer_id"),
                    "state": record.get("state"),
                }
            )
    except Exception as exc:
        ctx.append_failed_audit_log(
            "batch_update_sales_invoices",
            fingerprint=fingerprint,
            error=str(exc),
            partial={"updated": updated},
        )
        raise

    ctx.append_audit_log(
        {
            "action": "batch_update_sales_invoices",
            "fingerprint": fingerprint,
            "result": "success",
            "updated": updated,
        }
    )
    return {
        "status": "completed",
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "updated": updated,
        "fingerprint": fingerprint,
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_batch_schedule_sales_invoices(
    entries: list[dict[str, Any]],
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
def batch_schedule_sales_invoices_from_approval(approval_id: str) -> dict[str, Any]:
    """Schedule a prepared invoice batch and verify every resulting invoice."""
    client = ctx.get_client()
    pending = pop_approval(
        approval_id,
        "batch_schedule_sales_invoices",
        administration_id=client.administration_id,
    )
    payload = pending["payload"]
    fingerprint = payload["fingerprint"]
    if ctx.audit_log_contains_success("batch_schedule_sales_invoices", fingerprint):
        raise MoneybirdError(
            "This schedule batch already completed successfully according to the local audit log."
        )

    scheduled: list[dict[str, Any]] = []
    try:
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
            record = client.send_sales_invoice(
                item["sales_invoice_id"],
                item["sales_invoice_sending"],
            )
            scheduled.append(
                {
                    "sales_invoice_id": str(record.get("id")),
                    "customer_id": item.get("customer_id"),
                    "action": "scheduled",
                }
            )
    except Exception as exc:
        ctx.append_failed_audit_log(
            "batch_schedule_sales_invoices",
            fingerprint=fingerprint,
            error=str(exc),
            partial={"scheduled": scheduled},
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
            "total_unchanged": str(invoice.get("total_price_incl_tax"))
            == str(item.get("before_total_price_incl_tax")),
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
    ctx.append_audit_log(
        {
            "action": "batch_schedule_sales_invoices",
            "fingerprint": fingerprint,
            "result": "success",
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
    rows: list[dict[str, Any]],
    period_label: str,
    invoice_date: str,
    schedule_send_on: str = "",
    minimum_usage_kwh: str = "0",
    description_prefix: str = "Elektra",
    default_unit_price: str = "",
    default_tax_rate_id: str = "",
    default_ledger_account_id: str = "",
    skip_meters: list[str] | None = None,
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
def meter_usage_sales_invoices_from_approval(approval_id: str) -> dict[str, Any]:
    """Execute an approved metered-usage run and return automatic verification."""
    return batch_create_sales_invoices_from_approval(approval_id)


