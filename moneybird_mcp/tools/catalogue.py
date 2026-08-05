"""Read-only discovery for workflows implemented end to end."""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ..config import READ_ONLY_ANNOTATIONS
from ..workflow_catalogue import get_workflow, list_workflows
from ._registry import mcp


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
