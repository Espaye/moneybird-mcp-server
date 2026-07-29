"""Task-oriented workflows that group exact guarded bookkeeping actions."""
from __future__ import annotations

from collections import defaultdict
from typing import Annotated, Any, Callable

from pydantic import Field

from ..config import (
    MoneybirdError,
    PREPARE_ANNOTATIONS,
    WRITE_ANNOTATIONS,
)
from ..formatting import duplicate_fingerprint, iso_now
from ..safety import (
    discard_approval,
    make_approval,
    peek_approval,
    pop_approval,
)
from ..task_context import MoneybirdTaskContext
from ._params import ApprovalId
from ._registry import mcp
from . import _context as ctx
from .bank import (
    _preflight_bank_reclassification,
    prepare_reclassify_bank_mutation_bookings,
    reclassify_bank_mutation_bookings_from_approval,
)
from .purchases import (
    _validate_reconcile_preflight,
    prepare_reconcile_purchase_invoice,
    reconcile_purchase_invoice_from_approval,
)

_WORKFLOW_EXECUTORS: dict[str, Callable[[str], dict[str, Any]]] = {
    "reclassify_bank_mutation_bookings": (
        reclassify_bank_mutation_bookings_from_approval
    ),
    "reconcile_purchase_invoice": reconcile_purchase_invoice_from_approval,
}


def _workflow_child_verified(action: str, result: dict[str, Any]) -> bool:
    """Return whether an executed child reported complete post-write verification."""
    if action == "reclassify_bank_mutation_bookings":
        return (
            result.get("status") == "completed"
            and result.get("fully_verified") is True
        )
    if action == "reconcile_purchase_invoice":
        return result.get("status") == "completed"
    return False


def _discard_children(
    children: list[dict[str, Any]],
    administration_id: str,
) -> list[str]:
    discarded: list[str] = []
    for child in children:
        approval_id = str(child["approval_id"])
        if discard_approval(
            approval_id,
            administration_id=administration_id,
        ):
            discarded.append(approval_id)
    return discarded


