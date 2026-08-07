"""Which Moneybird OAuth scopes this server's tools actually need.

Moneybird documents exactly six scopes (https://developer.moneybird.com/authentication):
``sales_invoices``, ``documents``, ``estimates``, ``bank``, ``time_entries`` and
``settings``. The default when a scope parameter is omitted is ``sales_invoices``
alone, which is far too narrow for this tool surface.

Two properties of Moneybird's scope model shape everything below.

**Scopes are per resource family, not per verb.** There is no read-only variant of
any scope: ``documents`` grants both reading and rewriting purchase invoices. Scopes
therefore cannot express this server's read-first posture. That is the job of
:mod:`moneybird_mcp.capabilities` (``MONEYBIRD_CAPABILITY_MODE``) and the
prepare/approve/execute flow, which are enforced locally and are a different
concept entirely. Requesting fewer scopes does not make a connection safer to
write with, and enabling writes does not require broader scopes.

**Contacts are covered incidentally.** Moneybird documents that any one of
``sales_invoices``, ``documents``, ``estimates``, ``bank`` or ``settings`` grants
access to contacts, so contacts never need a scope of their own.

:data:`CAPABILITY_SCOPES` records the rationale per tool area so the request stays
reviewable instead of being "all six because it works". Where Moneybird does not
document which scope an endpoint family sits under, the entry says so rather than
implying a verified fact.
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
    """One area of the tool surface and the scope it needs."""

    area: str
    scope: str
    reason: str
    examples: tuple[str, ...]
    # False where Moneybird does not document the endpoint family's scope and the
    # mapping is this project's inference. Kept explicit so a future live check
    # can correct it without having to guess which rows were ever verified.
    documented: bool = True


CAPABILITY_SCOPES: tuple[CapabilityScope, ...] = (
    CapabilityScope(
        area="Sales invoicing",
        scope="sales_invoices",
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
    ),
    CapabilityScope(
        area="Purchase administration",
        scope="documents",
        reason=(
            "Purchase invoices, receipts and general journal documents live under "
            "/documents, together with their attachments. The VAT settlement "
            "journal is a general journal document."
        ),
        examples=(
            "list_purchase_documents",
            "get_purchase_invoice_by_reference",
            "review_purchase_invoices",
            "prepare_reconcile_purchase_invoice",
            "prepare_create_general_journal_document",
            "prepare_vat_settlement_journal",
            "read_document_attachment",
        ),
    ),
    CapabilityScope(
        area="Estimates",
        scope="estimates",
        reason="Quotations. Only list_estimates reads them today.",
        examples=("list_estimates",),
    ),
    CapabilityScope(
        area="Banking",
        scope="bank",
        reason=(
            "Financial accounts, financial mutations and the bookings linking a "
            "mutation to an invoice, document or ledger account."
        ),
        examples=(
            "list_financial_accounts",
            "list_financial_mutations",
            "suggest_bank_mutation_matches",
            "prepare_link_bank_mutation_booking",
            "prepare_reclassify_bank_mutation_bookings",
        ),
    ),
    CapabilityScope(
        area="Time registration",
        scope="time_entries",
        reason=(
            "Time entries have their own scope; a token without it gets 401 on "
            "that endpoint even when every other scope is granted."
        ),
        examples=("list_time_entries",),
    ),
    CapabilityScope(
        area="Reference data",
        scope="settings",
        reason=(
            "Ledger accounts, tax rates, workflows, document styles and custom "
            "fields. Almost every write preview resolves a ledger account or tax "
            "rate first, so this is not optional for bookkeeping use."
        ),
        examples=(
            "list_ledger_accounts",
            "list_tax_rates",
            "prepare_create_ledger_account",
            "get_invoice_defaults_for_contact",
        ),
    ),
    CapabilityScope(
        area="Products and projects",
        scope="settings",
        reason=(
            "Grouped with reference data. Moneybird does not document which scope "
            "covers /products and /projects; settings is the inference that "
            "matches their place in the Moneybird UI."
        ),
        examples=("list_products", "audit_products", "list_projects"),
        documented=False,
    ),
    CapabilityScope(
        area="Financial reports",
        scope="settings",
        reason=(
            "Moneybird does not document a scope for /reports. Requesting "
            "settings alongside the resource scopes has covered every report this "
            "server reads."
        ),
        examples=("get_financial_report", "analyze_vat_settlement"),
        documented=False,
    ),
)


# Named scope sets. `full` is what an ordinary local login requests: this server
# is a general bookkeeping assistant and a user who cannot see their bank feed or
# purchase invoices has a broken installation rather than a safer one. The
# narrower sets exist so a future product tier, or a user who genuinely only
# invoices, can consent to less without editing code.
SCOPE_PROFILES: dict[str, tuple[str, ...]] = {
    "full": KNOWN_SCOPES,
    # Everything except quotations and time registration, which many
    # administrations never use.
    "bookkeeping": ("sales_invoices", "documents", "bank", "settings"),
    # Draft, send and follow up sales invoices; no purchase or bank access.
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
