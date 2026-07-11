"""Contact reads and guarded contact writes (create/update/archive, delivery method)."""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ..config import (
    MoneybirdError,
    PREPARE_ANNOTATIONS,
    READ_ONLY_ANNOTATIONS,
    WRITE_ANNOTATIONS,
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
from ..safety import make_approval, pop_approval
from ..invoicing import (
    build_invoice_delivery_audit,
)
from ._params import ApprovalId, ContactId, CustomerId, Limit, Page
from ._registry import mcp
from ._writes import run_approved_write, stage_write
from . import _context as ctx


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
    """Use this before creating a Moneybird contact. Do not execute the write until the user explicitly confirms."""
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


def _contact_result(client, record: dict[str, Any], status: str) -> dict[str, Any]:
    record_id = str(record.get("id"))
    return {
        "_status": status,
        "_audit": {
            "contact_id": record_id,
            "customer_id": record.get("customer_id"),
            **({"archived": record.get("archived")} if status == "archived" else {}),
        },
        "contact": {
            "id": record_id,
            "title": contact_title(record),
            "customer_id": record.get("customer_id"),
            "email": record.get("email"),
            "archived": record.get("archived"),
            "url": api_url("contacts", record_id, client.administration_id),
        },
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_contact_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared contact creation."""
    client = ctx.get_client()
    return run_approved_write(
        client,
        approval_id,
        "create_contact",
        lambda client, payload: _contact_result(
            client,
            client.create_contact(
                {key: value for key, value in payload.items() if key != "fingerprint"}
            ),
            "created",
        ),
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

    payload = {
        "contact_ids": [item["contact_id"] for item in contacts],
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
    pending = pop_approval(approval_id, "set_contacts_delivery_method_email", administration_id=client.administration_id)
    payload = pending["payload"]
    fingerprint = payload["fingerprint"]
    if ctx.audit_log_contains_success("set_contacts_delivery_method_email", fingerprint):
        raise MoneybirdError(
            "This contact delivery-method payload already completed successfully according to the local audit log."
        )

    updated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    try:
        for contact_id in payload["contact_ids"]:
            before = client.get_contact(str(contact_id))
            before_record = contact_delivery_record(before, client.administration_id)
            if before_record["delivery_method"] == "Email":
                skipped.append({**before_record, "reason": "already_email"})
                continue

            record = client.update_contact(str(contact_id), {"delivery_method": "Email"})
            after_record = contact_delivery_record(record, client.administration_id)
            updated.append(
                {
                    **after_record,
                    "delivery_method_before": before_record["delivery_method"],
                    "delivery_method_after": after_record["delivery_method"],
                }
            )
    except Exception as exc:
        ctx.append_failed_audit_log(
            "set_contacts_delivery_method_email",
            fingerprint=fingerprint,
            error=str(exc),
            partial={"updated": updated, "skipped": skipped},
        )
        raise

    verification = build_invoice_delivery_audit(
        client,
        include_archived_contacts=bool(payload.get("include_archived_contacts")),
    )
    ctx.append_audit_log(
        {
            "action": "set_contacts_delivery_method_email",
            "fingerprint": fingerprint,
            "result": "success",
            "updated_count": len(updated),
            "skipped_count": len(skipped),
            "remaining_non_email_contact_count": verification["summary"][
                "non_email_contact_count"
            ],
            "remaining_recurring_issue_count": verification["summary"][
                "recurring_issue_count"
            ],
        }
    )
    return {
        "status": "completed",
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "updated_count": len(updated),
        "skipped_count": len(skipped),
        "updated": updated,
        "skipped": skipped,
        "verification_summary": verification["summary"],
        "remaining_non_email_contacts": verification["non_email_contacts"],
        "remaining_recurring_issues": verification["recurring_issues"],
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
    """Use this before updating a Moneybird contact. Do not execute the write until the user explicitly confirms."""
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

    return stage_write(
        "update_contact",
        summary=(
            f"Update contact {contact_id} fields: "
            + ", ".join(sorted(update_payload.keys()))
        ),
        payload={"contact_id": contact_id, "contact": update_payload},
        preview={"contact_id": contact_id, "contact": update_payload},
    )


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def update_contact_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared contact update."""
    client = ctx.get_client()
    return run_approved_write(
        client,
        approval_id,
        "update_contact",
        lambda client, payload: _contact_result(
            client,
            client.update_contact(payload["contact_id"], payload["contact"]),
            "updated",
        ),
    )


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_archive_contact(contact_id: ContactId) -> dict[str, Any]:
    """Use this before archiving a Moneybird contact. Do not execute the archive until the user explicitly confirms."""
    client = ctx.get_client()
    record = client.get_contact(contact_id)
    return stage_write(
        "archive_contact",
        summary=f"Archive contact {contact_title(record)}",
        payload={"contact_id": contact_id},
        preview={"contact_id": contact_id, "title": contact_title(record)},
    )


def _execute_archive_contact(client, payload: dict[str, Any]) -> dict[str, Any]:
    client.archive_contact(payload["contact_id"])
    record = client.get_contact(payload["contact_id"])
    return _contact_result(client, record, "archived")


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def archive_contact_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared contact archive."""
    client = ctx.get_client()
    return run_approved_write(
        client, approval_id, "archive_contact", _execute_archive_contact
    )


