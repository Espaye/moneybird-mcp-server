"""Reference data: products, tax rates, ledger accounts, financial accounts, projects, time entries."""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ..config import (
    READ_ONLY_ANNOTATIONS,
)
from ..formatting import (
    compact_financial_account_summary,
    compact_ledger_account_summary,
)
from . import _context as ctx
from ._params import FilterString, Limit, Page, Period
from ._registry import mcp


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_products(limit: Limit = 25, page: Page = 1) -> dict[str, Any]:
    """List producten with their defaults (tax_rate_id, ledger_account_id, prijs)."""
    client = ctx.get_client()
    products = client.list_products(limit=limit, page=page)
    return {
        "products": [
            {
                "id": str(item.get("id")),
                "description": item.get("description"),
                "identifier": item.get("identifier"),
                "price": item.get("price"),
                "currency": item.get("currency"),
                "tax_rate_id": item.get("tax_rate_id"),
                "ledger_account_id": item.get("ledger_account_id"),
            }
            for item in products
        ],
        "page": page,
        "count": len(products),
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_tax_rates() -> dict[str, Any]:
    """List btw-tarieven (tax rates) with the valid tax_rate_id values for invoice lines."""
    client = ctx.get_client()
    tax_rates = client.list_tax_rates()
    return {
        "tax_rates": [
            {
                "id": str(item.get("id")),
                "name": item.get("name"),
                "percentage": item.get("percentage"),
                "tax_rate_type": item.get("tax_rate_type"),
                "active": item.get("active"),
            }
            for item in tax_rates
        ]
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_ledger_accounts() -> dict[str, Any]:
    """List ledger ids and RGS taxonomy codes, including codes usable for new accounts."""
    client = ctx.get_client()
    ledger_accounts = client.list_ledger_accounts()
    return {
        "ledger_accounts": [compact_ledger_account_summary(item) for item in ledger_accounts]
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_financial_accounts(limit: Limit = 25, page: Page = 1) -> dict[str, Any]:
    """List bankrekeningen, kasrekeningen and intermediary accounts (financiële rekeningen).

    Moneybird returns the complete collection without server-side pagination; this tool
    applies limit/page locally so later pages never repeat page 1.
    """
    client = ctx.get_client()
    all_financial_accounts = client.list_all_financial_accounts()
    start = (page - 1) * limit
    financial_accounts = all_financial_accounts[start : start + limit]
    return {
        "financial_accounts": [
            compact_financial_account_summary(item) for item in financial_accounts
        ],
        "page": page,
        "count": len(financial_accounts),
        "total_count": len(all_financial_accounts),
        "has_more": start + len(financial_accounts) < len(all_financial_accounts),
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_projects(
    limit: Limit = 25,
    page: Page = 1,
    state: Annotated[str, Field(description="Project state filter: 'active', 'archived', or 'all'. Empty = Moneybird default (active).")] = "",
) -> dict[str, Any]:
    """Use this to list Moneybird projects. Optional state filter: active, archived, or all."""
    client = ctx.get_client()
    projects = client.list_projects(limit=limit, page=page, state=state)
    return {
        "projects": [
            {
                "id": str(item.get("id")),
                "name": item.get("name"),
                "state": item.get("state"),
                "budget": item.get("budget"),
            }
            for item in projects
        ],
        "page": page,
        "count": len(projects),
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_time_entries(
    limit: Limit = 25,
    page: Page = 1,
    filter: FilterString = "",
    period: Period = "",
) -> dict[str, Any]:
    """Use this to list Moneybird time entries (logged hours). Dutch: geschreven uren bekijken; for uren registreren, note that this tool is read-only.

    Optional `filter` accepts Moneybird query syntax (e.g. 'contact_id:123',
    'project_id:456', 'state:open', 'user_id:789'); combine with commas.
    Optional `period` accepts e.g. '202506' or '20250101..20250331'.
    """
    client = ctx.get_client()
    entries = client.list_time_entries(limit=limit, page=page, filter=filter, period=period)

    def _party_name(obj: Any) -> str | None:
        if not isinstance(obj, dict):
            return None
        full = " ".join(p for p in (obj.get("firstname"), obj.get("lastname")) if p)
        return obj.get("name") or (full or None) or obj.get("company_name")

    return {
        "time_entries": [
            {
                "id": str(item.get("id")),
                "started_at": item.get("started_at"),
                "ended_at": item.get("ended_at"),
                "description": item.get("description"),
                "billable": item.get("billable"),
                "paused_duration": item.get("paused_duration"),
                "contact": _party_name(item.get("contact")),
                "project": _party_name(item.get("project")),
                "user_id": str(item.get("user_id")) if item.get("user_id") is not None else None,
            }
            for item in entries
        ],
        "page": page,
        "count": len(entries),
    }

