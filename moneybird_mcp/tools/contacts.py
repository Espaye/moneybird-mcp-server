"""Contact reads and guarded contact writes (create/update/archive, delivery method)."""
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
    api_url,
    clean_dict,
    contact_delivery_record,
    contact_title,
    duplicate_fingerprint,
    iso_now,
    render_contact_delivery_table,
    stringify_record,
)
from ..invoicing import (
    build_invoice_delivery_audit,
)
from ..safety import (
    approval_execution_state,
    classify_failed_write,
    make_approval,
    pop_approval,
    record_approval_outcome,
    record_approval_phase,
)
from . import _context as ctx
from ._params import ApprovalId, ContactId, CustomerId, Limit, Page
from ._registry import mcp
from ._writes import (
    mark_write_dispatch_started,
    mark_write_verifying,
    run_approved_write,
    stage_write,
)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_contacts(limit: Limit = 10, page: Page = 1) -> dict[str, Any]:
    """Use this when you need a compact list of Moneybird contacts without opening each record."""
    client = ctx.get_client()
    contacts = client.list_contacts(limit=limit, page=page)
    return {
        "contacts": [
            {
                "id": str(item.get("id")),
                "title": contact_title(item),
                "email": item.get("email"),
                "customer_id": item.get("customer_id"),
                "phone": item.get("phone"),
                "url": api_url("contacts", str(item.get("id")), client.administration_id),
            }
            for item in contacts
        ],
        "page": page,
        "count": len(contacts),
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def audit_invoice_delivery_settings(
    include_archived_contacts: Annotated[bool, Field(description="Also audit archived contacts.")] = False,
    include_inactive_recurring: Annotated[bool, Field(description="Also audit inactive recurring invoice templates.")] = False,
) -> dict[str, Any]:
    """Use this to verify contacts and recurring sales invoices are configured for automatic invoice e-mail delivery."""
    client = ctx.get_client()
    return build_invoice_delivery_audit(
        client,
        include_archived_contacts=include_archived_contacts,
        include_inactive_recurring=include_inactive_recurring,
    )


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_contact_by_customer_id(customer_id: CustomerId) -> dict[str, Any]:
    """Use this when you have your own external customer id and need the matching Moneybird contact."""
    client = ctx.get_client()
    record = client.get_contact_by_customer_id(customer_id)
    record_id = str(record.get("id"))
    return {
        "id": f"contact:{record_id}",
        "title": contact_title(record),
        "text": stringify_record(record),
        "url": api_url("contacts", record_id, client.administration_id),
        "metadata": {
            "kind": "contact",
            "moneybird_id": record_id,
            "customer_id": record.get("customer_id"),
            "administration_id": client.administration_id,
        },
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_create_contact(
    company_name: Annotated[str, Field(description="Company name. Give this and/or firstname+lastname.")] = "",
    firstname: str = "",
    lastname: str = "",
    email: Annotated[str, Field(description="Email address; Moneybird also uses it for invoice delivery.")] = "",
    customer_id: Annotated[str, Field(description="Optional human-facing customer number; empty = Moneybird assigns one.")] = "",
    phone: str = "",
    address1: Annotated[str, Field(description="Street and house number.")] = "",
    zipcode: str = "",
    city: str = "",
    country: Annotated[str, Field(description="ISO 3166-1 alpha-2 country code.")] = "NL",
) -> dict[str, Any]:
    """Create a new customer. Add a contact, client, supplier, or vendor. Dutch: klant toevoegen, leverancier toevoegen. Requires explicit approval before execution."""
    ctx.get_client()  # Resolve and bind the active administration to the approval.
    payload = clean_dict(
        {
            "company_name": company_name,
            "firstname": firstname,
            "lastname": lastname,
            "email": email,
            "customer_id": customer_id,
            "phone": phone,
            "address1": address1,
            "zipcode": zipcode,
            "city": city,
            "country": country,
        }
    )
    if not payload:
        raise MoneybirdError("At least one contact field is required.")

    summary_name = company_name or " ".join(part for part in [firstname, lastname] if part).strip()
    return stage_write(
        "create_contact",
        summary=f"Create contact '{summary_name or 'unnamed contact'}'",
        payload=payload,
        preview=payload,
    )


def _contact_result(
    client,
    record: dict[str, Any],
    status: str,
    *,
    expected_fields: dict[str, Any] | None = None,
    expected_record_id: str = "",
) -> dict[str, Any]:
    record_id = str(record.get("id") or "")
    record_id_matches = (
        not expected_record_id or record_id == str(expected_record_id)
    )
    field_mismatches = {
        key: {"expected": expected, "actual": record.get(key)}
        for key, expected in (expected_fields or {}).items()
        if str(record.get(key) or "") != str(expected or "")
    }
    verified = record_id_matches and (
        record.get("archived") is True
        if status == "archived"
        else not field_mismatches
    )
    return {
        "_status": status if verified else "completed_with_verification_errors",
        "_audit_result": "success" if verified else "verification_failed",
        "_audit": {
            "contact_id": record_id,
            "customer_id": record.get("customer_id"),
            **({"archived": record.get("archived")} if status == "archived" else {}),
            "fully_verified": verified,
        },
        "contact": {
            "id": record_id,
            "title": contact_title(record),
            "customer_id": record.get("customer_id"),
            "email": record.get("email"),
            "archived": record.get("archived"),
            "url": api_url("contacts", record_id, client.administration_id),
        },
        "verification": {
            "independent_post_read": True,
            "requested_fields_match": not field_mismatches,
            "record_id_matches": record_id_matches,
            "field_mismatches": field_mismatches,
        },
    }


def _execute_create_contact(client, payload: dict[str, Any]) -> dict[str, Any]:
    requested = {
        key: value for key, value in payload.items() if key != "fingerprint"
    }
    mark_write_dispatch_started()
    created = client.create_contact(requested)
    record_id = str(created.get("id") or "")
    if not record_id:
        raise MoneybirdError(
            "Moneybird did not return a contact id; reconcile before retrying."
        )
    mark_write_verifying()
    record = client.get_contact(record_id)
    return _contact_result(
        client,
        record,
        "created",
        expected_fields=requested,
        expected_record_id=record_id,
    )


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_contact_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared contact creation."""
    client = ctx.get_client()
    return run_approved_write(
        client,
        approval_id,
        "create_contact",
        _execute_create_contact,
    )


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_set_contacts_delivery_method_email(
    include_archived_contacts: Annotated[bool, Field(description="Also fix archived contacts.")] = False,
) -> dict[str, Any]:
    """Use this before bulk-changing Moneybird contacts so invoice delivery_method becomes Email. Do not execute the write until the user explicitly confirms."""
    client = ctx.get_client()
    audit = build_invoice_delivery_audit(
        client,
        include_archived_contacts=include_archived_contacts,
    )
    contacts = audit["non_email_contacts"]
    if not contacts:
        return {
            "status": "no_changes_needed",
            "summary": "All checked contacts already have delivery_method Email.",
            "audit_summary": audit["summary"],
            "non_email_contacts": [],
        }

    items: list[dict[str, Any]] = []
    for contact in contacts:
        record = client.get_contact(str(contact["contact_id"]))
        items.append(
            {
                "contact_id": str(contact["contact_id"]),
                "expected_record": {
                    key: record.get(key)
                    for key in ("version", "updated_at", "delivery_method", "archived")
                    if record.get(key) is not None
                },
            }
        )
    payload = {
        "items": items,
        "include_archived_contacts": include_archived_contacts,
    }
    fingerprint = duplicate_fingerprint(
        "set_contacts_delivery_method_email",
        payload,
    )
    payload["fingerprint"] = fingerprint
    summary = f"Set delivery_method Email for {len(contacts)} contact(s)"
    approval = make_approval(
        "set_contacts_delivery_method_email",
        payload,
        summary,
    )
    approval["payload"] = payload
    approval["preview"] = {
        "contact_count": len(contacts),
        "preview_table": render_contact_delivery_table(contacts),
        "contacts": contacts,
        "audit_summary": audit["summary"],
    }
    return approval


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def set_contacts_delivery_method_email_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed bulk-updating contact invoice delivery methods to Email."""
    client = ctx.get_client()
    require_write_capability(action="set_contacts_delivery_method_email")
    pending = pop_approval(approval_id, "set_contacts_delivery_method_email", administration_id=client.administration_id)
    payload = pending["payload"]
    fingerprint = payload["fingerprint"]
    if ctx.audit_log_contains_success("set_contacts_delivery_method_email", fingerprint):
        record_approval_outcome(
            approval_id,
            "duplicate_suppressed",
            administration_id=client.administration_id,
        )
        raise MoneybirdError(
            "This contact delivery-method payload already completed successfully according to the local audit log."
        )

    updated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    verification: list[dict[str, Any]] = []
    writes_applied = 0
    try:
        # Bind every selected contact before the first update.
        before_by_id: dict[str, dict[str, Any]] = {}
        for item in payload.get("items") or []:
            contact_id = str(item["contact_id"])
            before = client.get_contact(contact_id)
            changed = {
                key: {"expected": value, "actual": before.get(key)}
                for key, value in item.get("expected_record", {}).items()
                if str(before.get(key) or "") != str(value or "")
            }
            if str(before.get("id") or "") != contact_id:
                changed["id"] = {
                    "expected": contact_id,
                    "actual": before.get("id"),
                }
            if changed:
                raise MoneybirdError(
                    f"Contact {contact_id} changed after preview: {changed}. Prepare again."
                )
            before_by_id[contact_id] = before

        if payload.get("items"):
            record_approval_phase(
                approval_id,
                "dispatching",
                administration_id=client.administration_id,
            )
        for item in payload.get("items") or []:
            contact_id = str(item["contact_id"])
            before = before_by_id[contact_id]
            before_record = contact_delivery_record(before, client.administration_id)
            if before_record["delivery_method"] == "Email":
                skipped.append({**before_record, "reason": "already_email"})
                continue

            client.update_contact(contact_id, {"delivery_method": "Email"})
            writes_applied += 1
            record = client.get_contact(contact_id)
            after_record = contact_delivery_record(record, client.administration_id)
            updated.append(
                {
                    **after_record,
                    "delivery_method_before": before_record["delivery_method"],
                    "delivery_method_after": after_record["delivery_method"],
                }
            )
            verification.append(
                {
                    "contact_id": contact_id,
                    "delivery_method": record.get("delivery_method"),
                    "record_id_matches": str(record.get("id") or "")
                    == contact_id,
                    "fully_verified": (
                        str(record.get("id") or "") == contact_id
                        and record.get("delivery_method") == "Email"
                    ),
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
            "set_contacts_delivery_method_email",
            fingerprint=fingerprint,
            error=str(exc),
            partial={
                "writes_applied": writes_applied,
                "updated": updated,
                "skipped": skipped,
            },
            result=audit_result,
        )
        raise

    fully_verified = (
        len(verification) + len(skipped) == len(payload.get("items") or [])
        and all(item["fully_verified"] for item in verification)
    )
    audit_result = "success" if fully_verified else "verification_failed"
    record_approval_outcome(
        approval_id,
        audit_result,
        administration_id=client.administration_id,
    )
    ctx.append_audit_log(
        {
            "action": "set_contacts_delivery_method_email",
            "fingerprint": fingerprint,
            "result": audit_result,
            "updated_count": len(updated),
            "skipped_count": len(skipped),
            "verified_count": sum(
                1 for item in verification if item["fully_verified"]
            ),
        }
    )
    return {
        "status": (
            "completed"
            if fully_verified
            else "completed_with_verification_errors"
        ),
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "updated_count": len(updated),
        "skipped_count": len(skipped),
        "updated": updated,
        "skipped": skipped,
        "verification": verification,
        "verification_summary": verification["summary"],
        "remaining_non_email_contacts": verification["non_email_contacts"],
        "remaining_recurring_issues": verification["recurring_issues"],
        "fully_verified": fully_verified,
        "fingerprint": fingerprint,
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_update_contact(
    contact_id: ContactId,
    company_name: str = "",
    firstname: str = "",
    lastname: str = "",
    email: str = "",
    phone: str = "",
    customer_id: Annotated[str, Field(description="New human-facing customer number.")] = "",
    address1: Annotated[str, Field(description="Street and house number.")] = "",
    zipcode: str = "",
    city: str = "",
    country: Annotated[str, Field(description="ISO 3166-1 alpha-2 country code.")] = "",
    send_invoices_to_email: Annotated[str, Field(description="Email address invoices are sent to (may differ from the main email).")] = "",
    delivery_method: Annotated[str, Field(description="Invoice delivery method: 'Email', 'Simplerinvoicing', 'Post', or 'Manual'.")] = "",
    clear_fields: Annotated[list[str] | None, Field(description="Field names to blank on the contact, e.g. ['send_invoices_to_email']. Empty fields elsewhere mean 'keep current value'.")] = None,
) -> dict[str, Any]:
    """Use this to change or edit an existing contact: update a company's or person's name, address, email, phone, or delivery method. Do not execute the write until the user explicitly confirms."""
    allowed_clear_fields = {
        "company_name",
        "firstname",
        "lastname",
        "email",
        "phone",
        "customer_id",
        "address1",
        "zipcode",
        "city",
        "country",
        "send_invoices_to_email",
    }
    update_payload = clean_dict(
        {
            "company_name": company_name,
            "firstname": firstname,
            "lastname": lastname,
            "email": email,
            "phone": phone,
            "customer_id": customer_id,
            "address1": address1,
            "zipcode": zipcode,
            "city": city,
            "country": country,
            "send_invoices_to_email": send_invoices_to_email,
            "delivery_method": delivery_method,
        }
    )

    clear_fields = clear_fields or []
    invalid_fields = [field for field in clear_fields if field not in allowed_clear_fields]
    if invalid_fields:
        raise MoneybirdError(
            f"Unsupported clear_fields: {', '.join(sorted(invalid_fields))}"
        )
    for field in clear_fields:
        update_payload[field] = ""

    if not update_payload:
        raise MoneybirdError("Provide at least one field to update or clear.")

    client = ctx.get_client()
    current = client.get_contact(contact_id)
    expected_record = {
        key: current.get(key)
        for key in ("version", "updated_at")
        if current.get(key) is not None
    }
    before = {key: current.get(key) for key in update_payload}
    return stage_write(
        "update_contact",
        summary=(
            f"Update contact {contact_id} fields: "
            + ", ".join(sorted(update_payload.keys()))
        ),
        payload={
            "contact_id": contact_id,
            "contact": update_payload,
            "expected_record": expected_record,
        },
        preview={
            "contact_id": contact_id,
            "before": before,
            "after": update_payload,
            "expected_record": expected_record,
        },
    )


def _execute_update_contact(client, payload: dict[str, Any]) -> dict[str, Any]:
    contact_id = payload["contact_id"]
    before = client.get_contact(contact_id)
    expected_record = payload.get("expected_record") or {}
    changed_preconditions = {
        key: {"expected": expected, "actual": before.get(key)}
        for key, expected in expected_record.items()
        if str(before.get(key) or "") != str(expected or "")
    }
    if str(before.get("id") or "") != str(contact_id):
        changed_preconditions["id"] = {
            "expected": contact_id,
            "actual": before.get("id"),
        }
    if changed_preconditions:
        return {
            "_status": "precondition_failed",
            "_audit_result": "failed_pre_write",
            "_audit": {
                "contact_id": contact_id,
                "precondition_failed": True,
            },
            "verification": {
                "write_dispatched": False,
                "changed_preconditions": changed_preconditions,
            },
        }

    mark_write_dispatch_started()
    client.update_contact(contact_id, payload["contact"])
    mark_write_verifying()
    after = client.get_contact(contact_id)
    return _contact_result(
        client,
        after,
        "updated",
        expected_fields=payload["contact"],
        expected_record_id=contact_id,
    )


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def update_contact_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared contact update."""
    client = ctx.get_client()
    return run_approved_write(
        client,
        approval_id,
        "update_contact",
        _execute_update_contact,
    )


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_archive_contact(contact_id: ContactId) -> dict[str, Any]:
    """Use this before archiving a Moneybird contact. Archiving does not close its open invoices, and this server has no unarchive action. Do not execute the archive until the user explicitly confirms."""
    client = ctx.get_client()
    record = client.get_contact(contact_id)
    expected_record = {
        key: record.get(key)
        for key in ("version", "updated_at", "archived")
        if record.get(key) is not None
    }
    return stage_write(
        "archive_contact",
        summary=f"Archive contact {contact_title(record)}",
        payload={
            "contact_id": contact_id,
            "expected_record": expected_record,
        },
        preview={
            "contact_id": contact_id,
            "title": contact_title(record),
            "expected_record": expected_record,
            "warnings": [
                "Archiving does not close or cancel this contact's open sales or "
                "purchase invoices.",
                "This MCP server has no unarchive tool, so the archive cannot be "
                "undone through this server.",
            ],
        },
    )


def _execute_archive_contact(client, payload: dict[str, Any]) -> dict[str, Any]:
    contact_id = payload["contact_id"]
    before = client.get_contact(contact_id)
    changed_preconditions = {
        key: {"expected": expected, "actual": before.get(key)}
        for key, expected in (payload.get("expected_record") or {}).items()
        if str(before.get(key) or "") != str(expected or "")
    }
    if str(before.get("id") or "") != str(contact_id):
        changed_preconditions["id"] = {
            "expected": contact_id,
            "actual": before.get("id"),
        }
    if changed_preconditions:
        return {
            "_status": "precondition_failed",
            "_audit_result": "failed_pre_write",
            "_audit": {
                "contact_id": contact_id,
                "precondition_failed": True,
            },
            "verification": {
                "write_dispatched": False,
                "changed_preconditions": changed_preconditions,
            },
        }
    mark_write_dispatch_started()
    client.archive_contact(contact_id)
    mark_write_verifying()
    record = client.get_contact(contact_id)
    return _contact_result(
        client,
        record,
        "archived",
        expected_record_id=contact_id,
    )


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def archive_contact_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared contact archive."""
    client = ctx.get_client()
    return run_approved_write(
        client, approval_id, "archive_contact", _execute_archive_contact
    )


