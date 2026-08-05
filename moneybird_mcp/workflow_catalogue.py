"""Small registry of outcome workflows implemented end to end by this slice."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .config import MoneybirdError


@dataclass(frozen=True)
class WorkflowDefinition:
    id: str
    version: int
    title: str
    domain: str
    risk: str
    mode: str
    intent_examples: tuple[str, ...]
    required_tools: tuple[str, ...]
    required_scope: str
    preconditions: tuple[str, ...]
    verification: tuple[str, ...]
    failure_modes: tuple[str, ...]
    limitations: tuple[str, ...]
    test_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in asdict(self).items()
        }


WORKFLOWS: tuple[WorkflowDefinition, ...] = (
    WorkflowDefinition(
        id="product_inventory_audit",
        version=1,
        title="Audit the product inventory",
        domain="products",
        risk="read_only_analysis",
        mode="read",
        intent_examples=(
            "Find duplicate product SKUs.",
            "Zoek producten zonder btw-tarief of grootboekrekening.",
        ),
        required_tools=("audit_products",),
        required_scope="settings",
        preconditions=(
            "an administration is selected and readable",
            "the maximum product count is explicit",
        ),
        verification=(
            "inspected and finding counts reconcile",
            "every finding contains source product ids and evidence",
        ),
        failure_modes=(
            "settings access refused",
            "product collection truncated",
            "malformed provider data",
        ),
        limitations=(
            "The current product response omits several create/update settings.",
            "Recurring-price and historical-invoice comparisons are separate workflows.",
        ),
        test_status="unit_contract_and_search",
    ),
    WorkflowDefinition(
        id="bulk_update_product_prices",
        version=1,
        title="Bulk-adjust product prices",
        domain="products",
        risk="financial_write",
        mode="analyse_prepare_execute_verify",
        intent_examples=(
            "Increase these products by 4%.",
            "Verhoog deze productprijzen en rond af op 50 cent.",
        ),
        required_tools=(
            "analyse_product_price_adjustment",
            "prepare_bulk_update_product_prices",
            "execute_approved_action",
        ),
        required_scope="settings",
        preconditions=(
            "an administration is selected and readable",
            "selection and one price strategy are explicit",
            "every selected product has a valid id, currency, price, and updated_at",
        ),
        verification=(
            "every product is independently re-read",
            "the exact new price matches",
            "identity and accounting defaults remain unchanged",
        ),
        failure_modes=(
            "stale product version",
            "definitive provider rejection after a known partial update",
            "uncertain write outcome",
            "verification mismatch",
        ),
        limitations=(
            "Updates apply immediately; past or future effective dates are analysis-only.",
            "Product changes do not update invoices, recurring invoices, or subscriptions.",
        ),
        test_status="unit_contract_search_and_guarded_executor",
    ),
)

WORKFLOW_BY_ID = {workflow.id: workflow for workflow in WORKFLOWS}


def get_workflow(workflow_id: str) -> WorkflowDefinition:
    key = str(workflow_id or "").strip()
    try:
        return WORKFLOW_BY_ID[key]
    except KeyError as exc:
        raise MoneybirdError(
            f"Unknown workflow_id {key!r}. Supported workflow ids: "
            f"{', '.join(sorted(WORKFLOW_BY_ID))}."
        ) from exc


def list_workflows(*, domain: str = "", risk: str = "") -> list[WorkflowDefinition]:
    domain_filter = str(domain or "").strip().casefold()
    risk_filter = str(risk or "").strip().casefold()
    return [
        workflow
        for workflow in WORKFLOWS
        if (not domain_filter or workflow.domain.casefold() == domain_filter)
        and (not risk_filter or workflow.risk.casefold() == risk_filter)
    ]


def render_workflow_catalogue_markdown() -> str:
    lines = [
        "# Supported workflow catalogue",
        "",
        "Generated from `moneybird_mcp.workflow_catalogue`; do not edit by hand.",
        "Only workflows integrated and tested end to end are listed.",
        "",
        "| Workflow | Domain | Mode | Risk | Version | Tests |",
        "|---|---|---|---|---:|---|",
    ]
    for workflow in WORKFLOWS:
        lines.append(
            f"| `{workflow.id}` | {workflow.domain} | {workflow.mode} | "
            f"{workflow.risk} | {workflow.version} | {workflow.test_status} |"
        )
    lines.extend(
        [
            "",
            "Use `list_supported_workflows` for the complete preconditions, verification, "
            "failure modes, and limitations. Workflow-specific tools perform their own "
            "concrete administration and record preflight.",
            "",
            "A catalogue entry never grants permission to execute a write.",
        ]
    )
    return "\n".join(lines) + "\n"
