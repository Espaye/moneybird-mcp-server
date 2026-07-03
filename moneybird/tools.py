"""All MCP tool definitions (the public surface exposed to ChatGPT)."""
from __future__ import annotations

import os
from typing import Any
from fastmcp import FastMCP

from .config import (
    MoneybirdError,
    PREPARE_ANNOTATIONS,
    READ_ONLY_ANNOTATIONS,
    WRITE_ANNOTATIONS,
)
from .client import get_client
from .formatting import (
    api_url,
    clean_dict,
    chunked,
    compact_document_summary,
    compact_financial_account_summary,
    compact_financial_mutation_summary,
    compact_general_journal_summary,
    compact_ledger_account_summary,
    contact_delivery_record,
    contact_title,
    document_search_record,
    document_url,
    duplicate_fingerprint,
    financial_mutation_search_record,
    financial_mutation_title,
    general_journal_search_record,
    general_journal_title,
    invoice_title,
    iso_now,
    matches_query,
    normalize_text,
    purchase_document_title,
    render_contact_delivery_table,
    render_preview_table,
    report_title,
    stringify_record,
)
from .safety import (
    append_audit_log,
    append_failed_audit_log,
    audit_log_contains_success,
    make_approval,
    pop_approval,
)
from .sync import (
    load_sync_index,
    sync_search_index_data,
)
from .invoicing import (
    apply_batch_group_merge_checks,
    build_batch_invoice_payload,
    build_invoice_delivery_audit,
    build_meter_usage_entries,
    build_merge_snapshot_from_invoice,
    build_recent_sales_invoice_send_method_audit,
    details_attributes_payload,
    evaluate_merge_compatibility,
    find_contact_matches,
    infer_contact_invoice_defaults,
    list_scheduled_merge_candidates,
    prepare_general_journal_entries,
    prepare_reclassification_batch,
    resolve_contact_reference,
    summarize_batch_preview,
)

