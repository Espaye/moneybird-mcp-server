"""Administration selection, generic GET escape hatch, and the local search index."""
from __future__ import annotations

import os
from typing import Annotated, Any

from pydantic import Field

from .. import __version__
from ..client import normalize_generic_get_path, validate_moneybird_id
from ..config import (
    READ_ONLY_ANNOTATIONS,
    MoneybirdError,
)
from ..credentials import (
    CREDENTIAL_MODE_HOSTED_REQUEST_ONLY,
    credentials_are_configured,
    get_credential_mode,
    missing_credentials_message,
)
from ..formatting import (
    api_url,
    contact_title,
    document_search_record,
    document_url,
    financial_mutation_search_record,
    financial_mutation_title,
    general_journal_search_record,
    general_journal_title,
    invoice_title,
    matches_query,
    normalize_text,
    purchase_document_title,
    stringify_record,
)
from ..invoicing import (
    find_contact_matches,
)
from ..search_fts import refresh_fts_index, search_fts
from ..sync import (
    load_sync_index,
    sync_search_index_data,
)
from ..telemetry import performance_snapshot, tenant_scope_for_token
from . import _context as ctx
from ._params import FilterString, GenericGetPath, Limit, SearchRecordId
from ._registry import mcp


@mcp.tool(
    annotations=READ_ONLY_ANNOTATIONS,
    tags={"domain:core", "capability:read", "always-visible"},
)
def get_server_status(
    recent_tools: Annotated[
        int,
        Field(
            ge=1,
            le=100,
            description="How many recent MCP tool timings to include (1-100).",
        ),
    ] = 20,
) -> dict[str, Any]:
    """Return build, credential, and privacy-safe process diagnostics."""
    credential_mode = get_credential_mode()
    if (
        credential_mode != CREDENTIAL_MODE_HOSTED_REQUEST_ONLY
        and not credentials_are_configured(credential_mode)
    ):
        # This is the one status tool that remains useful before setup is
        # complete.  Do not resolve credentials: OAuth resolution can contact
        # Moneybird and refresh the durable token store.
        return {
            "version": __version__,
            "credential_state": {
                "mode": credential_mode,
                "configured": False,
                "message": missing_credentials_message(credential_mode),
            },
            "tool_discovery": os.environ.get(
                "MONEYBIRD_TOOL_DISCOVERY",
                "full",
            ),
            "performance": performance_snapshot(
                recent_tools=recent_tools,
                tenant_scope=None,
            ),
        }

    # Resolve only the caller's credential scope; require_administration=False
    # avoids a Moneybird API call when no administration id was configured.
    client = ctx.get_client(require_administration=False)
    return {
        "version": __version__,
        "credential_state": {
            "mode": credential_mode,
            "configured": True,
        },
        "tool_discovery": os.environ.get(
            "MONEYBIRD_TOOL_DISCOVERY",
            "full",
        ),
        "performance": performance_snapshot(
            recent_tools=recent_tools,
            tenant_scope=tenant_scope_for_token(client.token),
        ),
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_administrations() -> dict[str, Any]:
    """Use this when you need to inspect which Moneybird administrations are available to the token."""
    client = ctx.get_client(require_administration=False)
    administrations = client.list_administrations()
    # The caller's resolved credential context is authoritative. Consulting the
    # process-global environment here would expose an operator default to hosted
    # request-scoped callers.
    configured_id = client.administration_id
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
def search(
    query: Annotated[str, Field(description="Free-text search over the synced index: contacts, invoices, documents, and bank mutations.")],
    limit: Limit = 8,
) -> dict[str, Any]:
    """Use this when you want ChatGPT to search Moneybird records in a connector-friendly way."""
    client = ctx.get_client()
    # Administration-keyed files are not an authorization boundary. Revalidate
    # the active token/grant before touching JSON or FTS cache state.
    client.require_current_administration_access()
    results: list[dict[str, Any]] = []
    use_durable_cache = get_credential_mode() != CREDENTIAL_MODE_HOSTED_REQUEST_ONLY
    # Hosted request credentials have no durable principal/grant identifier yet.
    # Keep that mode live-only so two grants to the same administration can never
    # share a process-local JSON or FTS artifact.
    index = load_sync_index(client.administration_id) if use_durable_cache else {}
    indexed_buckets = (
        "contacts",
        "sales_invoices",
        "purchase_invoices",
        "receipts",
        "general_journal_documents",
        "financial_mutations",
    )
    use_index = (
        use_durable_cache
        and index.get("administration_id") == client.administration_id
        and any(index[bucket]["records"] for bucket in indexed_buckets)
    )

    if use_index:
        capped_limit = max(1, min(limit, 20))
        # Ranked full-text match first (multi-word, any order, prefixes). The FTS
        # cache derives from the sync index and rebuilds when updated_at changes.
        if refresh_fts_index(index, client.administration_id):
            fts_results = search_fts(client.administration_id, query, capped_limit)
            if fts_results:
                return {
                    "results": fts_results,
                    "source": "sync_index_fts",
                    "updated_at": index.get("updated_at"),
                    "invoice_filter": index.get("invoice_filter"),
                    "document_filter": index.get("document_filter"),
                    "financial_mutation_filter": index.get("financial_mutation_filter"),
                }
        # Substring fallback: catches mid-word fragments FTS prefix matching cannot,
        # and any environment whose sqlite lacks FTS5.
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
                "results": results[:capped_limit],
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
        if use_durable_cache:
            response["hint"] = (
                "Some live sources were skipped. Run sync_search_index to build the "
                "local cache; search then uses the sync index instead of live scans."
            )
        else:
            response["hint"] = (
                "Some live sources were skipped. hosted_request_only mode is "
                "intentionally live-only, so local cache sync is unavailable; retry "
                "the search or use a narrower typed read tool."
            )
    return response


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def fetch(
    id: SearchRecordId,
) -> dict[str, Any]:
    """Use this when you already know a Moneybird record id from search and need the full record."""
    if ":" not in id:
        raise MoneybirdError(
            "Expected an id like contact:123, sales_invoice:123, purchase_invoice:123, receipt:123, general_journal_document:123, financial_mutation:123, ledger_account:123, or financial_account:123."
        )

    kind, record_id = id.split(":", 1)
    kind = kind.strip()
    record_id = record_id.strip()
    supported_kinds = {
        "contact",
        "sales_invoice",
        "purchase_invoice",
        "receipt",
        "general_journal_document",
        "financial_mutation",
        "ledger_account",
        "financial_account",
    }
    if kind not in supported_kinds:
        raise MoneybirdError(
            "Unsupported record kind. Use contact:<id>, sales_invoice:<id>, "
            "purchase_invoice:<id>, receipt:<id>, general_journal_document:<id>, "
            "financial_mutation:<id>, ledger_account:<id>, or financial_account:<id>."
        )
    record_id = validate_moneybird_id(record_id, f"{kind}_id")
    client = ctx.get_client()

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
def moneybird_request(
    path: GenericGetPath,
    query: Annotated[
        dict[str, Any] | None,
        Field(description="Optional query-string params, e.g. {'filter': 'state:open', 'per_page': 50}."),
    ] = None,
) -> dict[str, Any]:
    """Read-only escape hatch for allowlisted Moneybird GET endpoints.

    Performs a single GET within the configured administration. `path` is relative to the
    administration, e.g. 'estimates', 'subscriptions', 'time_entries/123',
    'documents/purchase_invoices', or 'projects'. Use 'administrations' to hit the API root.
    Put query-string params in `query`, e.g. {'filter': 'state:open', 'per_page': 50}.

    This can ONLY read. To change anything, use the matching prepare_* / *_from_approval tools.
    """
    cleaned = normalize_generic_get_path(path)
    need_admin = cleaned != "administrations"
    client = ctx.get_client(require_administration=need_admin)
    data = client.raw_get(cleaned, query=query)
    return {
        "path": cleaned,
        "result": data,
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def sync_search_index(
    invoice_filter: FilterString = "state:all,period:this_year",
    document_filter: FilterString = "period:this_year",
    financial_mutation_filter: FilterString = "period:this_year",
    force_full: Annotated[bool, Field(description="True = rebuild the index from scratch instead of an incremental refresh.")] = False,
) -> dict[str, Any]:
    """Use this when you want to build or refresh a local cached Moneybird search index."""
    client = ctx.get_client()
    return sync_search_index_data(
        client,
        invoice_filter=invoice_filter,
        document_filter=document_filter,
        financial_mutation_filter=financial_mutation_filter,
        force_full=force_full,
    )


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def search_contacts(
    query: Annotated[str, Field(description="Partial customer id, email, phone, city, or company/person name.")],
    limit: Limit = 10,
) -> dict[str, Any]:
    """Use this when you need a contact lookup by partial customer id, email, phone, city, or company/person name."""
    client = ctx.get_client()
    return {"contacts": find_contact_matches(client, query=query, limit=limit)}


