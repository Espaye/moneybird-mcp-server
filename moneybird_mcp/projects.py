"""Resolve the Moneybird project a document line is booked on.

Moneybird projects work as cost centres: a line carries an optional
``project_id`` next to its ledger account. Reading projects needs the
``settings`` scope, so callers fetch them only when a caller names a project.
"""
from __future__ import annotations

from typing import Any

from .config import MoneybirdError


def fetch_all_projects(client: Any) -> list[dict[str, Any]]:
    """Every project in the administration, archived ones included.

    The list endpoint returns only active projects unless the filter names
    another state. Fetching all of them lets an archived project be refused by
    name instead of reported as unknown.
    """
    projects: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = client.list_projects(limit=100, page=page, state="all")
        projects.extend(batch)
        if len(batch) < 100:
            return projects
        page += 1


def resolve_project_reference(
    projects: list[dict[str, Any]],
    *,
    project_id: str = "",
    project_name: str = "",
) -> dict[str, Any]:
    """Resolve one active project by id or exact name."""
    if project_id:
        match = next(
            (item for item in projects if str(item.get("id")) == str(project_id)),
            None,
        )
        if not match:
            raise MoneybirdError(f"Unknown project_id {project_id}.")
    elif project_name:
        matches = [item for item in projects if str(item.get("name") or "") == project_name]
        if len(matches) != 1:
            raise MoneybirdError(
                f"Expected exactly one project named '{project_name}', got {len(matches)}."
            )
        match = matches[0]
    else:
        raise MoneybirdError("Provide project_id or project_name.")
    state = str(match.get("state") or "")
    if state != "active":
        raise MoneybirdError(
            f"Project '{match.get('name')}' ({match.get('id')}) is {state or 'not active'}; "
            "only an active project can be set on a line."
        )
    return match
