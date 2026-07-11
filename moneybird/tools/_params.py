"""Shared annotated parameter types for the MCP tool signatures.

FastMCP turns these ``Annotated[..., Field(...)]`` aliases into JSON-schema
parameter descriptions, constraints, and enums, so every MCP client shows the
model *per-parameter* guidance instead of bare ``str``/``int``. Literal aliases
mirror the value sets in :mod:`moneybird.config`;
``tests/test_moneybird_helpers.py`` asserts they stay in sync.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

# --- Pagination ---------------------------------------------------------------

Limit = Annotated[int, Field(ge=1, le=100, description="Maximum number of records to return.")]
Page = Annotated[int, Field(ge=1, description="1-based page number.")]
ReportPage = Annotated[
    int,
    Field(
        ge=0,
        description=(
            "Page number for the paginated reports (debtors/creditors, aging, "
            "per-contact/per-project, journal_entries); 0 = no pagination."
        ),
    ),
]

# --- Filters and periods --------------------------------------------------------

Period = Annotated[
    str,
    Field(
        description=(
            "Period filter: 'this_month', 'prev_month', 'this_year', 'prev_year', "
            "a month like '202601', or a range like '20260101..20260131'. "
            "Empty string = no period filter."
        )
    ),
]

FilterString = Annotated[
    str,
    Field(
        description=(
            "Raw Moneybird filter string (comma-separated key:value pairs), "
            "e.g. 'state:open,period:this_month'. Empty string = no filter."
        )
    ),
]

# --- Identifiers ----------------------------------------------------------------

ContactId = Annotated[
    str,
    Field(description="Moneybird contact id (long numeric string), e.g. from search_contacts."),
]
CustomerId = Annotated[
    str,
    Field(description="The human-facing customer number of a contact (not the contact id)."),
]
SalesInvoiceId = Annotated[
    str,
    Field(description="Moneybird sales invoice id (long numeric string)."),
]
FinancialMutationId = Annotated[
    str,
    Field(description="Financial mutation (bank transaction) id, e.g. from list_financial_mutations."),
]
FinancialAccountId = Annotated[
    str,
    Field(description="Financial account (bank account) id, e.g. from list_financial_accounts."),
]
ApprovalId = Annotated[
    str,
    Field(
        description=(
            "The approval_id returned by the matching prepare_* tool. Call this only "
            "after showing the user the preview and receiving an explicit 'yes'."
        )
    ),
]

# --- Values ----------------------------------------------------------------------

DateString = Annotated[
    str,
    Field(description="Date in YYYY-MM-DD format."),
]
OptionalDateString = Annotated[
    str,
    Field(description="Date in YYYY-MM-DD format, or empty string to use the default."),
]
PriceString = Annotated[
    str,
    Field(description="Amount as a decimal string with a dot, e.g. '121.00'."),
]

# --- Enums (kept in sync with moneybird.config by tests) --------------------------

ReportName = Literal[
    "profit_loss",
    "balance_sheet",
    "general_ledger",
    "cash_flow",
    "tax",
    "debtors",
    "debtors_aging",
    "creditors",
    "creditors_aging",
    "revenue_by_contact",
    "revenue_by_project",
    "expenses_by_contact",
    "expenses_by_project",
    "journal_entries",
    "subscriptions",
    "assets",
]

LinkBookingType = Literal[
    "SalesInvoice",
    "Document",
    "LedgerAccount",
    "PaymentTransactionBatch",
    "PurchaseTransaction",
    "NewPurchaseInvoice",
    "NewReceipt",
    "PaymentTransaction",
    "PurchaseTransactionBatch",
    "ExternalSalesInvoice",
    "Payment",
    "VatDocument",
]

UnlinkBookingType = Literal["Payment", "LedgerAccountBooking"]

PayableDocumentType = Literal["sales_invoice", "purchase_invoice", "receipt"]
