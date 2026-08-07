"""Read-only discovery for workflows implemented end to end, plus the playbook."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from ..config import READ_ONLY_ANNOTATIONS
from ..guidance import PLAYBOOK_TOPICS, playbook_topic
from ..workflow_catalogue import get_workflow, list_workflows
from ._registry import mcp

BookkeepingTopic = Annotated[
    Literal[
        "gouden_regels",
        "sync_index",
        "btw",
        "btw_afwikkeling",
        "prive_zakelijk",
        "categoriseren",
        "consistentie",
        "bankmutaties",
        "achterstand",
        "cijfers_uitleggen",
        "meterverbruik",
        "grenzen",
    ],
    Field(
        description=(
            "Which part of the bookkeeping playbook to read. btw = VAT rates, "
            "private use and the rounding rule; btw_afwikkeling = clearing a VAT "
            "period with a journal entry; bankmutaties = why a bank transaction "
            "was not booked automatically; categoriseren = choosing a ledger "
            "account; consistentie = processing a series uniformly; achterstand = "
            "working through a backlog or a whole year; meterverbruik = metered "
            "usage invoicing; grenzen = where to defer to the bookkeeper."
        )
    ),
]


@mcp.tool(
    annotations=READ_ONLY_ANNOTATIONS,
    tags={"domain:core", "capability:read", "discovery"},
)
def get_bookkeeping_guide(topic: BookkeepingTopic) -> dict[str, Any]:
    """Read the Dutch bookkeeping playbook for one topic: btw/VAT rates and the
    rounding rule, btw-aangifte afwikkelen (clearing a VAT period), waarom een
    bankmutatie niet automatisch verwerkt is, grootboek kiezen (categorising),
    consistent verwerken van een reeks facturen, achterstand wegwerken, privé vs
    zakelijk, meterverbruik factureren.

    Read this BEFORE proposing bookkeeping changes in an unfamiliar area. It holds
    the domain rules that the Moneybird API itself does not express — reverse-charge
    VAT, incl/excl price flags, whole-euro declaration rounding, and what booking
    rules (boekingsregels) hide from you. Same content as the
    moneybird://playbook/bookkeeping resource, addressable per topic.
    """
    return playbook_topic(topic)


@mcp.tool(
    annotations=READ_ONLY_ANNOTATIONS,
    tags={"domain:core", "capability:read", "discovery"},
)
def list_bookkeeping_guide_topics() -> dict[str, Any]:
    """List the bookkeeping playbook topics that get_bookkeeping_guide can return."""
    return {
        "topics": {name: summary for name, (_, summary) in PLAYBOOK_TOPICS.items()}
    }


@mcp.tool(
    annotations=READ_ONLY_ANNOTATIONS,
    tags={"domain:workflow", "capability:read", "discovery"},
)
def list_supported_workflows(
    workflow_id: Annotated[
        str,
        Field(description="Optional exact workflow id. When set, return its complete definition."),
    ] = "",
    domain: Annotated[
        str,
        Field(description="Optional exact domain such as products."),
    ] = "",
    risk: Annotated[
        str,
        Field(description="Optional exact risk such as read_only_analysis or financial_write."),
    ] = "",
) -> dict[str, Any]:
    """List dependable outcome workflows or explain one exact workflow id."""
    if workflow_id:
        return {"workflow": get_workflow(workflow_id).to_dict(), "count": 1}
    definitions = list_workflows(domain=domain, risk=risk)
    return {
        "workflows": [workflow.to_dict() for workflow in definitions],
        "count": len(definitions),
        "filters": {"domain": domain or None, "risk": risk or None},
    }
