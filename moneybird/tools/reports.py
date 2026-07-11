"""Moneybird report tools (profit & loss, balance sheet, ledgers, tax, debtors, ...)."""
from __future__ import annotations

from typing import Any

from ..config import (
    MoneybirdError,
    READ_ONLY_ANNOTATIONS,
    REPORT_ENDPOINTS,
)
from ..formatting import (
    report_title,
)
from ._registry import mcp
from . import _context as ctx


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_profit_loss(period: str) -> dict[str, Any]:
    """Use this when you need the Moneybird profit and loss report for a specific period."""
    client = ctx.get_client()
    report = client.get_report("profit_loss", period=period)
    return {
        "title": report_title("profit_loss", period),
        "period": period,
        "report": report,
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_balance_sheet(period: str) -> dict[str, Any]:
    """Use this when you need the Moneybird balance sheet report for a specific period."""
    client = ctx.get_client()
    report = client.get_report("balance_sheet", period=period)
    return {
        "title": report_title("balance_sheet", period),
        "period": period,
        "report": report,
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_general_ledger(period: str) -> dict[str, Any]:
    """Use this when you need the Moneybird general ledger report for a specific period."""
    client = ctx.get_client()
    report = client.get_report("general_ledger", period=period)
    return {
        "title": report_title("general_ledger", period),
        "period": period,
        "report": report,
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_financial_report(report_name: str, period: str, page: int = 0) -> dict[str, Any]:
    """Use this for any Moneybird report: profit_loss, balance_sheet, general_ledger, cash_flow,
    tax (btw), debtors (openstaande verkoopfacturen), creditors (openstaande inkoopfacturen),
    debtors_aging / creditors_aging, revenue_by_contact, revenue_by_project,
    expenses_by_contact, expenses_by_project, journal_entries, subscriptions, or assets.
    period accepts e.g. this_year, prev_month, 202601..202603 — BUT cash_flow, tax, debtors,
    and creditors accept at most one month (use this_month or 202606), and the aging reports
    take a whole month as reference (202606). Set page only for the paginated
    per-contact/per-project, debtor/creditor, and journal_entries reports."""
    name = str(report_name).strip()
    if name not in REPORT_ENDPOINTS:
        supported = ", ".join(sorted(REPORT_ENDPOINTS))
        raise MoneybirdError(f"Unsupported report '{report_name}'. Use one of: {supported}.")
    client = ctx.get_client()
    report = client.get_report(name, period=period, page=page if page > 0 else None)
    return {
        "title": report_title(name, period),
        "report_name": name,
        "period": period,
        "report": report,
    }


