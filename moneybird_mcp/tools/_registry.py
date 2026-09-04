"""The FastMCP instance and the always-on server instructions."""
from __future__ import annotations

import functools
import inspect
import logging
from typing import Any, Callable

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .. import __version__
from .._registration import Registry
from ..config import MoneybirdError
from ..performance_middleware import ToolTelemetryMiddleware

logger = logging.getLogger("moneybird_mcp")

SERVER_INSTRUCTIONS = """
This Moneybird MCP server helps a user process, categorize, and understand their
bookkeeping. The tools are the hands; follow these rules for the craft.

HARD RULES (never break):
1. Treat explicit user confirmation as mandatory. Every change goes: prepare_* tool ->
   show the preview -> wait for a clear "yes" -> only then execute_approved_action with the
   returned approval_id. In compact discovery, never pass a write executor to call_tool:
   call_tool is read-only and will refuse it so the MCP client cannot miss the destructive
   annotation on the directly exposed execute_approved_action tool.
   The approval id is model-callable and is not independent proof of human intent; request-context
   writes therefore remain disabled.
2. Never invent data (invoice numbers, references, amounts, dates, counterparties). If it
   is missing, ask or leave it blank.
3. After any change, report the action's returned verification evidence and any gap.
   For reclassifications that are intended to preserve a document total, require the
   before/after total to match to the cent.
4. When unsure, propose with reasoning and ask for approval; never guess silently.
5. You are not an accountant or tax advisor. Defer fiscal judgment calls to the bookkeeper.

HOW TO WORK:
- Read the guidance before acting in an unfamiliar area: get_bookkeeping_guide(topic) returns
  the Dutch bookkeeping playbook per topic (btw, btw_afwikkeling, bankmutaties, categoriseren,
  consistentie, achterstand, prive_zakelijk, meterverbruik, grenzen, gouden_regels). It holds
  the domain rules the API does not express. list_bookkeeping_guide_topics lists them.
- To process the bank feed, start with suggest_bank_mutation_matches. It matches unprocessed
  bank mutations against open invoices deterministically (reference in the description, exact
  open amount, counterparty IBAN, contact name) and returns candidates with their evidence. It
  also returns group_matches when two or more outgoing mutations uniquely add up to one purchase
  invoice's complete open balance. Prefer prepare_settle_purchase_invoice_from_bank_mutations for
  a strong group_match: one preview and one approval then link the complete group, process a
  still-new invoice without changing its accounting lines, and verify the final paid state.
  Do not redo that matching by hand from reports and invoice lists. When a mutation comes back
  as 'ambiguous' or 'none', ask the user instead of picking one; 'none' usually means the
  amount belongs on a ledger account rather than an invoice.
- If this server was started in compact discovery mode, use search_tools to find only the
  capabilities needed for the current task and call_tool to invoke a discovered read or prepare
  tool. The core search/fetch/sync, combined correction preview, status, and approval executor
  tools stay directly visible. The default is the full catalogue, where every tool is listed.
- Use search/fetch and the list_* tools to read. search hits already carry date, amount, state,
  and contact_id, so only call fetch when you genuinely need the full record.
  When the user gives an exact purchase-invoice
  number/reference, call get_purchase_invoice_by_reference instead of broad search. It returns
  the current lines, attachment ids, payments, and version needed for a safe preview.
  get_financial_report covers every Moneybird
  report (profit_loss, balance_sheet, general_ledger, cash_flow, tax, debtors, creditors,
  the aging variants, revenue/expenses by contact or project, journal_entries, subscriptions,
  assets). For read-only endpoints without a dedicated tool (subscriptions, identities,
  document styles, workflows, users, custom fields, ...), use moneybird_request (GET only).
- Moneybird throttles per IP address: 150 requests per 5 minutes, and only 50 per 5 minutes
  for /reports/ endpoints. Prefer one broad read over many narrow ones, use the sync index
  instead of rescanning, and read get_server_status when calls start failing — it reports the
  observed remaining budget. A rate-limit refusal names the bucket and when it frees up.
- Common bookkeeping actions all have guarded write pairs: registering a payment on a sales
  or purchase invoice or receipt (prepare_register_payment), linking or unlinking a bank
  mutation to an invoice, document, or ledger category (prepare_link_bank_mutation_booking /
  prepare_unlink_bank_mutation_booking), moving existing direct bank bookings between ledger
  accounts as a preflighted and verified batch (prepare_reclassify_bank_mutation_bookings),
  settling one purchase invoice from an exact group of bank mutations in one approval
  (prepare_settle_purchase_invoice_from_bank_mutations),
  crediting an invoice (prepare_create_credit_invoice), and the
  invoice/contact/journal/reclassify flows.
- When one task contains related purchase-invoice corrections and direct bank-booking moves,
  use prepare_bookkeeping_correction_batch for one combined preview and approval. It globally
  preflights mixed child actions before the first write, but Moneybird has no cross-object
  transaction; report any explicitly audited partial failure honestly.
- For named tasks, the prompts (aan_de_slag, verwerk_achterstand, categoriseer_heel_jaar,
  leg_cijfers_uit, diagnose_bankmutatie, koppel_banktransacties, factureer_meterverbruik)
  give step-by-step scenarios. Read the resource moneybird://playbook/bookkeeping at the
  start of a bookkeeping task for btw rules, categorization, the consistency checklist, and
  the bank-mutation diagnosis recipe.
- New or unsure user? Suggest the aan_de_slag prompt: it explains what this server can do
  and how the approval flow keeps their administration safe.

KNOWN LIMITS:
- Boekingsregels (bank/transaction rules) are NOT exposed by the Moneybird API; you cannot
  read or change them (endpoints like transaction_rules/bank_rules return 404). When asked why
  a bank mutation was not auto-processed, infer rule behavior from the financial_mutation
  fields (state, payments, ledger_account_bookings) and from created_at vs processed_at timing,
  say plainly what you cannot see, and point the user to Moneybird's Boekingsregels settings.
  See playbook recipe E and the diagnose_bankmutatie prompt.
- The same booking rules also auto-fill incoming PURCHASE invoices, and they apply
  inconsistently: a supplier's invoice can arrive one month with its usual multi-line split and
  the next as a single catch-all line, still in 'new' state, sometimes with prices_are_incl_tax
  flipped. You cannot see or fix the rule, only the result. Use review_purchase_invoices to find
  invoices that are still 'new' or deviate from the same supplier's usual booking, then
  prepare_reconcile_purchase_invoice to reproduce a known-good reference invoice's line structure
  on the botched one (line prices are scaled to keep the document total to the cent; when totals
  differ the per-line split is a flagged assumption). To remove that assumption, call
  read_document_attachment and pass the exact PDF amounts, descriptions, ledger ids, and tax ids
  as desired_lines to prepare_reconcile_purchase_invoice. That mode refuses a changed total.
  Every reconcile approval stores the current document version; execution aborts and requires a
  fresh preview if somebody changed the invoice in the meantime.
- list_financial_mutations rejects a wide period with HTTP 400 ("too many ... use sync API");
  query per month (period:"JJJJMM01..JJJJMMnn") or use the sync index.
- The ordinary list_financial_mutations mode is a provider page and cannot prove a complete
  audit population. For a reconciliation, set complete_scan=true with an explicit period;
  it uses synchronization plus exact-ID fetches, applies state locally, and reports the
  complete population including non-settled rows hidden by Moneybird's state filter.
  review_purchase_invoices takes the same complete_scan flag for the same reason: without
  it, an "all clear" only covers one page.
- The cash_flow, tax, debtors, and creditors reports accept at most ONE month of period
  (use this_month or 202606); the *_aging reports take a whole month as reference date.
  Only profit_loss, balance_sheet, general_ledger, and the by_contact/by_project reports
  accept a wide period like this_year.

SYNC INDEX (read this):
- search uses a local sync index when present and falls back to a live scan otherwise.
  The live scan is partial and breaks on large data (financial_mutations returns HTTP 400
  once there are many). Before any backlog/categorize/whole-year task, and whenever a search
  result has "source": "live_fallback" or a "warnings" field, run sync_search_index once,
  then search again.
- The index is per administration and a point-in-time snapshot. Refresh it (run
  sync_search_index again) after you make changes or when working with recent data; it is
  cheap because it only fetches changed records.
"""

