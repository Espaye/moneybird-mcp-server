"""Which Moneybird OAuth scopes this server's tools actually need.

Moneybird documents exactly six scopes: ``sales_invoices``, ``documents``,
``estimates``, ``bank``, ``time_entries`` and ``settings``. The default when the
scope parameter is omitted is ``sales_invoices`` alone, which is far too narrow
for this tool surface.

**The per-endpoint reference is the source of truth, not the Authentication
page.** Each operation on developer.moneybird.com carries its own
``Required scope(s)`` line, and the grouping is not the intuitive one:

- ``/products``, ``/projects`` and ``/financial_accounts`` require ``settings``
  — financial *accounts* are settings, while financial *mutations* are ``bank``.
- Reports do **not** share one scope. ``balance_sheet``, ``cash_flow`` and
  ``general_ledger`` require ``bank``; ``profit_loss``, ``tax`` and
  ``journal_entries`` require ``documents`` *and* ``sales_invoices`` together;
  the debtor/revenue reports require ``sales_invoices`` and the
  creditor/expense/asset reports require ``documents``. No report requires
  ``settings``.
- Contacts, ledger-account reads and tax-rate reads are satisfied by **any one**
  of several scopes, so they never need a scope of their own.

:data:`CAPABILITY_SCOPES` records that per tool area, and
``tests/test_oauth_scopes.py`` checks every claim here against the vendored
``docs/moneybird_api_scopes.json`` snapshot, so a wrong entry fails CI instead of
becoming a 401 in the middle of someone's task.

Two properties of the scope model shape the rest of this module.

**Scopes are per resource family, not per verb.** There is no read-only variant
of any scope: ``documents`` grants both reading and rewriting purchase invoices.
Scopes therefore cannot express this server's read-first posture. That is the job
of :mod:`moneybird_mcp.capabilities` (``MONEYBIRD_CAPABILITY_MODE``) and the
prepare/approve/execute flow, which are enforced locally and are a different
concept entirely. Requesting fewer scopes does not make a connection safer to
write with, and enabling writes does not require broader scopes.

**All six are genuinely required by the currently exposed tools**, each by at
least one endpoint that accepts no substitute — which is why the default profile
asks for all six rather than out of convenience. ``tests/test_oauth_scopes.py``
proves that minimality from the snapshot too, so dropping a tool that turns out
to be a scope's only justification will surface here.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import MoneybirdError

# The complete documented scope vocabulary. An unknown scope name is rejected
# rather than forwarded: Moneybird answers a typo with a generic authorization
# failure much later, at the point where the user has already left the terminal.
KNOWN_SCOPES: tuple[str, ...] = (
    "sales_invoices",
    "documents",
    "estimates",
    "bank",
    "time_entries",
    "settings",
)


@dataclass(frozen=True)
class CapabilityScope:
    """One area of the tool surface and the scope(s) its endpoints require.

    ``scopes`` lists what the area needs *together*; an area whose endpoints each
    need a single scope is split into separate entries rather than merged, so the
    reason attached to a scope stays true for every endpoint it covers.
    """

    area: str
    scopes: tuple[str, ...]
    reason: str
    examples: tuple[str, ...]
    # Endpoints backing this area, normalised as "METHOD /path" exactly as in
    # docs/moneybird_api_scopes.json. The test suite joins on these, so a claim
    # here cannot drift from Moneybird's published requirement.
    endpoints: tuple[str, ...]


CAPABILITY_SCOPES: tuple[CapabilityScope, ...] = (
    CapabilityScope(
        area="Sales invoicing",
        scopes=("sales_invoices",),
        reason=(
            "Sales invoices, recurring sales invoices, credit invoices, invoice "
            "sending and payments registered against a sales invoice."
        ),
        examples=(
            "list_sales_invoices",
            "prepare_create_sales_invoice_draft",
            "prepare_meter_usage_sales_invoices",
            "prepare_create_credit_invoice",
            "prepare_send_sales_invoice",
            "list_recurring_sales_invoices",
        ),
        endpoints=(
            "GET /sales_invoices",
            "POST /sales_invoices",
            "PATCH /sales_invoices/*/send_invoice",
            "PATCH /sales_invoices/*/register_payment",
            "GET /recurring_sales_invoices",
        ),
    ),
    CapabilityScope(
        area="Purchase administration",
        scopes=("documents",),
        reason=(
            "Purchase invoices, receipts and general journal documents live under "
            "/documents, together with their attachments. The VAT settlement "
            "journal is a general journal document."
        ),
        examples=(
            "list_purchase_documents",
            "review_purchase_invoices",
            "prepare_reconcile_purchase_invoice",
            "prepare_settle_purchase_invoice_from_bank_mutations",
            "prepare_create_general_journal_document",
            "prepare_vat_settlement_journal",
            "read_document_attachment",
        ),
        endpoints=(
            "GET /documents/purchase_invoices",
            "PATCH /documents/purchase_invoices/*",
            "GET /documents/receipts",
            "POST /documents/general_journal_documents",
            "GET /documents/purchase_invoices/*/attachments/*/download",
        ),
    ),
    CapabilityScope(
        area="Estimates",
        scopes=("estimates",),
        reason="Quotations. Only list_estimates reads them today.",
        examples=("list_estimates",),
        endpoints=("GET /estimates", "GET /estimates/*"),
    ),
    CapabilityScope(
        area="Bank mutations",
        scopes=("bank",),
        reason=(
            "Financial mutations and the bookings linking a mutation to an "
            "invoice, document or ledger account. Note that the financial "
            "*accounts* they belong to are settings, not bank."
        ),
        examples=(
            "list_financial_mutations",
            "suggest_bank_mutation_matches",
            "prepare_link_bank_mutation_booking",
            "prepare_settle_purchase_invoice_from_bank_mutations",
            "prepare_unlink_bank_mutation_booking",
            "prepare_reclassify_bank_mutation_bookings",
        ),
        endpoints=(
            "GET /financial_mutations",
            "PATCH /financial_mutations/*/link_booking",
            "DELETE /financial_mutations/*/unlink_booking",
        ),
    ),
    CapabilityScope(
        area="Time registration",
        scopes=("time_entries",),
        reason=(
            "Time entries have their own scope; a token without it gets 401 on "
            "that endpoint even when every other scope is granted."
        ),
        examples=("list_time_entries",),
        endpoints=("GET /time_entries",),
    ),
    CapabilityScope(
        area="Settings and reference data",
        scopes=("settings",),
        reason=(
            "Financial accounts, products, projects, and creating a ledger "
            "account. Reading ledger accounts and tax rates is satisfied by any "
            "of several scopes, but these four accept no substitute."
        ),
        examples=(
            "list_financial_accounts",
            "list_products",
            "audit_products",
            "prepare_bulk_update_product_prices",
            "list_projects",
            "prepare_create_ledger_account",
            "prepare_update_ledger_account",
            "prepare_delete_empty_ledger_account",
        ),
        endpoints=(
            "GET /financial_accounts",
            "GET /products",
            "PATCH /products/*",
            "GET /projects",
            "POST /ledger_accounts",
            "PATCH /ledger_accounts/*",
            "DELETE /ledger_accounts/*",
        ),
    ),
    # Reports are listed separately per scope group because Moneybird assigns
    # them individually. Presenting them as one row would have to name a single
    # scope, and every available choice would be wrong for most of the set.
    CapabilityScope(
        area="Reports: balance sheet, cash flow, general ledger",
        scopes=("bank",),
        reason=(
            "Moneybird files these three under bank, not settings. An easy "
            "assumption to get wrong, because they read like reference data."
        ),
        examples=("get_financial_report",),
        endpoints=(
            "GET /reports/balance_sheet",
            "GET /reports/cash_flow",
            "GET /reports/general_ledger",
        ),
    ),
    CapabilityScope(
        area="Reports: profit and loss, tax, journal entries",
        scopes=("documents", "sales_invoices"),
        reason=(
            "These need both scopes together, since they span incoming and "
            "outgoing sides. The tax report backs analyze_vat_settlement."
        ),
        examples=("get_financial_report", "analyze_vat_settlement"),
        endpoints=(
            "GET /reports/profit_loss",
            "GET /reports/tax",
            "GET /reports/journal_entries",
        ),
    ),
    CapabilityScope(
        area="Reports: debtors, revenue, subscriptions",
        scopes=("sales_invoices",),
        reason="The receivable side, scoped like the sales invoices behind it.",
        examples=("get_financial_report",),
        endpoints=(
            "GET /reports/debtors",
            "GET /reports/debtors_aging",
            "GET /reports/revenue_by_contact",
            "GET /reports/revenue_by_project",
            "GET /reports/subscriptions",
        ),
    ),
    CapabilityScope(
        area="Reports: creditors, expenses, assets",
        scopes=("documents",),
        reason="The payable side, scoped like the purchase documents behind it.",
        examples=("get_financial_report",),
        endpoints=(
            "GET /reports/creditors",
            "GET /reports/creditors_aging",
            "GET /reports/expenses_by_contact",
            "GET /reports/expenses_by_project",
            "GET /reports/assets",
        ),
    ),
)


# Areas reachable with any one of several scopes, so they never drive the
# request. Kept explicit because "why is there no contacts scope?" is a fair
# question to ask of a bookkeeping integration, and the answer is Moneybird's.
INCIDENTAL_ACCESS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "Contacts",
        ("estimates", "sales_invoices", "documents", "bank", "settings"),
        ("GET /contacts", "POST /contacts", "PATCH /contacts/*"),
    ),
    (
        "Ledger account and tax rate reads",
        ("settings", "sales_invoices", "documents", "estimates"),
        ("GET /ledger_accounts", "GET /tax_rates"),
    ),
    ("Administration listing", (), ("GET /administrations",)),
)


# Named scope sets. `full` is what an ordinary login requests: every one of the
# six is required by at least one currently exposed tool, so a narrower default
# would just be a broken installation. The narrower sets exist so a future
# product tier, or a user who genuinely only invoices, can consent to less.
SCOPE_PROFILES: dict[str, tuple[str, ...]] = {
    "full": KNOWN_SCOPES,
    # Everything except quotations and time registration, which many
    # administrations never use. Keeps every report except none — all report
    # groups are covered by sales_invoices, documents and bank.
    "bookkeeping": ("sales_invoices", "documents", "bank", "settings"),
    # Draft, send and follow up sales invoices. No purchase, bank, estimate or
    # time access; of the reports, only the debtor/revenue/subscription group.
    "invoicing": ("sales_invoices", "settings"),
}

DEFAULT_SCOPE_PROFILE = "full"

# Explicit override, read only by the login command. Accepts either a profile
# name from SCOPE_PROFILES or a space/comma-separated list of documented scopes.
SCOPES_ENV = "MONEYBIRD_OAUTH_SCOPES"


def scopes_for_profile(profile: str = DEFAULT_SCOPE_PROFILE) -> tuple[str, ...]:
    try:
        return SCOPE_PROFILES[profile]
    except KeyError:
        raise MoneybirdError(
            f"Unknown OAuth scope profile {profile!r}. "
            f"Choose one of: {', '.join(sorted(SCOPE_PROFILES))}."
        ) from None


def parse_scopes(value: str) -> tuple[str, ...]:
    """Parse a profile name or an explicit scope list into validated scopes.

    Order follows :data:`KNOWN_SCOPES` and duplicates are dropped, so the same
    request always produces the same ``scope`` parameter.
    """
    text = (value or "").strip()
    if not text:
        return scopes_for_profile()
    if text in SCOPE_PROFILES:
        return SCOPE_PROFILES[text]

    requested = {item for item in text.replace(",", " ").split() if item}
    unknown = sorted(requested - set(KNOWN_SCOPES))
    if unknown:
        raise MoneybirdError(
            f"Unknown Moneybird OAuth scope(s): {', '.join(unknown)}. "
            f"Moneybird documents only: {', '.join(KNOWN_SCOPES)}. "
            f"A profile name is also accepted: {', '.join(sorted(SCOPE_PROFILES))}."
        )
    if not requested:
        raise MoneybirdError(f"{SCOPES_ENV} is set but lists no scopes.")
    return tuple(scope for scope in KNOWN_SCOPES if scope in requested)


def format_scopes(scopes: tuple[str, ...]) -> str:
    """The space-separated ``scope`` parameter Moneybird expects."""
    return " ".join(scopes)


def missing_scopes(granted: str, required: tuple[str, ...]) -> tuple[str, ...]:
    """Requested scopes absent from what Moneybird actually granted.

    Moneybird echoes the granted scopes on the token response. A shortfall is
    worth reporting at login: the affected tools fail with a bare 401 much later,
    in the middle of a task, where the cause is not obvious.
    """
    have = {item for item in (granted or "").replace(",", " ").split() if item}
    return tuple(scope for scope in required if scope not in have)


def unavailable_areas(scopes: tuple[str, ...]) -> tuple[str, ...]:
    """Tool areas a connection with ``scopes`` cannot reach.

    Used by ``auth scopes`` to describe a narrowed profile in terms of what stops
    working, rather than leaving the user to infer it from six scope names.
    """
    granted = set(scopes)
    return tuple(
        entry.area
        for entry in CAPABILITY_SCOPES
        if not set(entry.scopes) <= granted
    )
