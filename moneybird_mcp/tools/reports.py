"""Moneybird report tools (profit & loss, balance sheet, ledgers, tax, debtors, ...)."""
from __future__ import annotations

from typing import Any

from ..config import (
    READ_ONLY_ANNOTATIONS,
    REPORT_ENDPOINTS,
    MoneybirdError,
)
from ..formatting import (
    report_title,
)
from . import _context as ctx
from ._params import Period, ReportName, ReportPage
from ._registry import mcp


def _enrich_ledger_account_rows(
    value: Any,
    accounts_by_id: dict[str, dict[str, Any]],
) -> Any:
    """Recursively join Moneybird report rows with their ledger-account labels."""

    if isinstance(value, list):
        return [
            _enrich_ledger_account_rows(item, accounts_by_id) for item in value
        ]
    if not isinstance(value, dict):
        return value
    enriched = {
        key: _enrich_ledger_account_rows(item, accounts_by_id)
        for key, item in value.items()
    }
    ledger_account_id = str(value.get("ledger_account_id") or "")
    account = accounts_by_id.get(ledger_account_id)
    if account:
        enriched.setdefault("ledger_account_name", account.get("name"))
        enriched.setdefault("ledger_account_number", account.get("account_id"))
        enriched.setdefault("ledger_account_type", account.get("account_type"))
    return enriched


def _report_with_ledger_labels(client: Any, report: dict[str, Any]) -> dict[str, Any]:
    accounts_by_id = {
        str(account.get("id") or ""): account
        for account in client.list_ledger_accounts()
    }
    return _enrich_ledger_account_rows(report, accounts_by_id)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_financial_report(
    report_name: ReportName,
    period: Period = "this_month",
    page: ReportPage = 0,
) -> dict[str, Any]:
    """Any Moneybird report: profit_loss (winst-en-verliesrekening), balance_sheet (balans),
    general_ledger (grootboek), cash_flow, tax (btw), debtors (openstaande verkoopfacturen),
    creditors (openstaande inkoopfacturen), debtors_aging / creditors_aging,
    revenue_by_contact, revenue_by_project, expenses_by_contact, expenses_by_project,
    journal_entries (memoriaalboekingen), subscriptions, or assets.

    period accepts e.g. this_year, prev_month, 202601..202603 — BUT cash_flow, tax, debtors,
    and creditors accept at most one month (use this_month or 202606); asking those for a
    longer period is refused with the exact per-month calls to make instead. The aging
    reports take a whole month as reference (202606). Set page only for the paginated
    per-contact/per-project, debtor/creditor, and journal_entries reports.

    Reports are throttled separately by Moneybird at 50 requests per 5 minutes, three times
    tighter than the rest of the API, so a per-month sweep over a year is a quarter of that
    budget."""
    name = str(report_name).strip()
    if name not in REPORT_ENDPOINTS:
        supported = ", ".join(sorted(REPORT_ENDPOINTS))
        raise MoneybirdError(f"Unsupported report '{report_name}'. Use one of: {supported}.")
    client = ctx.get_client()
    report = client.get_report(name, period=period, page=page if page > 0 else None)
    if name in {"profit_loss", "balance_sheet"}:
        report = _report_with_ledger_labels(client, report)
    return {
        "title": report_title(name, period),
        "report_name": name,
        "period": period,
        "report": report,
    }