@mcp.tool(
    annotations=PREPARE_ANNOTATIONS,
    tags={"domain:workflow", "capability:prepare", "always-visible"},
)
def prepare_bookkeeping_correction_batch(
    bank_reclassifications: Annotated[
        list[dict[str, Any]] | None,
        Field(
            description=(
                "Optional direct bank-booking moves. Each dict needs "
                "financial_mutation_id, ledger_account_booking_id, and a target "
                "ledger account id or exact name."
            )
        ),
    ] = None,
    purchase_reconciliations: Annotated[
        list[dict[str, Any]] | None,
        Field(
            description=(
                "Optional purchase-invoice corrections. Each dict accepts the "
                "parameters of prepare_reconcile_purchase_invoice: document_id, "
                "optional kind/reference_document_id/target_total/relabel_period, "
                "or exact desired_lines with prices_are_incl_tax and source_note."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Prepare one exact preview for related invoice and bank corrections.

    The workflow stages the existing guarded actions as private children, then
    presents one combined approval. Execution preflights every child before the
    first write when the plan contains multiple action types. Moneybird has no
    cross-object transaction, so a runtime API failure can still yield an
    explicitly audited partial result.
    """
    bank_entries = list(bank_reclassifications or [])
    purchase_entries = list(purchase_reconciliations or [])
    if not bank_entries and not purchase_entries:
        raise MoneybirdError(
            "Provide bank_reclassifications, purchase_reconciliations, or both."
        )
    if len(purchase_entries) > 20:
        raise MoneybirdError(
            "At most 20 purchase reconciliations can be grouped in one workflow."
        )

    client = ctx.get_client()
    children: list[dict[str, Any]] = []
    previews: list[dict[str, Any]] = []
    try:
        for spec in purchase_entries:
            prepared = prepare_reconcile_purchase_invoice(
                document_id=str(spec.get("document_id") or ""),
                reference_document_id=str(
                    spec.get("reference_document_id") or ""
                ),
                kind=str(spec.get("kind") or "purchase_invoice"),
                target_total=str(spec.get("target_total") or ""),
                relabel_period=bool(spec.get("relabel_period", True)),
                desired_lines=spec.get("desired_lines"),
                prices_are_incl_tax=spec.get("prices_are_incl_tax"),
                source_note=str(spec.get("source_note") or ""),
            )
            children.append(
                {
                    "approval_id": prepared["approval_id"],
                    "action": prepared["action"],
                    "payload": prepared["payload"],
                }
            )
            previews.append(
                {
                    "action": prepared["action"],
                    "summary": prepared["summary"],
                    "preview": prepared["preview"],
                }
            )

        if bank_entries:
            prepared = prepare_reclassify_bank_mutation_bookings(bank_entries)
            children.append(
                {
                    "approval_id": prepared["approval_id"],
                    "action": prepared["action"],
                    "payload": prepared["payload"],
                }
            )
            previews.append(
                {
                    "action": prepared["action"],
                    "summary": prepared["summary"],
                    "preview": prepared["preview"],
                }
            )
    except Exception:
        _discard_children(children, client.administration_id)
        raise

    fingerprint_source = {
        "children": [
            {
                "action": child["action"],
                "payload": child["payload"],
            }
            for child in children
        ]
    }
    fingerprint = duplicate_fingerprint(
        "bookkeeping_correction_batch",
        fingerprint_source,
    )
    payload = {
        "children": children,
        "fingerprint": fingerprint,
    }
    approval = make_approval(
        "bookkeeping_correction_batch",
        payload,
        f"Execute {len(children)} guarded bookkeeping correction action(s)",
    )
    approval["preview"] = {
        "actions": previews,
        "action_count": len(children),
        "safety": {
            "single_combined_approval": True,
            "all_children_preflighted_before_first_write": len(children) > 1,
            "cross_object_transaction_available": False,
            "partial_failures_are_audited": True,
        },
    }
    approval["fingerprint"] = fingerprint
    return approval


def _preflight_workflow_children(
    client: Any,
    children: list[dict[str, Any]],
) -> None:
    task = MoneybirdTaskContext(client)
    bank_items: list[dict[str, Any]] = []
    purchase_children: list[dict[str, Any]] = []

    for child in children:
        action = child["action"]
        approval = peek_approval(
            child["approval_id"],
            administration_id=client.administration_id,
        )
        if approval["action"] != action or approval["payload"] != child["payload"]:
            raise MoneybirdError(
                "A child approval no longer matches the combined preview. "
                "Prepare the workflow again."
            )
        if action == "reclassify_bank_mutation_bookings":
            bank_items.extend(child["payload"]["items"])
        elif action == "reconcile_purchase_invoice":
            purchase_children.append(child)
        else:
            raise MoneybirdError(
                f"Unsupported workflow child action {action!r}."
            )

    if bank_items:
        _preflight_bank_reclassification(client, bank_items, task=task)

    by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for child in purchase_children:
        by_kind[child["payload"]["document_kind"]].append(child)
    for kind, grouped in by_kind.items():
        records = task.documents(
            kind,
            [child["payload"]["document_id"] for child in grouped],
            refresh=True,
        )
        for child in grouped:
            payload = child["payload"]
            _validate_reconcile_preflight(
                records[payload["document_id"]],
                payload,
            )


@mcp.tool(
    annotations=WRITE_ANNOTATIONS,
    tags={"domain:workflow", "capability:execute"},
)
def bookkeeping_correction_batch_from_approval(
    approval_id: ApprovalId,
) -> dict[str, Any]:
    """Execute one explicitly approved combined bookkeeping correction plan."""
    client = ctx.get_client()
    pending = pop_approval(
        approval_id,
        "bookkeeping_correction_batch",
        administration_id=client.administration_id,
    )
    payload = pending["payload"]
    fingerprint = payload["fingerprint"]
    children = payload["children"]
    if ctx.audit_log_contains_success(
        "bookkeeping_correction_batch",
        fingerprint,
    ):
        _discard_children(children, client.administration_id)
        raise MoneybirdError(
            "This combined bookkeeping correction already completed successfully "
            "according to the local audit log."
        )

    try:
        if len(children) > 1:
            _preflight_workflow_children(client, children)
    except Exception as exc:
        discarded = _discard_children(children, client.administration_id)
        ctx.append_failed_audit_log(
            "bookkeeping_correction_batch",
            fingerprint=fingerprint,
            error=str(exc),
            partial={"discarded_child_approvals": discarded},
        )
        raise

    completed: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index, child in enumerate(children):
        executor = _WORKFLOW_EXECUTORS[child["action"]]
        try:
            result = executor(child["approval_id"])
            if not _workflow_child_verified(child["action"], result):
                failures.append(
                    {
                        "action": child["action"],
                        "error": (
                            "Child action completed without full post-write "
                            "verification."
                        ),
                        "result": result,
                    }
                )
                _discard_children(
                    children[index + 1 :],
                    client.administration_id,
                )
                break
            completed.append(
                {
                    "action": child["action"],
                    "status": result.get("status"),
                    "result": result,
                }
            )
        except Exception as exc:
            failures.append(
                {
                    "action": child["action"],
                    "error": str(exc),
                }
            )
            _discard_children(
                children[index + 1 :],
                client.administration_id,
            )
            break

    fully_verified = not failures and len(completed) == len(children)
    ctx.append_audit_log(
        {
            "action": "bookkeeping_correction_batch",
            "fingerprint": fingerprint,
            "result": "success" if fully_verified else "partial_failure",
            "completed_count": len(completed),
            "failure_count": len(failures),
        }
    )
    return {
        "status": (
            "completed"
            if fully_verified
            else "completed_with_errors"
        ),
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "fully_verified": fully_verified,
        "completed": completed,
        "failures": failures,
        "not_started_count": max(
            0,
            len(children) - len(completed) - len(failures),
        ),
        "fingerprint": fingerprint,
    }