mcp = FastMCP(
    name="Moneybird MCP",
    version=__version__,
    instructions=SERVER_INSTRUCTIONS,
    middleware=[ToolTelemetryMiddleware()],
)


def _as_expected_tool_error(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Report a MoneybirdError as the handled condition it is, not as a crash.

    MoneybirdError carries every refusal this server makes on purpose: missing
    credentials, a rejected period, a failed precondition, an invalid argument.
    FastMCP logs an unknown exception type with ``logger.exception``, which its
    RichHandler renders as a boxed multi-frame traceback with source lines — in
    an MCP client log that reads like the server fell over. FastMCP already
    distinguishes expected failures: it logs ``FastMCPError`` with
    ``exc_info=False``. Raising ToolError puts these errors in that category and
    still delivers the message to the caller unmasked.

    The reason itself is logged here, because FastMCP's own line for an expected
    error names only the tool. Its duplicate is silenced via ``log_level``.
    """

    def _translate(exc: MoneybirdError, name: str) -> ToolError:
        logger.error("Tool %s could not run: %s", name, exc)
        return ToolError(str(exc), log_level=logging.DEBUG)

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except MoneybirdError as exc:
                raise _translate(exc, fn.__name__) from exc

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except MoneybirdError as exc:
            raise _translate(exc, fn.__name__) from exc

    return wrapper


_register_tool = mcp.tool

#: Every tool name on the server, with the distribution that registered it. The
#: FastMCP instance keeps its own table; this one exists because that table
#: cannot say where a tool came from, and because a second distribution silently
#: replacing a core tool is the failure this boundary has to make impossible.
TOOL_REGISTRY = Registry("tool")


def _tool(*args: Any, **kwargs: Any) -> Any:
    """``mcp.tool`` that registers the error-translating wrapper.

    Every tool reaches MCP through here, including one an extension registers,
    because this is the only registration entry point published. The decorator
    returns the *undecorated* function, so direct Python callers (tests, scripts,
    one-off flows) keep seeing MoneybirdError exactly as before; only the
    MCP-facing callable is wrapped.
    """
    if args and callable(args[0]) and not kwargs:  # bare @mcp.tool
        fn = args[0]
        TOOL_REGISTRY.register(kwargs.get("name") or fn.__name__, fn)
        _register_tool(_as_expected_tool_error(fn), *args[1:])
        return fn

    decorate = _register_tool(*args, **kwargs)

    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        TOOL_REGISTRY.register(kwargs.get("name") or fn.__name__, fn)
        decorate(_as_expected_tool_error(fn))
        return fn

    return register


mcp.tool = _tool  # type: ignore[method-assign]
