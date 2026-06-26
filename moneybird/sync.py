"""Local search-index sync (versioned buckets cached on disk)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import MoneybirdError
from .client import MoneybirdClient
from .formatting import (
    chunked,
    contact_search_record,
    document_kind_config,
    document_search_record,
    financial_mutation_search_record,
    general_journal_search_record,
    iso_now,
    normalize_document_kind,
    sales_invoice_search_record,
)

SYNC_INDEX_PATH = Path(".moneybird_sync_index.json")



def ensure_sync_index_shape(index: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(index)
    normalized.setdefault("administration_id", None)
    normalized.setdefault("updated_at", None)
    normalized.setdefault("invoice_filter", "")
    normalized.setdefault("document_filter", "")
    normalized.setdefault("financial_mutation_filter", "")
    for bucket in (
        "contacts",
        "sales_invoices",
        "purchase_invoices",
        "receipts",
        "general_journal_documents",
        "financial_mutations",
    ):
        bucket_value = normalized.setdefault(bucket, {})
        bucket_value.setdefault("versions", {})
        bucket_value.setdefault("records", {})
    return normalized




def document_sync_record(
    kind: str,
    record: dict[str, Any],
    administration_id: str | None,
) -> dict[str, Any]:
    normalized_kind = normalize_document_kind(kind)
    if normalized_kind == "general_journal_document":
        return general_journal_search_record(record, administration_id)
    return document_search_record(normalized_kind, record, administration_id)




def load_sync_index() -> dict[str, Any]:
    if not SYNC_INDEX_PATH.exists():
        return ensure_sync_index_shape({})

    return ensure_sync_index_shape(json.loads(SYNC_INDEX_PATH.read_text(encoding="utf-8")))




def save_sync_index(index: dict[str, Any]) -> None:
    index = ensure_sync_index_shape(index)
    SYNC_INDEX_PATH.write_text(
        json.dumps(index, indent=2, ensure_ascii=True, sort_keys=True),
        encoding="utf-8",
    )




def update_versioned_sync_bucket(
    *,
    stored_versions: dict[str, Any],
    stored_records: dict[str, Any],
    current_versions: dict[str, int],
    fetch_many: Any,
    fetch_one: Any,
    record_builder: Any,
) -> dict[str, int]:
    removed_ids = set(stored_versions) - set(current_versions)
    for removed_id in removed_ids:
        stored_versions.pop(removed_id, None)
        stored_records.pop(removed_id, None)

    changed_ids = [
        item_id
        for item_id, version in current_versions.items()
        if stored_versions.get(item_id) != version
    ]

    for group in chunked(changed_ids, 100):
        try:
            items = fetch_many(group)
        except MoneybirdError as exc:
            if "HTTP 403" not in str(exc):
                raise
            items = [fetch_one(item_id) for item_id in group]

        for item in items:
            item_id = str(item["id"])
            stored_versions[item_id] = int(item["version"])
            stored_records[item_id] = record_builder(item)

    return {
        "changed": len(changed_ids),
        "removed": len(removed_ids),
        "total": len(current_versions),
    }




def update_contact_sync_index(index: dict[str, Any], client: MoneybirdClient) -> dict[str, int]:
    versions = client.list_contact_versions()
    current_versions = {str(item["id"]): int(item["version"]) for item in versions}
    stored_versions = index["contacts"]["versions"]
    stored_records = index["contacts"]["records"]
    return update_versioned_sync_bucket(
        stored_versions=stored_versions,
        stored_records=stored_records,
        current_versions=current_versions,
        fetch_many=client.fetch_contacts_by_ids,
        fetch_one=client.get_contact,
        record_builder=lambda contact: contact_search_record(contact, client.administration_id),
    )




def update_sales_invoice_sync_index(
    index: dict[str, Any],
    client: MoneybirdClient,
    *,
    invoice_filter: str,
) -> dict[str, int]:
    versions = client.list_sales_invoice_versions(filter=invoice_filter)
    current_versions = {str(item["id"]): int(item["version"]) for item in versions}
    stored_versions = index["sales_invoices"]["versions"]
    stored_records = index["sales_invoices"]["records"]
    stats = update_versioned_sync_bucket(
        stored_versions=stored_versions,
        stored_records=stored_records,
        current_versions=current_versions,
        fetch_many=client.fetch_sales_invoices_by_ids,
        fetch_one=client.get_sales_invoice,
        record_builder=lambda invoice: sales_invoice_search_record(
            invoice,
            client.administration_id,
        ),
    )
    index["invoice_filter"] = invoice_filter
    return stats




def update_document_sync_index(
    index: dict[str, Any],
    client: MoneybirdClient,
    *,
    kind: str,
    document_filter: str,
) -> dict[str, int]:
    normalized_kind = normalize_document_kind(kind)
    bucket_name = document_kind_config(normalized_kind)["collection_name"]
    versions = client.list_document_versions(normalized_kind, filter=document_filter)
    current_versions = {str(item["id"]): int(item["version"]) for item in versions}
    stored_versions = index[bucket_name]["versions"]
    stored_records = index[bucket_name]["records"]
    return update_versioned_sync_bucket(
        stored_versions=stored_versions,
        stored_records=stored_records,
        current_versions=current_versions,
        fetch_many=lambda ids: client.fetch_documents_by_ids(normalized_kind, ids),
        fetch_one=lambda item_id: client.get_document(normalized_kind, item_id),
        record_builder=lambda record: document_sync_record(
            normalized_kind,
            record,
            client.administration_id,
        ),
    )




def update_financial_mutation_sync_index(
    index: dict[str, Any],
    client: MoneybirdClient,
    *,
    mutation_filter: str,
) -> dict[str, int]:
    versions = client.list_financial_mutation_versions(filter=mutation_filter)
    current_versions = {str(item["id"]): int(item["version"]) for item in versions}
    stored_versions = index["financial_mutations"]["versions"]
    stored_records = index["financial_mutations"]["records"]
    return update_versioned_sync_bucket(
        stored_versions=stored_versions,
        stored_records=stored_records,
        current_versions=current_versions,
        fetch_many=client.fetch_financial_mutations_by_ids,
        fetch_one=client.get_financial_mutation,
        record_builder=lambda mutation: financial_mutation_search_record(
            mutation,
            client.administration_id,
        ),
    )




def sync_search_index_data(
    client: MoneybirdClient,
    *,
    invoice_filter: str = "state:all,period:this_year",
    document_filter: str = "period:this_year",
    financial_mutation_filter: str = "period:this_year",
    force_full: bool = False,
) -> dict[str, Any]:
    index = load_sync_index()
    if force_full or index.get("administration_id") != client.administration_id:
        index = ensure_sync_index_shape({"administration_id": client.administration_id})

    contact_stats = update_contact_sync_index(index, client)
    invoice_stats = update_sales_invoice_sync_index(
        index,
        client,
        invoice_filter=invoice_filter,
    )
    purchase_invoice_stats = update_document_sync_index(
        index,
        client,
        kind="purchase_invoice",
        document_filter=document_filter,
    )
    receipt_stats = update_document_sync_index(
        index,
        client,
        kind="receipt",
        document_filter=document_filter,
    )
    general_journal_stats = update_document_sync_index(
        index,
        client,
        kind="general_journal_document",
        document_filter=document_filter,
    )
    financial_mutation_stats = update_financial_mutation_sync_index(
        index,
        client,
        mutation_filter=financial_mutation_filter,
    )
    index["administration_id"] = client.administration_id
    index["updated_at"] = iso_now()
    index["document_filter"] = document_filter
    index["financial_mutation_filter"] = financial_mutation_filter
    save_sync_index(index)

    return {
        "updated_at": index["updated_at"],
        "contacts": contact_stats,
        "sales_invoices": invoice_stats,
        "purchase_invoices": purchase_invoice_stats,
        "receipts": receipt_stats,
        "general_journal_documents": general_journal_stats,
        "financial_mutations": financial_mutation_stats,
        "invoice_filter": invoice_filter,
        "document_filter": document_filter,
        "financial_mutation_filter": financial_mutation_filter,
        "path": str(SYNC_INDEX_PATH.resolve()),
    }
