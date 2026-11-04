"""Project writes: create, rename or re-budget, and delete or archive a project.

Moneybird projects work as cost centres on document lines. Every call here
needs the ``settings`` scope. A project name must be unique within the
administration, archived projects included, so both create and rename check the
complete ``state:all`` listing before anything is staged.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field

from ..config import PREPARE_ANNOTATIONS, MoneybirdError, MoneybirdHTTPError
from ..formatting import duplicate_fingerprint
from ..projects import fetch_all_projects
from . import _context as ctx
from ._params import ApprovalId
from ._registry import mcp
from ._writes import (
    mark_write_dispatch_started,
    mark_write_verifying,
    run_approved_write,
    stage_write,
)

BudgetHours = Annotated[
    int | None,
    Field(
        ge=1,
        description=(
            "Optional budget in whole hours. Leave empty for no budget "
            "(or, on an update, to keep the current one)."
        ),
    ),
]


def _project_occurrence(record: dict[str, Any]) -> dict[str, Any]:
    budget = record.get("budget")
    if budget not in (None, ""):
        # Moneybird returns whole hours; anything else is kept as given, so a
        # comparison with a requested whole number fails rather than rounds.
        amount = Decimal(str(budget))
        budget = int(amount) if amount == amount.to_integral_value() else str(budget)
    return {
        "id": str(record.get("id") or ""),
        "name": str(record.get("name") or ""),
        "state": str(record.get("state") or ""),
        "budget": None if budget in (None, "") else budget,
    }


def _name_conflicts(
    projects: list[dict[str, Any]], name: str, *, except_id: str = ""
) -> list[dict[str, Any]]:
    """Projects other than ``except_id`` whose name equals ``name``, ignoring case."""
    wanted = name.casefold()
    return [
        _project_occurrence(item)
        for item in projects
        if str(item.get("id") or "") != except_id
        and str(item.get("name") or "").strip().casefold() == wanted
    ]


def _refuse_name_conflicts(
    projects: list[dict[str, Any]], name: str, *, except_id: str = ""
) -> None:
    conflicts = _name_conflicts(projects, name, except_id=except_id)
    if conflicts:
        existing = ", ".join(
            f"'{item['name']}' ({item['id']}, {item['state'] or 'unknown state'})"
            for item in conflicts
        )
        raise MoneybirdError(
            f"A project named '{name}' already exists: {existing}. Project names "
            "must be unique within the administration, archived projects included."
        )


def _resolve_project(
    client: Any, project_id: str, project_name: str
) -> dict[str, Any]:
    """One project by id or exact name, in any state."""
    project_id = project_id.strip()
    project_name = project_name.strip()
    if bool(project_id) == bool(project_name):
        raise MoneybirdError("Provide exactly one of project_id or project_name.")
    if project_id:
        return client.get_project(project_id)
    matches = [
        item
        for item in fetch_all_projects(client)
        if str(item.get("name") or "") == project_name
    ]
    if len(matches) != 1:
        raise MoneybirdError(
            f"Expected exactly one project named '{project_name}', got {len(matches)}."
        )
    return client.get_project(str(matches[0].get("id")))


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_create_project(
    name: Annotated[
        str, Field(description="Project name; unique within the administration.")
    ],
    budget_hours: BudgetHours = None,
) -> dict[str, Any]:
    """Use this before creating a Moneybird project (cost centre). Do not execute the write until the user explicitly confirms.

    A name already used by another project, archived ones included and ignoring
    case, is refused."""
    name = name.strip()
    if not name:
        raise MoneybirdError("name is required.")
    client = ctx.get_client()
    _refuse_name_conflicts(fetch_all_projects(client), name)
    project: dict[str, Any] = {"name": name}
    if budget_hours is not None:
        project["budget"] = budget_hours
    return stage_write(
        "create_project",
        summary=f"Create project '{name}'",
        payload={"project": project},
        preview={
            "project": {**project, "budget": budget_hours},
            "planned_api_actions": [
                "GET /projects?filter=state:all complete name check",
                "POST /projects",
                "GET /projects/{id} read-after-write",
            ],
        },
        fingerprint=duplicate_fingerprint("create_project", project),
    )


def _execute_create_project(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    project = payload["project"]
    conflicts = _name_conflicts(fetch_all_projects(client), project["name"])
    if conflicts:
        return {
            "_status": "precondition_failed",
            "_audit_result": "failed_pre_write",
            "_audit": {"name": project["name"]},
            "error": (
                "A project with this name was created after preview; no write was sent."
            ),
            "conflicting_projects": conflicts,
        }

    mark_write_dispatch_started()
    created = client.create_project(project)
    project_id = str(created.get("id") or "")
    if not project_id:
        raise MoneybirdError(
            "Moneybird did not return a project id; reconcile before retrying."
        )
    mark_write_verifying()
    after = _project_occurrence(client.get_project(project_id))
    expected = {
        "id": project_id,
        "name": project["name"],
        "state": "active",
        "budget": project.get("budget"),
    }
    mismatches = {
        key: {"expected": value, "actual": after[key]}
        for key, value in expected.items()
        if after[key] != value
    }
    fully_verified = not mismatches
    return {
        "_status": "created" if fully_verified else "completed_with_verification_errors",
        "_audit_result": "success" if fully_verified else "verification_failed",
        "_audit": {
            "project_id": project_id,
            "name": after["name"],
            "fully_verified": fully_verified,
        },
        "project": after,
        "verification": {
            "independent_post_read": True,
            "field_mismatches": mismatches,
            "fully_verified": fully_verified,
        },
    }


# Not registered as an MCP tool: every approved action executes through the single
# annotated execute_approved_action entry point.
def create_project_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Execute one explicitly approved project creation."""
    return run_approved_write(
        ctx.get_client(), approval_id, "create_project", _execute_create_project
    )


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_update_project(
    project_id: Annotated[
        str, Field(description="Exact Moneybird project id; or give project_name.")
    ] = "",
    project_name: Annotated[
        str, Field(description="Exact current project name; or give project_id.")
    ] = "",
    name: Annotated[
        str, Field(description="Optional new name; leave empty to keep the current one.")
    ] = "",
    budget_hours: BudgetHours = None,
    clear_budget: Annotated[
        bool, Field(description="Remove the project's budget. Not with budget_hours.")
    ] = False,
) -> dict[str, Any]:
    """Use this before renaming a Moneybird project or changing its budget. Do not execute the write until the user explicitly confirms.

    The new name must not be used by another project, archived ones included and
    ignoring case. The project's state is not changed."""
    if budget_hours is not None and clear_budget:
        raise MoneybirdError("Give budget_hours or clear_budget, not both.")
    client = ctx.get_client()
    before = _project_occurrence(_resolve_project(client, project_id, project_name))
    new_name = name.strip() or before["name"]
    if clear_budget:
        new_budget = None
    elif budget_hours is not None:
        new_budget = budget_hours
    else:
        new_budget = before["budget"]
    if new_name == before["name"] and new_budget == before["budget"]:
        raise MoneybirdError(
            f"Nothing to change: project '{before['name']}' already has this name "
            "and budget."
        )
    if new_name != before["name"]:
        _refuse_name_conflicts(
            fetch_all_projects(client), new_name, except_id=before["id"]
        )
    # Moneybird requires the name on every update, so it is always sent; the
    # budget only when it changes.
    patch: dict[str, Any] = {"name": new_name}
    if new_budget != before["budget"]:
        patch["budget"] = new_budget
    payload = {"project_occurrence": before, "project": patch}
    return stage_write(
        "update_project",
        summary=f"Update project '{before['name']}'",
        payload=payload,
        preview={
            "before": before,
            "after": {**before, "name": new_name, "budget": new_budget},
            "planned_api_actions": [
                "GET /projects/{id} precondition",
                "PATCH /projects/{id}",
                "GET /projects/{id} read-after-write",
            ],
        },
        fingerprint=duplicate_fingerprint("update_project", payload),
    )