SERVER_INSTRUCTIONS = """
This Moneybird MCP server helps a user process, categorize, and understand their
bookkeeping. The tools are the hands; follow these rules for the craft.

HARD RULES (never break):
1. Never write without explicit confirmation. Every change goes: prepare_* tool ->
   show the preview -> wait for a clear "yes" -> only then the matching *_from_approval tool.
2. Never invent data (invoice numbers, references, amounts, dates, counterparties). If it
   is missing, ask or leave it blank.
3. After any change, verify the document total is unchanged (to the cent) and say so.
4. When unsure, propose with reasoning and ask for approval; never guess silently.
5. You are not an accountant or tax advisor. Defer fiscal judgment calls to the bookkeeper.

HOW TO WORK:
- Use search/fetch and the list_* tools to read; the report tools for profit and loss,
  balance sheet, and general ledger. For read-only endpoints without a dedicated tool
  (estimates, subscriptions, projects, time entries, etc.), use moneybird_request (GET only).
- For named tasks, the prompts (verwerk_achterstand, categoriseer_heel_jaar, leg_cijfers_uit,
  diagnose_bankmutatie, factureer_meterverbruik) give step-by-step scenarios. Read the resource
  moneybird://playbook/bookkeeping at the start of a bookkeeping task for btw rules,
  categorization, the consistency checklist, and the bank-mutation diagnosis recipe.

KNOWN LIMITS:
- Boekingsregels (bank/transaction rules) are NOT exposed by the Moneybird API; you cannot
  read or change them (endpoints like transaction_rules/bank_rules return 404). When asked why
  a bank mutation was not auto-processed, infer rule behavior from the financial_mutation
  fields (state, payments, ledger_account_bookings) and from created_at vs processed_at timing,
  say plainly what you cannot see, and point the user to Moneybird's Boekingsregels settings.
  See playbook recipe E and the diagnose_bankmutatie prompt.
- list_financial_mutations rejects a wide period with HTTP 400 ("too many ... use sync API");
  query per month (period:"JJJJMM01..JJJJMMnn") or use the sync index.

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

mcp = FastMCP(name="Moneybird MCP", instructions=SERVER_INSTRUCTIONS)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_administrations() -> dict[str, Any]:
    """Use this when you need to inspect which Moneybird administrations are available to the token."""
    client = get_client(require_administration=False)
    administrations = client.list_administrations()
    configured_id = os.environ.get("MONEYBIRD_ADMINISTRATION_ID", "").strip() or None
    return {
        "administrations": [
            {
                "id": str(item.get("id")),
                "name": item.get("name"),
                "language": item.get("language"),
                "currency": item.get("currency"),
                "is_default": str(item.get("id")) == configured_id,
            }
            for item in administrations
        ]
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_contacts(limit: int = 10, page: int = 1) -> dict[str, Any]:
    """Use this when you need a compact list of Moneybird contacts without opening each record."""
    client = get_client()
    contacts = client.list_contacts(limit=limit, page=page)
    return {
        "contacts": [
            {
                "id": str(item.get("id")),
                "title": contact_title(item),
                "email": item.get("email"),
                "customer_id": item.get("customer_id"),
                "phone": item.get("phone"),
                "url": api_url("contacts", str(item.get("id")), client.administration_id),
            }
            for item in contacts
        ],
        "page": page,
        "count": len(contacts),
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def audit_invoice_delivery_settings(
    include_archived_contacts: bool = False,
    include_inactive_recurring: bool = False,
) -> dict[str, Any]:
    """Use this to verify contacts and recurring sales invoices are configured for automatic invoice e-mail delivery."""
    client = get_client()
    return build_invoice_delivery_audit(
        client,
        include_archived_contacts=include_archived_contacts,
        include_inactive_recurring=include_inactive_recurring,
    )


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_sales_invoices(
    limit: int = 10,
    page: int = 1,
    state: str = "all",
    reference: str = "",
    contact_id: str = "",
    period: str = "",
) -> dict[str, Any]:
    """Use this when you need a compact list of Moneybird sales invoices filtered by state, reference, contact, or period."""
    client = get_client()
    invoices = client.list_sales_invoices(
        limit=limit,
        page=page,
        state=state,
        reference=reference,
        contact_id=contact_id,
        period=period,
    )
    return {
        "sales_invoices": [
            {
                "id": str(item.get("id")),
                "title": invoice_title(item),
                "invoice_id": item.get("invoice_id"),
                "state": item.get("state"),
                "reference": item.get("reference"),
                "invoice_date": item.get("invoice_date"),
                "total_price_incl_tax": item.get("total_price_incl_tax"),
                "contact_id": item.get("contact_id"),
                "url": api_url("sales_invoices", str(item.get("id")), client.administration_id),
            }
            for item in invoices
        ],
        "page": page,
        "count": len(invoices),
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def audit_recent_sales_invoice_send_methods(
    limit: int = 30,
    page_scan_limit: int = 10,
) -> dict[str, Any]:
    """Use this to inspect whether recent Moneybird sales invoices were sent manually, by scheduled e-mail, or by e-invoice delivery."""
    client = get_client()
    return build_recent_sales_invoice_send_method_audit(
        client,
        limit=limit,
        page_scan_limit=page_scan_limit,
    )


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def search(query: str, limit: int = 8) -> dict[str, Any]:
    """Use this when you want ChatGPT to search Moneybird records in a connector-friendly way."""
    client = get_client()
    results: list[dict[str, Any]] = []
    index = load_sync_index(client.administration_id)
    indexed_buckets = (
        "contacts",
        "sales_invoices",
        "purchase_invoices",
        "receipts",
        "general_journal_documents",
        "financial_mutations",
    )
    use_index = (
        index.get("administration_id") == client.administration_id
        and any(index[bucket]["records"] for bucket in indexed_buckets)
    )

    if use_index:
        cached_records: list[dict[str, Any]] = []
        for bucket in indexed_buckets:
            cached_records.extend(index[bucket]["records"].values())
        for record in cached_records:
            if matches_query(record.get("search_text", ""), query):
                results.append(
                    {
                        "id": record["id"],
                        "title": record["title"],
                        "url": record["url"],
                    }
                )
        if results:
            return {
                "results": results[: max(1, min(limit, 20))],
                "source": "sync_index",
                "updated_at": index.get("updated_at"),
                "invoice_filter": index.get("invoice_filter"),
                "document_filter": index.get("document_filter"),
                "financial_mutation_filter": index.get("financial_mutation_filter"),
            }

    # Live fallback: scan each source independently so one failing endpoint cannot
    # break the whole search. Notably financial_mutations returns HTTP 400
    # ("too many ... use sync API") once an administration has many bank mutations;
    # in that case we skip it and tell the caller to build the sync index.
    scan_warnings: list[str] = []

    def _safe_scan(label: str, fetch) -> list[dict[str, Any]]:
        try:
            return fetch()
        except MoneybirdError as exc:
            scan_warnings.append(f"{label} skipped: {exc}")
            return []

    contacts = _safe_scan("contacts", lambda: client.list_contacts(limit=100, page=1))
    invoices = _safe_scan(
        "sales_invoices",
        lambda: client.list_sales_invoices(limit=100, page=1, state="all"),
    )
    purchase_invoices = _safe_scan(
        "purchase_invoices",
        lambda: client.list_documents("purchase_invoice", limit=100, page=1, period="this_year"),
    )
    receipts = _safe_scan(
        "receipts",
        lambda: client.list_documents("receipt", limit=100, page=1, period="this_year"),
    )
    journal_documents = _safe_scan(
        "general_journal_documents",
        lambda: client.list_documents("general_journal_document", limit=100, page=1, period="this_year"),
    )
    financial_mutations = _safe_scan(
        "financial_mutations",
        lambda: client.list_financial_mutations(limit=100, page=1, period="this_year"),
    )

    for contact in contacts:
        text = normalize_text(
            contact.get("company_name"),
            contact.get("firstname"),
            contact.get("lastname"),
            contact.get("email"),
            contact.get("customer_id"),
        )
        if matches_query(text, query):
            results.append(
                {
                    "id": f'contact:{contact.get("id")}',
                    "title": contact_title(contact),
                    "url": api_url("contacts", str(contact.get("id")), client.administration_id),
                }
            )

    for invoice in invoices:
        text = normalize_text(
            invoice.get("invoice_id"),
            invoice.get("reference"),
            invoice.get("state"),
            invoice.get("contact", {}).get("company_name"),
            invoice.get("contact", {}).get("firstname"),
            invoice.get("contact", {}).get("lastname"),
        )
        if matches_query(text, query):
            results.append(
                {
                    "id": f'sales_invoice:{invoice.get("id")}',
                    "title": invoice_title(invoice),
                    "url": api_url(
                        "sales_invoices",
                        str(invoice.get("id")),
                        client.administration_id,
                    ),
                }
            )

    for document in purchase_invoices:
        record = document_search_record("purchase_invoice", document, client.administration_id)
        if matches_query(record.get("search_text", ""), query):
            results.append(
                {"id": record["id"], "title": record["title"], "url": record["url"]}
            )

    for document in receipts:
        record = document_search_record("receipt", document, client.administration_id)
        if matches_query(record.get("search_text", ""), query):
            results.append(
                {"id": record["id"], "title": record["title"], "url": record["url"]}
            )

    for document in journal_documents:
        record = general_journal_search_record(document, client.administration_id)
        if matches_query(record.get("search_text", ""), query):
            results.append(
                {"id": record["id"], "title": record["title"], "url": record["url"]}
            )

    for mutation in financial_mutations:
        record = financial_mutation_search_record(mutation, client.administration_id)
        if matches_query(record.get("search_text", ""), query):
            results.append(
                {"id": record["id"], "title": record["title"], "url": record["url"]}
            )

    response: dict[str, Any] = {
        "results": results[: max(1, min(limit, 20))],
        "source": "live_fallback",
    }
    if scan_warnings:
        response["warnings"] = scan_warnings
        response["hint"] = (
            "Some live sources were skipped. Run sync_search_index to build the local "
            "cache; search then uses the sync index instead of live scans."
        )
    return response


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def fetch(id: str) -> dict[str, Any]:
    """Use this when you already know a Moneybird record id from search and need the full record."""
    client = get_client()

    if ":" not in id:
        raise MoneybirdError(
            "Expected an id like contact:123, sales_invoice:123, purchase_invoice:123, receipt:123, general_journal_document:123, financial_mutation:123, ledger_account:123, or financial_account:123."
        )

    kind, record_id = id.split(":", 1)
    kind = kind.strip()
    record_id = record_id.strip()

    if kind == "contact":
        record = client.get_contact(record_id)
        return {
            "id": id,
            "title": contact_title(record),
            "text": stringify_record(record),
            "url": api_url("contacts", record_id, client.administration_id),
            "metadata": {
                "kind": "contact",
                "moneybird_id": record_id,
                "administration_id": client.administration_id,
            },
        }

    if kind == "sales_invoice":
        record = client.get_sales_invoice(record_id)
        return {
            "id": id,
            "title": invoice_title(record),
            "text": stringify_record(record),
            "url": api_url("sales_invoices", record_id, client.administration_id),
            "metadata": {
                "kind": "sales_invoice",
                "moneybird_id": record_id,
                "administration_id": client.administration_id,
            },
        }

    if kind in {"purchase_invoice", "receipt", "general_journal_document"}:
        record = client.get_document(kind, record_id)
        title = (
            general_journal_title(record)
            if kind == "general_journal_document"
            else purchase_document_title(kind, record)
        )
        return {
            "id": id,
            "title": title,
            "text": stringify_record(record),
            "url": document_url(kind, record_id, client.administration_id),
            "metadata": {
                "kind": kind,
                "moneybird_id": record_id,
                "administration_id": client.administration_id,
            },
        }

    if kind == "financial_mutation":
        record = client.get_financial_mutation(record_id)
        return {
            "id": id,
            "title": financial_mutation_title(record),
            "text": stringify_record(record),
            "url": api_url("financial_mutations", record_id, client.administration_id),
            "metadata": {
                "kind": "financial_mutation",
                "moneybird_id": record_id,
                "administration_id": client.administration_id,
            },
        }

    if kind == "ledger_account":
        record = client.get_ledger_account(record_id)
        return {
            "id": id,
            "title": f"Ledger account {record.get('name') or record_id}",
            "text": stringify_record(record),
            "url": api_url("ledger_accounts", record_id, client.administration_id),
            "metadata": {
                "kind": "ledger_account",
                "moneybird_id": record_id,
                "administration_id": client.administration_id,
            },
        }

    if kind == "financial_account":
        record = client.get_financial_account(record_id)
        return {
            "id": id,
            "title": f"Financial account {record.get('name') or record_id}",
            "text": stringify_record(record),
            "url": api_url("financial_accounts", record_id, client.administration_id),
            "metadata": {
                "kind": "financial_account",
                "moneybird_id": record_id,
                "administration_id": client.administration_id,
            },
        }

    raise MoneybirdError(
        "Unsupported record kind. Use contact:<id>, sales_invoice:<id>, purchase_invoice:<id>, receipt:<id>, general_journal_document:<id>, financial_mutation:<id>, ledger_account:<id>, or financial_account:<id>."
    )


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_contact_by_customer_id(customer_id: str) -> dict[str, Any]:
    """Use this when you have your own external customer id and need the matching Moneybird contact."""
    client = get_client()
    record = client.get_contact_by_customer_id(customer_id)
    record_id = str(record.get("id"))
    return {
        "id": f"contact:{record_id}",
        "title": contact_title(record),
        "text": stringify_record(record),
        "url": api_url("contacts", record_id, client.administration_id),
        "metadata": {
            "kind": "contact",
            "moneybird_id": record_id,
            "customer_id": record.get("customer_id"),
            "administration_id": client.administration_id,
        },
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_products(limit: int = 25, page: int = 1) -> dict[str, Any]:
    """Use this when you need Moneybird product defaults such as tax_rate_id and ledger_account_id."""
    client = get_client()
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
    """Use this when you need valid Moneybird tax_rate_id values for invoice lines."""
    client = get_client()
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
    """Use this when you need valid Moneybird ledger_account_id values for invoice lines."""
    client = get_client()
    ledger_accounts = client.list_ledger_accounts()
    return {
        "ledger_accounts": [
            {
                "id": str(item.get("id")),
                "name": item.get("name"),
                "account_type": item.get("account_type"),
                "active": item.get("active"),
            }
            for item in ledger_accounts
        ]
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_financial_accounts(limit: int = 25, page: int = 1) -> dict[str, Any]:
    """Use this when you need the available Moneybird bank, cash, or intermediary accounts."""
    client = get_client()
    financial_accounts = client.list_financial_accounts(limit=limit, page=page)
    return {
        "financial_accounts": [
            compact_financial_account_summary(item) for item in financial_accounts
        ],
        "page": page,
        "count": len(financial_accounts),
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_projects(limit: int = 25, page: int = 1, state: str = "") -> dict[str, Any]:
    """Use this to list Moneybird projects. Optional state filter: active, archived, or all."""
    client = get_client()
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
    limit: int = 25,
    page: int = 1,
    filter: str = "",
    period: str = "",
) -> dict[str, Any]:
    """Use this to list Moneybird time entries (logged hours).

    Optional `filter` accepts Moneybird query syntax (e.g. 'contact_id:123',
    'project_id:456', 'state:open', 'user_id:789'); combine with commas.
    Optional `period` accepts e.g. '202506' or '20250101..20250331'.
    """
    client = get_client()
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


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def moneybird_request(path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read-only escape hatch for any Moneybird endpoint this server does not wrap explicitly.

    Performs a single GET within the configured administration. `path` is relative to the
    administration, e.g. 'estimates', 'subscriptions', 'time_entries/123',
    'documents/purchase_invoices', or 'projects'. Use 'administrations' to hit the API root.
    `query` is an optional dict of query-string params, e.g. {'filter': 'state:open', 'per_page': 50}.

    This can ONLY read. To change anything, use the matching prepare_* / *_from_approval tools.
    """
    cleaned = str(path).strip().lstrip("/")
    need_admin = not (cleaned == "administrations" or cleaned.startswith("administrations/"))
    client = get_client(require_administration=need_admin)
    data = client.raw_get(path, query=query)
    return {
        "path": str(path),
        "result": data,
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_purchase_invoices(
    limit: int = 10,
    page: int = 1,
    filter: str = "",
    period: str = "",
) -> dict[str, Any]:
    """Use this when you need a compact list of Moneybird purchase invoices."""
    client = get_client()
    documents = client.list_documents(
        "purchase_invoice",
        limit=limit,
        page=page,
        filter=filter,
        period=period,
    )
    return {
        "purchase_invoices": [
            compact_document_summary("purchase_invoice", item, client.administration_id)
            for item in documents
        ],
        "page": page,
        "count": len(documents),
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_receipts(
    limit: int = 10,
    page: int = 1,
    filter: str = "",
    period: str = "",
) -> dict[str, Any]:
    """Use this when you need a compact list of Moneybird receipts and cash/other-account expense documents."""
    client = get_client()
    documents = client.list_documents(
        "receipt",
        limit=limit,
        page=page,
        filter=filter,
        period=period,
    )
    return {
        "receipts": [
            compact_document_summary("receipt", item, client.administration_id)
            for item in documents
        ],
        "page": page,
        "count": len(documents),
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_general_journal_documents(
    limit: int = 10,
    page: int = 1,
    filter: str = "",
    period: str = "",
) -> dict[str, Any]:
    """Use this when you need a compact list of Moneybird general journal documents."""
    client = get_client()
    documents = client.list_documents(
        "general_journal_document",
        limit=limit,
        page=page,
        filter=filter,
        period=period,
    )
    return {
        "general_journal_documents": [
            compact_general_journal_summary(item, client.administration_id)
            for item in documents
        ],
        "page": page,
        "count": len(documents),
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_financial_mutations(
    limit: int = 10,
    page: int = 1,
    filter: str = "",
    period: str = "",
) -> dict[str, Any]:
    """Use this when you need a compact list of Moneybird bank or cash mutations."""
    client = get_client()
    mutations = client.list_financial_mutations(
        limit=limit,
        page=page,
        filter=filter,
        period=period,
    )
    return {
        "financial_mutations": [
            compact_financial_mutation_summary(item, client.administration_id)
            for item in mutations
        ],
        "page": page,
        "count": len(mutations),
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_profit_loss(period: str) -> dict[str, Any]:
    """Use this when you need the Moneybird profit and loss report for a specific period."""
    client = get_client()
    report = client.get_report("profit_loss", period=period)
    return {
        "title": report_title("profit_loss", period),
        "period": period,
        "report": report,
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_balance_sheet(period: str) -> dict[str, Any]:
    """Use this when you need the Moneybird balance sheet report for a specific period."""
    client = get_client()
    report = client.get_report("balance_sheet", period=period)
    return {
        "title": report_title("balance_sheet", period),
        "period": period,
        "report": report,
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_general_ledger(period: str) -> dict[str, Any]:
    """Use this when you need the Moneybird general ledger report for a specific period."""
    client = get_client()
    report = client.get_report("general_ledger", period=period)
    return {
        "title": report_title("general_ledger", period),
        "period": period,
        "report": report,
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def sync_search_index(
    invoice_filter: str = "state:all,period:this_year",
    document_filter: str = "period:this_year",
    financial_mutation_filter: str = "period:this_year",
    force_full: bool = False,
) -> dict[str, Any]:
    """Use this when you want to build or refresh a local cached Moneybird search index."""
    client = get_client()
    return sync_search_index_data(
        client,
        invoice_filter=invoice_filter,
        document_filter=document_filter,
        financial_mutation_filter=financial_mutation_filter,
        force_full=force_full,
    )


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def search_contacts(query: str, limit: int = 10) -> dict[str, Any]:
    """Use this when you need a contact lookup by partial customer id, email, phone, city, or company/person name."""
    client = get_client()
    return {"contacts": find_contact_matches(client, query=query, limit=limit)}


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_invoice_defaults_for_contact(
    contact_id: str = "",
    customer_id: str = "",
) -> dict[str, Any]:
    """Use this when you want the default workflow, document style, identity, tax, ledger, and send settings inferred from a contact's latest invoice."""
    client = get_client()
    contact = resolve_contact_reference(
        client,
        contact_id=contact_id,
        customer_id=customer_id,
    )
    defaults = infer_contact_invoice_defaults(client, contact)
    return {
        "contact": {
            "id": str(contact["id"]),
            "customer_id": contact.get("customer_id"),
            "title": contact_title(contact),
        },
        "defaults": defaults,
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_create_ledger_account(
    name: str,
    account_type: str,
    account_id: str = "",
    rgs_code: str = "",
    active: bool = True,
) -> dict[str, Any]:
    """Use this before creating a Moneybird ledger account. Do not execute the write until the user explicitly confirms."""
    if not name.strip():
        raise MoneybirdError("name is required.")
    if not account_type.strip():
        raise MoneybirdError("account_type is required.")

    client = get_client()
    payload = clean_dict(
        {
            "name": name.strip(),
            "account_type": account_type.strip(),
            "account_id": account_id.strip(),
            "active": active,
        }
    )
    fingerprint = duplicate_fingerprint(
        "create_ledger_account",
        {"ledger_account": payload, "rgs_code": rgs_code.strip()},
    )
    existing_matches = [
        compact_ledger_account_summary(item)
        for item in client.list_ledger_accounts()
        if str(item.get("name") or "") == name.strip()
    ]
    approval_payload = {
        "ledger_account": payload,
        "rgs_code": rgs_code.strip(),
        "fingerprint": fingerprint,
    }
    approval = make_approval(
        "create_ledger_account",
        approval_payload,
        f"Create ledger account '{name.strip()}'",
    )
    approval["payload"] = approval_payload
    approval["preview"] = {
        "ledger_account": payload,
        "rgs_code": rgs_code.strip(),
        "existing_name_matches": existing_matches,
    }
    return approval


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_ledger_account_from_approval(approval_id: str) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared ledger account creation."""
    client = get_client()
    pending = pop_approval(approval_id, "create_ledger_account", administration_id=client.administration_id)
    payload = pending["payload"]
    fingerprint = payload["fingerprint"]
    if audit_log_contains_success("create_ledger_account", fingerprint):
        raise MoneybirdError(
            "This ledger account payload already completed successfully according to the local audit log."
        )
    try:
        record = client.create_ledger_account(
            payload["ledger_account"],
            rgs_code=payload.get("rgs_code", ""),
        )
    except Exception as exc:
        append_failed_audit_log(
            "create_ledger_account",
            fingerprint=fingerprint,
            error=str(exc),
        )
        raise
    append_audit_log(
        {
            "action": "create_ledger_account",
            "fingerprint": fingerprint,
            "result": "success",
            "ledger_account_id": str(record.get("id")),
            "name": record.get("name"),
        }
    )
    return {
        "status": "created",
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "ledger_account": compact_ledger_account_summary(record),
        "fingerprint": fingerprint,
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_create_general_journal_document(
    reference: str,
    date: str,
    entries: list[dict[str, Any]],
    description: str = "",
) -> dict[str, Any]:
    """Use this before creating a Moneybird general journal document. Do not execute the write until the user explicitly confirms."""
    if not reference.strip():
        raise MoneybirdError("reference is required.")
    if not date.strip():
        raise MoneybirdError("date is required.")

    client = get_client()
    prepared = prepare_general_journal_entries(client, entries)
    payload = clean_dict(
        {
            "reference": reference.strip(),
            "date": date.strip(),
            "description": description.strip(),
            "general_journal_document_entries_attributes": details_attributes_payload(
                prepared["entries"]
            ),
        }
    )
    fingerprint = duplicate_fingerprint(
        "create_general_journal_document",
        {"general_journal_document": payload},
    )
    approval_payload = {
        "general_journal_document": payload,
        "fingerprint": fingerprint,
    }
    approval = make_approval(
        "create_general_journal_document",
        approval_payload,
        f"Create general journal document '{reference.strip()}'",
    )
    approval["payload"] = approval_payload
    approval["preview"] = {
        "reference": reference.strip(),
        "date": date.strip(),
        "description": description.strip(),
        "entries": prepared["preview_entries"],
        "total_debit": prepared["total_debit"],
        "total_credit": prepared["total_credit"],
    }
    return approval


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_general_journal_document_from_approval(approval_id: str) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared general journal creation."""
    client = get_client()
    pending = pop_approval(approval_id, "create_general_journal_document", administration_id=client.administration_id)
    payload = pending["payload"]
    fingerprint = payload["fingerprint"]
    if audit_log_contains_success("create_general_journal_document", fingerprint):
        raise MoneybirdError(
            "This general journal payload already completed successfully according to the local audit log."
        )
    try:
        record = client.create_general_journal_document(payload["general_journal_document"])
    except Exception as exc:
        append_failed_audit_log(
            "create_general_journal_document",
            fingerprint=fingerprint,
            error=str(exc),
        )
        raise
    append_audit_log(
        {
            "action": "create_general_journal_document",
            "fingerprint": fingerprint,
            "result": "success",
            "general_journal_document_id": str(record.get("id")),
            "reference": record.get("reference"),
        }
    )
    return {
        "status": "created",
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "general_journal_document": compact_general_journal_summary(
            record,
            client.administration_id,
        ),
        "fingerprint": fingerprint,
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_reclassify_document_lines(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Use this before reclassifying purchase invoice or receipt lines to other ledger accounts. It can optionally prepare balancing general journal documents for asset or liability moves."""
    client = get_client()
    prepared = prepare_reclassification_batch(client, entries)
    approval = make_approval(
        "reclassify_document_lines",
        prepared["payload"],
        f"Reclassify {prepared['preview']['line_count']} document line(s)",
    )
    approval["payload"] = prepared["payload"]
    approval["preview"] = prepared["preview"]
    return approval


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def reclassify_document_lines_from_approval(approval_id: str) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared document line reclassification."""
    client = get_client()
    pending = pop_approval(approval_id, "reclassify_document_lines", administration_id=client.administration_id)
    payload = pending["payload"]
    fingerprint = payload["fingerprint"]
    if audit_log_contains_success("reclassify_document_lines", fingerprint):
        raise MoneybirdError(
            "This document reclassification payload already completed successfully according to the local audit log."
        )

    updated_documents: list[dict[str, Any]] = []
    created_general_journal_documents: list[dict[str, Any]] = []
    try:
        for item in payload["document_updates"]:
            record = client.update_document(
                item["document_kind"],
                item["document_id"],
                {
                    "details_attributes": details_attributes_payload(
                        item["details_attributes"]
                    )
                },
            )
            updated_documents.append(
                {
                    "document_kind": item["document_kind"],
                    "document_id": str(record.get("id")),
                    "reference": record.get("reference"),
                    "version": record.get("version"),
                }
            )

        for item in payload["general_journal_documents"]:
            record = client.create_general_journal_document(
                item["general_journal_document"]
            )
            created_general_journal_documents.append(
                {
                    "general_journal_document_id": str(record.get("id")),
                    "reference": record.get("reference"),
                    "date": record.get("date"),
                }
            )
    except Exception as exc:
        append_failed_audit_log(
            "reclassify_document_lines",
            fingerprint=fingerprint,
            error=str(exc),
            partial={
                "updated_documents": updated_documents,
                "created_general_journal_documents": created_general_journal_documents,
            },
        )
        raise

    append_audit_log(
        {
            "action": "reclassify_document_lines",
            "fingerprint": fingerprint,
            "result": "success",
            "updated_documents": updated_documents,
            "created_general_journal_documents": created_general_journal_documents,
        }
    )
    return {
        "status": "completed",
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "updated_documents": updated_documents,
        "created_general_journal_documents": created_general_journal_documents,
        "fingerprint": fingerprint,
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_create_contact(
    company_name: str = "",
    firstname: str = "",
    lastname: str = "",
    email: str = "",
    customer_id: str = "",
    phone: str = "",
    address1: str = "",
    zipcode: str = "",
    city: str = "",
    country: str = "NL",
) -> dict[str, Any]:
    """Use this before creating a Moneybird contact. Do not execute the write until the user explicitly confirms."""
    get_client()  # Resolve and bind the active administration to the approval.
    payload = clean_dict(
        {
            "company_name": company_name,
            "firstname": firstname,
            "lastname": lastname,
            "email": email,
            "customer_id": customer_id,
            "phone": phone,
            "address1": address1,
            "zipcode": zipcode,
            "city": city,
            "country": country,
        }
    )
    if not payload:
        raise MoneybirdError("At least one contact field is required.")

    summary_name = company_name or " ".join(part for part in [firstname, lastname] if part).strip()
    summary = f"Create contact '{summary_name or 'unnamed contact'}'"
    approval = make_approval("create_contact", payload, summary)
    approval["payload"] = payload
    return approval


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_contact_from_approval(approval_id: str) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared contact creation."""
    client = get_client()
    pending = pop_approval(approval_id, "create_contact", administration_id=client.administration_id)
    record = client.create_contact(pending["payload"])
    record_id = str(record.get("id"))
    append_audit_log(
        {
            "action": "create_contact",
            "result": "success",
            "contact_id": record_id,
            "customer_id": record.get("customer_id"),
        }
    )
    return {
        "status": "created",
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "contact": {
            "id": record_id,
            "title": contact_title(record),
            "customer_id": record.get("customer_id"),
            "email": record.get("email"),
            "url": api_url("contacts", record_id, client.administration_id),
        },
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_create_sales_invoice_draft(
    contact_id: str,
    details: list[dict[str, Any]],
    reference: str = "",
    invoice_date: str = "",
    due_date: str = "",
    currency: str = "EUR",
) -> dict[str, Any]:
    """Use this before creating a draft Moneybird sales invoice. Do not execute the write until the user explicitly confirms."""
    if not details:
        raise MoneybirdError("At least one invoice line is required.")

    client = get_client()
    client.get_contact(contact_id)  # Validate scope and bind the approval to this tenant.

    normalized_details = []
    for detail in details:
        normalized_details.append(
            clean_dict(
                {
                    "description": detail.get("description"),
                    "price": detail.get("price"),
                    "amount": detail.get("amount", "1"),
                    "tax_rate_id": detail.get("tax_rate_id"),
                    "ledger_account_id": detail.get("ledger_account_id"),
                }
            )
        )

    payload = clean_dict(
        {
            "contact_id": contact_id,
            "reference": reference,
            "invoice_date": invoice_date,
            "due_date": due_date,
            "currency": currency,
            "details_attributes": normalized_details,
        }
    )
    summary = (
        f"Create draft sales invoice for contact {contact_id} "
        f"with {len(normalized_details)} line(s)"
    )
    approval = make_approval("create_sales_invoice_draft", payload, summary)
    approval["payload"] = payload
    return approval


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_sales_invoice_draft_from_approval(approval_id: str) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared draft invoice creation."""
    client = get_client()
    pending = pop_approval(approval_id, "create_sales_invoice_draft", administration_id=client.administration_id)
    record = client.create_sales_invoice(pending["payload"])
    record_id = str(record.get("id"))
    append_audit_log(
        {
            "action": "create_sales_invoice_draft",
            "result": "success",
            "sales_invoice_id": record_id,
            "contact_id": record.get("contact_id"),
            "reference": record.get("reference"),
        }
    )
    return {
        "status": "created",
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "sales_invoice": {
            "id": record_id,
            "invoice_id": record.get("invoice_id"),
            "state": record.get("state"),
            "contact_id": record.get("contact_id"),
            "reference": record.get("reference"),
            "total_price_incl_tax": record.get("total_price_incl_tax"),
            "url": api_url("sales_invoices", record_id, client.administration_id),
        },
    }


def _prepare_batch_create_sales_invoices(
    client: Any,
    entries: list[dict[str, Any]],
    skip_if_duplicate: bool = True,
    fail_on_duplicate: bool = False,
) -> dict[str, Any]:
    if not entries:
        raise MoneybirdError("Provide at least one batch entry.")
    batch_items = [build_batch_invoice_payload(client, entry) for entry in entries]
    apply_batch_group_merge_checks(batch_items)
    preview = summarize_batch_preview(batch_items)
    if fail_on_duplicate and preview["duplicate_count"]:
        raise MoneybirdError(
            "Potential duplicates found. Review the preview and rerun with fail_on_duplicate false if you want to continue."
        )

    payload = {
        "items": batch_items,
        "skip_if_duplicate": skip_if_duplicate,
        "fail_on_duplicate": fail_on_duplicate,
    }
    fingerprint = duplicate_fingerprint("batch_create_sales_invoices", payload)
    approval = make_approval(
        "batch_create_sales_invoices",
        {**payload, "fingerprint": fingerprint},
        f"Create {len(batch_items)} sales invoice(s) in batch",
    )
    approval["preview"] = preview
    approval["payload"] = {**payload, "fingerprint": fingerprint}
    return approval


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_batch_create_sales_invoices(
    entries: list[dict[str, Any]],
    skip_if_duplicate: bool = True,
    fail_on_duplicate: bool = False,
) -> dict[str, Any]:
    """Use this before creating multiple sales invoices in one batch. It returns a preview table, duplicate warnings, and an automatic merge-compatibility check before any write happens."""
    return _prepare_batch_create_sales_invoices(
        get_client(),
        entries,
        skip_if_duplicate=skip_if_duplicate,
        fail_on_duplicate=fail_on_duplicate,
    )


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def batch_create_sales_invoices_from_approval(approval_id: str) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared batch invoice creation."""
    client = get_client()
    pending = pop_approval(approval_id, "batch_create_sales_invoices", administration_id=client.administration_id)
    payload = pending["payload"]
    fingerprint = payload["fingerprint"]
    if audit_log_contains_success("batch_create_sales_invoices", fingerprint):
        raise MoneybirdError(
            "This batch payload already completed successfully according to the local audit log."
        )

    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    try:
        for item in payload["items"]:
            if item["duplicates"] and payload["skip_if_duplicate"]:
                skipped.append(
                    {
                        "customer_id": item["contact"].get("customer_id"),
                        "reason": "potential_duplicate",
                        "duplicates": item["duplicates"],
                    }
                )
                continue

            record = client.create_sales_invoice(item["sales_invoice"])
            result_row = {
                "customer_id": item["contact"].get("customer_id"),
                "sales_invoice_id": str(record.get("id")),
                "invoice_id": record.get("invoice_id"),
                "state": record.get("state"),
                "reference": record.get("reference"),
                "expected_total_incl_tax": item.get("expected_total_incl_tax"),
                "expected_state": "scheduled" if item["schedule_send_on"] else "draft",
                "expected_invoice_date": item["schedule_send_on"]
                or item["sales_invoice"].get("invoice_date"),
            }
            if item["schedule_send_on"]:
                record = client.send_sales_invoice(str(record["id"]), item["send_payload"])
                result_row.update(
                    {
                        "state": record.get("state"),
                        "invoice_date": record.get("invoice_date"),
                        "sent_at": record.get("sent_at"),
                    }
                )
            created.append(result_row)
    except Exception as exc:
        append_failed_audit_log(
            "batch_create_sales_invoices",
            fingerprint=fingerprint,
            error=str(exc),
            partial={"created": created, "skipped": skipped},
        )
        raise

    fetched: list[dict[str, Any]] = []
    created_ids = [row["sales_invoice_id"] for row in created]
    for id_batch in chunked(created_ids, 100):
        fetched.extend(client.fetch_sales_invoices_by_ids(id_batch))
    fetched_by_id = {str(item.get("id")): item for item in fetched}
    verification: list[dict[str, Any]] = []
    for row in created:
        invoice = fetched_by_id.get(row["sales_invoice_id"], {})
        checks = {
            "total_matches": str(invoice.get("total_price_incl_tax"))
            == str(row.get("expected_total_incl_tax")),
            "state_matches": str(invoice.get("state")) == str(row.get("expected_state")),
            "invoice_date_matches": (
                not row.get("expected_invoice_date")
                or str(invoice.get("invoice_date")) == str(row.get("expected_invoice_date"))
            ),
            "not_sent_yet": invoice.get("sent_at") in (None, ""),
        }
        verification.append(
            {
                "customer_id": row.get("customer_id"),
                "sales_invoice_id": row["sales_invoice_id"],
                "state": invoice.get("state"),
                "invoice_date": invoice.get("invoice_date"),
                "sent_at": invoice.get("sent_at"),
                "total_price_incl_tax": invoice.get("total_price_incl_tax"),
                "expected_total_incl_tax": row.get("expected_total_incl_tax"),
                "checks": checks,
                "verified": all(checks.values()),
            }
        )
    all_verified = all(row["verified"] for row in verification)

    append_audit_log(
        {
            "action": "batch_create_sales_invoices",
            "fingerprint": fingerprint,
            "result": "success",
            "created": created,
            "skipped": skipped,
            "verification": verification,
        }
    )
    return {
        "status": "completed" if all_verified else "completed_with_verification_errors",
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "created": created,
        "skipped": skipped,
        "verification": verification,
        "all_verified": all_verified,
        "fingerprint": fingerprint,
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_batch_update_sales_invoices(
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Use this before updating one or more existing sales invoices, either by explicit invoice id or by customer lookup plus filters."""
    if not entries:
        raise MoneybirdError("Provide at least one batch update entry.")

    client = get_client()
    prepared_items: list[dict[str, Any]] = []
    preview_rows: list[dict[str, Any]] = []

    for entry in entries:
        sales_invoice_id = str(entry.get("sales_invoice_id", "")).strip()
        if sales_invoice_id:
            invoice = client.get_sales_invoice(sales_invoice_id)
        else:
            customer_id = str(entry.get("customer_id", "")).strip()
            if not customer_id:
                raise MoneybirdError("Each update entry needs sales_invoice_id or customer_id.")
            contact = client.get_contact_by_customer_id(customer_id)
            matches = client.list_sales_invoices(
                limit=10,
                page=1,
                state=entry.get("state", "all"),
                reference=str(entry.get("reference", "")),
                contact_id=str(contact["id"]),
                period=str(entry.get("period_filter", "this_year")),
            )
            if len(matches) != 1:
                raise MoneybirdError(
                    f"Expected exactly one invoice for customer_id {customer_id}, got {len(matches)}."
                )
            invoice = client.get_sales_invoice(str(matches[0]["id"]))

        details_patch = []
        for detail_update in entry.get("detail_updates", []):
            row_order = int(detail_update.get("row_order", 0))
            details = invoice.get("details") or []
            matching = next((detail for detail in details if int(detail.get("row_order", 0)) == row_order), None)
            if not matching:
                raise MoneybirdError(
                    f"Could not find detail row_order {row_order} on invoice {invoice.get('id')}."
                )
            details_patch.append(
                clean_dict(
                    {
                        "id": matching["id"],
                        "description": detail_update.get("description", ""),
                        "period": detail_update.get("period", ""),
                        "price": detail_update.get("price", ""),
                        "amount": detail_update.get("amount", ""),
                        "tax_rate_id": detail_update.get("tax_rate_id"),
                        "ledger_account_id": detail_update.get("ledger_account_id"),
                    }
                )
            )

        sales_invoice_patch = clean_dict(
            {
                "reference": entry.get("new_reference", None),
                "invoice_date": entry.get("invoice_date", ""),
                "due_date": entry.get("due_date", ""),
                "details_attributes": details_patch,
            }
        )
        prepared_items.append(
            {
                "sales_invoice_id": str(invoice["id"]),
                "invoice_id": invoice.get("invoice_id"),
                "customer_id": invoice.get("contact", {}).get("customer_id"),
                "patch": sales_invoice_patch,
            }
        )
        preview_rows.append(
            {
                "customer_id": invoice.get("contact", {}).get("customer_id"),
                "description": ", ".join(
                    detail.get("description", "")
                    for detail in details_patch
                    if detail.get("description")
                )
                or "invoice update",
                "amount_excl_tax": "",
                "amount_tax": "",
                "amount_incl_tax": "",
                "status": "ready",
            }
        )

    fingerprint = duplicate_fingerprint(
        "batch_update_sales_invoices",
        {"items": prepared_items},
    )
    approval = make_approval(
        "batch_update_sales_invoices",
        {"items": prepared_items, "fingerprint": fingerprint},
        f"Update {len(prepared_items)} sales invoice(s) in batch",
    )
    approval["preview"] = {
        "preview_table": render_preview_table(preview_rows),
        "item_count": len(prepared_items),
    }
    approval["payload"] = {"items": prepared_items, "fingerprint": fingerprint}
    return approval


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def batch_update_sales_invoices_from_approval(approval_id: str) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared batch invoice update."""
    client = get_client()
    pending = pop_approval(approval_id, "batch_update_sales_invoices", administration_id=client.administration_id)
    payload = pending["payload"]
    fingerprint = payload["fingerprint"]
    if audit_log_contains_success("batch_update_sales_invoices", fingerprint):
        raise MoneybirdError(
            "This batch update payload already completed successfully according to the local audit log."
        )

    updated: list[dict[str, Any]] = []
    try:
        for item in payload["items"]:
            record = client.update_sales_invoice(item["sales_invoice_id"], item["patch"])
            updated.append(
                {
                    "sales_invoice_id": str(record.get("id")),
                    "invoice_id": record.get("invoice_id"),
                    "customer_id": record.get("contact", {}).get("customer_id"),
                    "state": record.get("state"),
                }
            )
    except Exception as exc:
        append_failed_audit_log(
            "batch_update_sales_invoices",
            fingerprint=fingerprint,
            error=str(exc),
            partial={"updated": updated},
        )
        raise

    append_audit_log(
        {
            "action": "batch_update_sales_invoices",
            "fingerprint": fingerprint,
            "result": "success",
            "updated": updated,
        }
    )
    return {
        "status": "completed",
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "updated": updated,
        "fingerprint": fingerprint,
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_send_sales_invoice(
    sales_invoice_id: str,
    sending_scheduled: bool = False,
    invoice_date: str = "",
    delivery_method: str = "",
    email_address: str = "",
    email_message: str = "",
) -> dict[str, Any]:
    """Use this before sending or scheduling a Moneybird sales invoice. Do not execute the send until the user explicitly confirms. Scheduled sends automatically include a merge-compatibility check against other invoices already planned for that contact/date."""
    if sending_scheduled and not invoice_date:
        raise MoneybirdError(
            "invoice_date is required when sending_scheduled is true."
        )

    client = get_client()
    record = client.get_sales_invoice(sales_invoice_id)
    payload = clean_dict(
        {
            "sending_scheduled": sending_scheduled,
            "invoice_date": invoice_date,
            "delivery_method": delivery_method,
            "email_address": email_address,
            "email_message": email_message,
        }
    )
    summary = (
        f"Send sales invoice {sales_invoice_id} now"
        if not sending_scheduled
        else f"Schedule sales invoice {sales_invoice_id} for {invoice_date}"
    )
    approval = make_approval(
        "send_sales_invoice",
        {
            "sales_invoice_id": sales_invoice_id,
            "sales_invoice_sending": payload,
        },
        summary,
    )
    approval["payload"] = {
        "sales_invoice_id": sales_invoice_id,
        "sales_invoice_sending": payload,
    }
    merge_check = {
        "checked": False,
        "status": "not_scheduled",
        "summary": "No automatic merge check because this invoice is not scheduled.",
    }
    if sending_scheduled:
        candidates = list_scheduled_merge_candidates(
            client,
            contact_id=str(record.get("contact_id") or record.get("contact", {}).get("id") or ""),
            scheduled_send_on=invoice_date,
            exclude_sales_invoice_id=sales_invoice_id,
        )
        merge_check = evaluate_merge_compatibility(
            build_merge_snapshot_from_invoice(
                record,
                scheduled_send_on=invoice_date,
            ),
            candidates,
        )
    approval["merge_check"] = merge_check
    return approval


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def send_sales_invoice_from_approval(approval_id: str) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared invoice send action."""
    client = get_client()
    pending = pop_approval(approval_id, "send_sales_invoice", administration_id=client.administration_id)
    payload = pending["payload"]
    record = client.send_sales_invoice(
        payload["sales_invoice_id"],
        payload["sales_invoice_sending"],
    )
    record_id = str(record.get("id"))
    append_audit_log(
        {
            "action": "send_sales_invoice",
            "result": "success",
            "sales_invoice_id": record_id,
            "state": record.get("state"),
            "invoice_date": record.get("invoice_date"),
        }
    )
    return {
        "status": "sent_or_scheduled",
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "sales_invoice": {
            "id": record_id,
            "invoice_id": record.get("invoice_id"),
            "state": record.get("state"),
            "invoice_date": record.get("invoice_date"),
            "sent_at": record.get("sent_at"),
            "url": api_url("sales_invoices", record_id, client.administration_id),
        },
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_batch_schedule_sales_invoices(
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Prepare multiple existing draft invoices for future sending in one approval.

    Each entry needs ``sales_invoice_id`` and ``invoice_date``. Optional delivery_method,
    email_address and email_message override the contact/workflow defaults.
    """
    if not entries:
        raise MoneybirdError("Provide at least one invoice to schedule.")

    client = get_client()
    ids = [str(entry.get("sales_invoice_id") or "").strip() for entry in entries]
    if any(not item_id for item_id in ids):
        raise MoneybirdError("Each schedule entry needs sales_invoice_id.")
    if len(set(ids)) != len(ids):
        raise MoneybirdError("sales_invoice_id values must be unique within the batch.")

    invoices: list[dict[str, Any]] = []
    for id_batch in chunked(ids, 100):
        invoices.extend(client.fetch_sales_invoices_by_ids(id_batch))
    invoices_by_id = {str(invoice.get("id")): invoice for invoice in invoices}
    if set(invoices_by_id) != set(ids):
        missing = sorted(set(ids) - set(invoices_by_id))
        raise MoneybirdError(f"Could not fetch sales invoice(s): {', '.join(missing)}.")

    prepared_items: list[dict[str, Any]] = []
    preview_rows: list[dict[str, Any]] = []
    merge_checks: list[dict[str, Any]] = []
    for entry, sales_invoice_id in zip(entries, ids):
        invoice = invoices_by_id[sales_invoice_id]
        invoice_date = str(entry.get("invoice_date") or "").strip()
        if not invoice_date:
            raise MoneybirdError(
                f"invoice_date is required for sales invoice {sales_invoice_id}."
            )
        state = str(invoice.get("state") or "")
        already_scheduled = state == "scheduled" and str(invoice.get("invoice_date")) == invoice_date
        if state not in {"draft", "scheduled"}:
            raise MoneybirdError(
                f"Sales invoice {sales_invoice_id} has state {state}; only draft or scheduled invoices can be prepared."
            )
        if state == "scheduled" and not already_scheduled:
            raise MoneybirdError(
                f"Sales invoice {sales_invoice_id} is already scheduled for {invoice.get('invoice_date')}."
            )

        send_payload = clean_dict(
            {
                "sending_scheduled": True,
                "invoice_date": invoice_date,
                "delivery_method": entry.get("delivery_method", ""),
                "email_address": entry.get("email_address", ""),
                "email_message": entry.get("email_message", ""),
            }
        )
        candidates = list_scheduled_merge_candidates(
            client,
            contact_id=str(invoice.get("contact_id") or (invoice.get("contact") or {}).get("id") or ""),
            scheduled_send_on=invoice_date,
            exclude_sales_invoice_id=sales_invoice_id,
        )
        merge_check = evaluate_merge_compatibility(
            build_merge_snapshot_from_invoice(invoice, scheduled_send_on=invoice_date),
            candidates,
        )
        merge_checks.append(
            {
                "customer_id": (invoice.get("contact") or {}).get("customer_id"),
                "sales_invoice_id": sales_invoice_id,
                **merge_check,
            }
        )
        prepared_items.append(
            {
                "sales_invoice_id": sales_invoice_id,
                "customer_id": (invoice.get("contact") or {}).get("customer_id"),
                "before_total_price_incl_tax": invoice.get("total_price_incl_tax"),
                "already_scheduled": already_scheduled,
                "sales_invoice_sending": send_payload,
            }
        )
        preview_rows.append(
            {
                "customer_id": (invoice.get("contact") or {}).get("customer_id") or sales_invoice_id,
                "description": ", ".join(
                    str(detail.get("description") or "") for detail in (invoice.get("details") or [])
                ),
                "amount_excl_tax": invoice.get("total_price_excl_tax") or "",
                "amount_tax": "",
                "amount_incl_tax": invoice.get("total_price_incl_tax") or "",
                "status": "already-scheduled" if already_scheduled else "ready",
            }
        )

    payload = {"items": prepared_items}
    fingerprint = duplicate_fingerprint("batch_schedule_sales_invoices", payload)
    approval = make_approval(
        "batch_schedule_sales_invoices",
        {**payload, "fingerprint": fingerprint},
        f"Schedule {len(prepared_items)} sales invoice(s)",
    )
    approval["payload"] = {**payload, "fingerprint": fingerprint}
    approval["preview"] = {
        "preview_table": render_preview_table(preview_rows),
        "item_count": len(prepared_items),
        "merge_checks": merge_checks,
    }
    return approval


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def batch_schedule_sales_invoices_from_approval(approval_id: str) -> dict[str, Any]:
    """Schedule a prepared invoice batch and verify every resulting invoice."""
    client = get_client()
    pending = pop_approval(
        approval_id,
        "batch_schedule_sales_invoices",
        administration_id=client.administration_id,
    )
    payload = pending["payload"]
    fingerprint = payload["fingerprint"]
    if audit_log_contains_success("batch_schedule_sales_invoices", fingerprint):
        raise MoneybirdError(
            "This schedule batch already completed successfully according to the local audit log."
        )

    scheduled: list[dict[str, Any]] = []
    try:
        for item in payload["items"]:
            if item.get("already_scheduled"):
                scheduled.append(
                    {
                        "sales_invoice_id": item["sales_invoice_id"],
                        "customer_id": item.get("customer_id"),
                        "action": "already_scheduled",
                    }
                )
                continue
            record = client.send_sales_invoice(
                item["sales_invoice_id"],
                item["sales_invoice_sending"],
            )
            scheduled.append(
                {
                    "sales_invoice_id": str(record.get("id")),
                    "customer_id": item.get("customer_id"),
                    "action": "scheduled",
                }
            )
    except Exception as exc:
        append_failed_audit_log(
            "batch_schedule_sales_invoices",
            fingerprint=fingerprint,
            error=str(exc),
            partial={"scheduled": scheduled},
        )
        raise

    ids = [item["sales_invoice_id"] for item in payload["items"]]
    fetched: list[dict[str, Any]] = []
    for id_batch in chunked(ids, 100):
        fetched.extend(client.fetch_sales_invoices_by_ids(id_batch))
    fetched_by_id = {str(invoice.get("id")): invoice for invoice in fetched}

    verification: list[dict[str, Any]] = []
    for item in payload["items"]:
        invoice = fetched_by_id.get(item["sales_invoice_id"], {})
        expected_date = item["sales_invoice_sending"]["invoice_date"]
        checks = {
            "total_unchanged": str(invoice.get("total_price_incl_tax"))
            == str(item.get("before_total_price_incl_tax")),
            "state_scheduled": invoice.get("state") == "scheduled",
            "invoice_date_matches": invoice.get("invoice_date") == expected_date,
            "not_sent_yet": invoice.get("sent_at") in (None, ""),
        }
        verification.append(
            {
                "customer_id": item.get("customer_id"),
                "sales_invoice_id": item["sales_invoice_id"],
                "state": invoice.get("state"),
                "invoice_date": invoice.get("invoice_date"),
                "sent_at": invoice.get("sent_at"),
                "total_price_incl_tax": invoice.get("total_price_incl_tax"),
                "checks": checks,
                "verified": all(checks.values()),
            }
        )
    all_verified = all(item["verified"] for item in verification)
    append_audit_log(
        {
            "action": "batch_schedule_sales_invoices",
            "fingerprint": fingerprint,
            "result": "success",
            "scheduled": scheduled,
            "verification": verification,
        }
    )
    return {
        "status": "completed" if all_verified else "completed_with_verification_errors",
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "scheduled": scheduled,
        "verification": verification,
        "all_verified": all_verified,
        "fingerprint": fingerprint,
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_meter_usage_sales_invoices(
    rows: list[dict[str, Any]],
    period_label: str,
    invoice_date: str,
    schedule_send_on: str = "",
    minimum_usage_kwh: str = "0",
    description_prefix: str = "Elektra",
    default_unit_price: str = "",
    default_tax_rate_id: str = "",
    default_ledger_account_id: str = "",
    skip_meters: list[str] | None = None,
) -> dict[str, Any]:
    """Prepare a complete metered-usage invoice run from readings or supplied usage.

    Each row accepts ``meter``, optional ``customer_id``, and either ``usage_kwh`` or
    ``begin_reading`` + ``end_reading``. ``action`` may be ``skip``, ``draft``,
    ``schedule``, ``merge`` or ``separate``. When price/tax/ledger are omitted, the
    newest matching invoice line (for example ``Elektra B5``) supplies those defaults.
    """
    client = get_client()
    prepared_usage = build_meter_usage_entries(
        client,
        rows=rows,
        period_label=period_label,
        invoice_date=invoice_date,
        schedule_send_on=schedule_send_on,
        minimum_usage_kwh=minimum_usage_kwh,
        description_prefix=description_prefix,
        default_unit_price=default_unit_price,
        default_tax_rate_id=default_tax_rate_id,
        default_ledger_account_id=default_ledger_account_id,
        skip_meters=skip_meters,
    )
    approval = _prepare_batch_create_sales_invoices(
        client,
        prepared_usage["entries"],
        skip_if_duplicate=True,
        fail_on_duplicate=True,
    )
    merge_checks = {
        str(item.get("customer_id") or ""): item
        for item in (approval.get("preview") or {}).get("merge_checks", [])
    }
    intent_warnings: list[dict[str, Any]] = []
    for decision in prepared_usage["decisions"]:
        intent = decision.get("merge_intent")
        check = merge_checks.get(str(decision.get("customer_id") or ""), {})
        if intent == "merge" and check.get("status") != "compatible":
            intent_warnings.append(
                {
                    "customer_id": decision.get("customer_id"),
                    "intent": "merge",
                    "warning": (
                        "No currently scheduled compatible invoice was found; "
                        "merging may only become verifiable when the recurring invoice exists."
                    ),
                }
            )
        if intent == "separate" and check.get("status") == "compatible":
            intent_warnings.append(
                {
                    "customer_id": decision.get("customer_id"),
                    "intent": "separate",
                    "warning": "A compatible scheduled invoice exists, so Moneybird may merge them.",
                }
            )
    approval["meter_usage_preview"] = {
        "period_label": period_label,
        "invoice_date": invoice_date,
        "schedule_send_on": schedule_send_on,
        "minimum_usage_kwh": minimum_usage_kwh,
        "decisions": prepared_usage["decisions"],
        "intent_warnings": intent_warnings,
        "invoice_preview": approval.get("preview"),
    }
    return approval


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def meter_usage_sales_invoices_from_approval(approval_id: str) -> dict[str, Any]:
    """Execute an approved metered-usage run and return automatic verification."""
    return batch_create_sales_invoices_from_approval(approval_id)


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_pause_sales_invoice_workflow(sales_invoice_id: str) -> dict[str, Any]:
    """Use this before pausing a sales invoice workflow. This is the safe way to stop a scheduled send from going out automatically."""
    client = get_client()
    record = client.get_sales_invoice(sales_invoice_id)
    summary = f"Pause workflow for sales invoice {record.get('invoice_id') or record.get('id')}"
    approval = make_approval(
        "pause_sales_invoice_workflow",
        {"sales_invoice_id": sales_invoice_id},
        summary,
    )
    approval["payload"] = {"sales_invoice_id": sales_invoice_id}
    return approval


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def pause_sales_invoice_workflow_from_approval(approval_id: str) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed pausing the invoice workflow."""
    client = get_client()
    pending = pop_approval(approval_id, "pause_sales_invoice_workflow", administration_id=client.administration_id)
    record = client.pause_sales_invoice(pending["payload"]["sales_invoice_id"])
    record_id = str(record.get("id"))
    append_audit_log(
        {
            "action": "pause_sales_invoice_workflow",
            "result": "success",
            "sales_invoice_id": record_id,
            "state": record.get("state"),
            "paused": record.get("paused"),
        }
    )
    return {
        "status": "paused",
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "sales_invoice": {
            "id": record_id,
            "invoice_id": record.get("invoice_id"),
            "state": record.get("state"),
            "paused": record.get("paused"),
            "url": api_url("sales_invoices", record_id, client.administration_id),
        },
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_resume_sales_invoice_workflow(sales_invoice_id: str) -> dict[str, Any]:
    """Use this before resuming a previously paused sales invoice workflow."""
    client = get_client()
    record = client.get_sales_invoice(sales_invoice_id)
    summary = f"Resume workflow for sales invoice {record.get('invoice_id') or record.get('id')}"
    approval = make_approval(
        "resume_sales_invoice_workflow",
        {"sales_invoice_id": sales_invoice_id},
        summary,
    )
    approval["payload"] = {"sales_invoice_id": sales_invoice_id}
    return approval


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def resume_sales_invoice_workflow_from_approval(approval_id: str) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed resuming the invoice workflow."""
    client = get_client()
    pending = pop_approval(approval_id, "resume_sales_invoice_workflow", administration_id=client.administration_id)
    record = client.resume_sales_invoice(pending["payload"]["sales_invoice_id"])
    record_id = str(record.get("id"))
    append_audit_log(
        {
            "action": "resume_sales_invoice_workflow",
            "result": "success",
            "sales_invoice_id": record_id,
            "state": record.get("state"),
            "paused": record.get("paused"),
        }
    )
    return {
        "status": "resumed",
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "sales_invoice": {
            "id": record_id,
            "invoice_id": record.get("invoice_id"),
            "state": record.get("state"),
            "paused": record.get("paused"),
            "url": api_url("sales_invoices", record_id, client.administration_id),
        },
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_set_contacts_delivery_method_email(
    include_archived_contacts: bool = False,
) -> dict[str, Any]:
    """Use this before bulk-changing Moneybird contacts so invoice delivery_method becomes Email. Do not execute the write until the user explicitly confirms."""
    client = get_client()
    audit = build_invoice_delivery_audit(
        client,
        include_archived_contacts=include_archived_contacts,
    )
    contacts = audit["non_email_contacts"]
    if not contacts:
        return {
            "status": "no_changes_needed",
            "summary": "All checked contacts already have delivery_method Email.",
            "audit_summary": audit["summary"],
            "non_email_contacts": [],
        }

    payload = {
        "contact_ids": [item["contact_id"] for item in contacts],
        "include_archived_contacts": include_archived_contacts,
    }
    fingerprint = duplicate_fingerprint(
        "set_contacts_delivery_method_email",
        payload,
    )
    payload["fingerprint"] = fingerprint
    summary = f"Set delivery_method Email for {len(contacts)} contact(s)"
    approval = make_approval(
        "set_contacts_delivery_method_email",
        payload,
        summary,
    )
    approval["payload"] = payload
    approval["preview"] = {
        "contact_count": len(contacts),
        "preview_table": render_contact_delivery_table(contacts),
        "contacts": contacts,
        "audit_summary": audit["summary"],
    }
    return approval


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def set_contacts_delivery_method_email_from_approval(approval_id: str) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed bulk-updating contact invoice delivery methods to Email."""
    client = get_client()
    pending = pop_approval(approval_id, "set_contacts_delivery_method_email", administration_id=client.administration_id)
    payload = pending["payload"]
    fingerprint = payload["fingerprint"]
    if audit_log_contains_success("set_contacts_delivery_method_email", fingerprint):
        raise MoneybirdError(
            "This contact delivery-method payload already completed successfully according to the local audit log."
        )

    updated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    try:
        for contact_id in payload["contact_ids"]:
            before = client.get_contact(str(contact_id))
            before_record = contact_delivery_record(before, client.administration_id)
            if before_record["delivery_method"] == "Email":
                skipped.append({**before_record, "reason": "already_email"})
                continue

            record = client.update_contact(str(contact_id), {"delivery_method": "Email"})
            after_record = contact_delivery_record(record, client.administration_id)
            updated.append(
                {
                    **after_record,
                    "delivery_method_before": before_record["delivery_method"],
                    "delivery_method_after": after_record["delivery_method"],
                }
            )
    except Exception as exc:
        append_failed_audit_log(
            "set_contacts_delivery_method_email",
            fingerprint=fingerprint,
            error=str(exc),
            partial={"updated": updated, "skipped": skipped},
        )
        raise

    verification = build_invoice_delivery_audit(
        client,
        include_archived_contacts=bool(payload.get("include_archived_contacts")),
    )
    append_audit_log(
        {
            "action": "set_contacts_delivery_method_email",
            "fingerprint": fingerprint,
            "result": "success",
            "updated_count": len(updated),
            "skipped_count": len(skipped),
            "remaining_non_email_contact_count": verification["summary"][
                "non_email_contact_count"
            ],
            "remaining_recurring_issue_count": verification["summary"][
                "recurring_issue_count"
            ],
        }
    )
    return {
        "status": "completed",
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "updated_count": len(updated),
        "skipped_count": len(skipped),
        "updated": updated,
        "skipped": skipped,
        "verification_summary": verification["summary"],
        "remaining_non_email_contacts": verification["non_email_contacts"],
        "remaining_recurring_issues": verification["recurring_issues"],
        "fingerprint": fingerprint,
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_update_contact(
    contact_id: str,
    company_name: str = "",
    firstname: str = "",
    lastname: str = "",
    email: str = "",
    phone: str = "",
    customer_id: str = "",
    address1: str = "",
    zipcode: str = "",
    city: str = "",
    country: str = "",
    send_invoices_to_email: str = "",
    delivery_method: str = "",
    clear_fields: list[str] | None = None,
) -> dict[str, Any]:
    """Use this before updating a Moneybird contact. Do not execute the write until the user explicitly confirms."""
    allowed_clear_fields = {
        "company_name",
        "firstname",
        "lastname",
        "email",
        "phone",
        "customer_id",
        "address1",
        "zipcode",
        "city",
        "country",
        "send_invoices_to_email",
    }
    update_payload = clean_dict(
        {
            "company_name": company_name,
            "firstname": firstname,
            "lastname": lastname,
            "email": email,
            "phone": phone,
            "customer_id": customer_id,
            "address1": address1,
            "zipcode": zipcode,
            "city": city,
            "country": country,
            "send_invoices_to_email": send_invoices_to_email,
            "delivery_method": delivery_method,
        }
    )

    clear_fields = clear_fields or []
    invalid_fields = [field for field in clear_fields if field not in allowed_clear_fields]
    if invalid_fields:
        raise MoneybirdError(
            f"Unsupported clear_fields: {', '.join(sorted(invalid_fields))}"
        )
    for field in clear_fields:
        update_payload[field] = ""

    if not update_payload:
        raise MoneybirdError("Provide at least one field to update or clear.")

    summary = (
        f"Update contact {contact_id} fields: "
        + ", ".join(sorted(update_payload.keys()))
    )
    approval = make_approval(
        "update_contact",
        {"contact_id": contact_id, "contact": update_payload},
        summary,
    )
    approval["payload"] = {"contact_id": contact_id, "contact": update_payload}
    return approval


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def update_contact_from_approval(approval_id: str) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared contact update."""
    client = get_client()
    pending = pop_approval(approval_id, "update_contact", administration_id=client.administration_id)
    payload = pending["payload"]
    record = client.update_contact(payload["contact_id"], payload["contact"])
    record_id = str(record.get("id"))
    append_audit_log(
        {
            "action": "update_contact",
            "result": "success",
            "contact_id": record_id,
            "customer_id": record.get("customer_id"),
        }
    )
    return {
        "status": "updated",
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "contact": {
            "id": record_id,
            "title": contact_title(record),
            "customer_id": record.get("customer_id"),
            "email": record.get("email"),
            "archived": record.get("archived"),
            "url": api_url("contacts", record_id, client.administration_id),
        },
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_archive_contact(contact_id: str) -> dict[str, Any]:
    """Use this before archiving a Moneybird contact. Do not execute the archive until the user explicitly confirms."""
    client = get_client()
    record = client.get_contact(contact_id)
    summary = f"Archive contact {contact_title(record)}"
    approval = make_approval(
        "archive_contact",
        {"contact_id": contact_id},
        summary,
    )
    approval["payload"] = {"contact_id": contact_id}
    return approval


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def archive_contact_from_approval(approval_id: str) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared contact archive."""
    client = get_client()
    pending = pop_approval(approval_id, "archive_contact", administration_id=client.administration_id)
    payload = pending["payload"]
    client.archive_contact(payload["contact_id"])
    record = client.get_contact(payload["contact_id"])
    record_id = str(record.get("id"))
    append_audit_log(
        {
            "action": "archive_contact",
            "result": "success",
            "contact_id": record_id,
            "customer_id": record.get("customer_id"),
            "archived": record.get("archived"),
        }
    )
    return {
        "status": "archived",
        "approved_at": iso_now(),
        "summary": pending["summary"],
        "contact": {
            "id": record_id,
            "title": contact_title(record),
            "customer_id": record.get("customer_id"),
            "archived": record.get("archived"),
            "url": api_url("contacts", record_id, client.administration_id),
        },
    }


# Register the guidance layer (playbook resource + scenario prompts) on this server.
# Imported last so `mcp` and all tools are already defined; guidance.py does not import
# this module, so there is no circular import.
from .guidance import register_guidance

register_guidance(mcp)
