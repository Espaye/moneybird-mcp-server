"""One stable executor for every guarded approval action."""
from __future__ import annotations

from typing import Any, Callable

from ..capabilities import require_write_capability
from ..config import WRITE_ANNOTATIONS, MoneybirdError
from ..credentials import CREDENTIAL_MODE_HOSTED_REQUEST_ONLY, get_credential_mode
from ..safety import peek_approval
from ..write_contracts import WRITE_SPECS
from . import _context as ctx
from ._params import ApprovalId
from ._registry import mcp
from .bank import (
    link_bank_mutation_booking_from_approval,
    reclassify_bank_mutation_bookings_from_approval,
    unlink_bank_mutation_booking_from_approval,
)
from .contacts import (
    archive_contact_from_approval,
    create_contact_from_approval,
    set_contacts_delivery_method_email_from_approval,
    update_contact_from_approval,
)
from .ledger import (
    create_general_journal_document_from_approval,
    create_ledger_account_from_approval,
    reclassify_document_lines_from_approval,
    vat_settlement_journal_from_approval,
)
from .payments import register_payment_from_approval
from .products import bulk_update_product_prices_from_approval
from .purchases import reconcile_purchase_invoice_from_approval
from .sales import (
    create_credit_invoice_from_approval,
    create_sales_invoice_draft_from_approval,
    pause_sales_invoice_workflow_from_approval,
    resume_sales_invoice_workflow_from_approval,
    send_sales_invoice_from_approval,
)
from .sales_batches import (
    batch_create_sales_invoices_from_approval,
    batch_schedule_sales_invoices_from_approval,
    batch_update_sales_invoices_from_approval,
)
from .workflows import bookkeeping_correction_batch_from_approval

ApprovalExecutor = Callable[[str], dict[str, Any]]

APPROVAL_EXECUTORS: dict[str, ApprovalExecutor] = {
    "archive_contact": archive_contact_from_approval,
    "batch_create_sales_invoices": batch_create_sales_invoices_from_approval,
    "batch_schedule_sales_invoices": batch_schedule_sales_invoices_from_approval,
    "batch_update_sales_invoices": batch_update_sales_invoices_from_approval,
    "bookkeeping_correction_batch": (
        bookkeeping_correction_batch_from_approval
    ),
    "bulk_update_product_prices": bulk_update_product_prices_from_approval,
    "create_contact": create_contact_from_approval,
    "create_credit_invoice": create_credit_invoice_from_approval,
    "create_general_journal_document": (
        create_general_journal_document_from_approval
    ),
    "create_ledger_account": create_ledger_account_from_approval,
    "create_sales_invoice_draft": create_sales_invoice_draft_from_approval,
    "link_bank_mutation_booking": link_bank_mutation_booking_from_approval,
    "pause_sales_invoice_workflow": pause_sales_invoice_workflow_from_approval,
    "reclassify_bank_mutation_bookings": (
        reclassify_bank_mutation_bookings_from_approval
    ),
    "reclassify_document_lines": reclassify_document_lines_from_approval,
    "reconcile_purchase_invoice": reconcile_purchase_invoice_from_approval,
    "register_payment": register_payment_from_approval,
    "resume_sales_invoice_workflow": resume_sales_invoice_workflow_from_approval,
    "send_sales_invoice": send_sales_invoice_from_approval,
    "set_contacts_delivery_method_email": (
        set_contacts_delivery_method_email_from_approval
    ),
    "settle_vat_period": vat_settlement_journal_from_approval,
    "unlink_bank_mutation_booking": unlink_bank_mutation_booking_from_approval,
    "update_contact": update_contact_from_approval,
}

if set(APPROVAL_EXECUTORS) != set(WRITE_SPECS):
    missing_specs = sorted(set(APPROVAL_EXECUTORS) - set(WRITE_SPECS))
    orphan_specs = sorted(set(WRITE_SPECS) - set(APPROVAL_EXECUTORS))
    raise RuntimeError(
        "Approval executor / WriteSpec registry mismatch: "
        f"missing_specs={missing_specs}, orphan_specs={orphan_specs}"
    )


@mcp.tool(
    annotations=WRITE_ANNOTATIONS,
    tags={"domain:core", "capability:execute", "always-visible"},
)
def execute_approved_action(approval_id: ApprovalId) -> dict[str, Any]:
    """Execute the exact pending action behind an approval id.

    Call this only after the user has explicitly confirmed the preview returned
    by the matching prepare tool. The approval remains tenant-bound, expiring,
    and single-use; this tool only removes the need to rediscover a separate
    action-specific ``*_from_approval`` tool.
    """
    # Hosted mode has no local principal-bound approval state and must fail before
    # touching credentials or the approvals database. Local read-only attempts
    # resolve their pending action first so the policy audit names that action.
    if get_credential_mode() == CREDENTIAL_MODE_HOSTED_REQUEST_ONLY:
        require_write_capability(action="execute_approved_action")
    client = ctx.get_client()
    pending = peek_approval(
        approval_id,
        administration_id=client.administration_id,
    )
    action = str(pending["action"])
    require_write_capability(action=action)
    executor = APPROVAL_EXECUTORS.get(action)
    if executor is None:
        raise MoneybirdError(
            f"Approval action '{action}' has no generic executor. "
            "Use its action-specific *_from_approval tool."
        )
    return executor(approval_id)