def _execute_update_project(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    before = payload["project_occurrence"]
    patch = payload["project"]
    project_id = before["id"]
    if _project_occurrence(client.get_project(project_id)) != before:
        return {
            "_status": "precondition_failed",
            "_audit_result": "failed_pre_write",
            "_audit": {"project_id": project_id},
            "error": "The project changed after preview; no write was sent.",
        }

    mark_write_dispatch_started()
    client.update_project(project_id, patch)
    mark_write_verifying()
    after = _project_occurrence(client.get_project(project_id))
    expected = {
        **before,
        "name": patch["name"],
        "budget": patch.get("budget", before["budget"]),
    }
    mismatches = {
        key: {"expected": value, "actual": after[key]}
        for key, value in expected.items()
        if after[key] != value
    }
    fully_verified = not mismatches
    return {
        "_status": "updated" if fully_verified else "verification_failed",
        "_audit_result": "success" if fully_verified else "verification_failed",
        "_audit": {
            "project_id": project_id,
            "name": after["name"],
            "fully_verified": fully_verified,
        },
        "project": after,
        "verification": {
            "independent_post_read": True,
            "field_mismatches": mismatches,
            "fully_verified": fully_verified,
        },
    }


# Not registered as an MCP tool: every approved action executes through the single
# annotated execute_approved_action entry point.
def update_project_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Execute one explicitly approved project rename or budget change."""
    return run_approved_write(
        ctx.get_client(), approval_id, "update_project", _execute_update_project
    )


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_delete_project(
    project_id: Annotated[
        str, Field(description="Exact Moneybird project id; or give project_name.")
    ] = "",
    project_name: Annotated[
        str, Field(description="Exact current project name; or give project_id.")
    ] = "",
) -> dict[str, Any]:
    """Use this before deleting a Moneybird project. Do not execute the write until the user explicitly confirms.

    Moneybird deletes a project nothing is linked to and archives one that still
    has linked records, such as invoice lines or time entries. Neither can be
    undone through the API. Only an active project is accepted."""
    client = ctx.get_client()
    before = _project_occurrence(_resolve_project(client, project_id, project_name))
    if before["state"] != "active":
        raise MoneybirdError(
            f"Project '{before['name']}' ({before['id']}) is "
            f"{before['state'] or 'not active'}. Only an active project can be "
            "deleted here: for an archived one, the result could not be verified."
        )
    payload = {"project_occurrence": before}
    return stage_write(
        "delete_project",
        summary=f"Delete or archive project '{before['name']}'",
        payload=payload,
        preview={
            "project": before,
            "outcome": (
                "Moneybird deletes the project when nothing is linked to it, and "
                "archives it when invoice lines, time entries or other records "
                "still use it. Archived projects stay on those records. Neither "
                "outcome can be undone through the API."
            ),
            "planned_api_actions": [
                "GET /projects/{id} precondition",
                "DELETE /projects/{id}",
                "GET /projects/{id} independent removal/archive read-back",
            ],
        },
        fingerprint=duplicate_fingerprint("delete_project", payload),
    )


def _execute_delete_project(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    before = payload["project_occurrence"]
    project_id = before["id"]
    if _project_occurrence(client.get_project(project_id)) != before:
        return {
            "_status": "precondition_failed",
            "_audit_result": "failed_pre_write",
            "_audit": {"project_id": project_id},
            "error": "The project changed after preview; no write was sent.",
        }

    mark_write_dispatch_started()
    client.delete_project(project_id)
    mark_write_verifying()
    try:
        record = client.get_project(project_id)
    except MoneybirdHTTPError as exc:
        if exc.status_code != 404:
            raise
        outcome = "deleted"
        after = None
        fully_verified = True
    else:
        after = _project_occurrence(record)
        # Archiving must change the state and nothing else.
        fully_verified = after == {**before, "state": "archived"}
        outcome = "archived" if fully_verified else "verification_failed"
    return {
        "_status": outcome,
        "_audit_result": "success" if fully_verified else "verification_failed",
        "_audit": {
            "project_id": project_id,
            "name": before["name"],
            "outcome": outcome,
            "fully_verified": fully_verified,
        },
        "project": after,
        "verification": {
            "independent_post_read": True,
            "provider_outcome": outcome,
            "fully_verified": fully_verified,
        },
    }


# Not registered as an MCP tool: every approved action executes through the single
# annotated execute_approved_action entry point.
def delete_project_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Execute one explicitly approved project deletion or archive."""
    return run_approved_write(
        ctx.get_client(), approval_id, "delete_project", _execute_delete_project
    )
