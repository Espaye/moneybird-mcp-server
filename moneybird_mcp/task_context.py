"""Task-scoped Moneybird snapshot and batch loaders.

This cache intentionally lives only for one prepare/execute tool invocation.  It
deduplicates reads without letting stale state leak into a later approval or a
different administration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .config import MoneybirdError
from .formatting import chunked


def _unique_ids(ids: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in ids if str(item).strip()))


@dataclass
class MoneybirdTaskContext:
    client: Any
    _ledger_accounts: list[dict[str, Any]] | None = None
    _tax_rates: list[dict[str, Any]] | None = None
    _financial_mutations: dict[str, dict[str, Any]] = field(default_factory=dict)
    _documents: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    _sales_invoices: dict[str, dict[str, Any]] = field(default_factory=dict)

    def ledger_accounts(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        if refresh or self._ledger_accounts is None:
            self._ledger_accounts = list(self.client.list_ledger_accounts())
        return self._ledger_accounts

    def tax_rates(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        if refresh or self._tax_rates is None:
            self._tax_rates = list(self.client.list_tax_rates())
        return self._tax_rates

    def financial_mutations(
        self,
        ids: Iterable[str],
        *,
        refresh: bool = False,
    ) -> dict[str, dict[str, Any]]:
        requested = _unique_ids(ids)
        missing = (
            requested
            if refresh
            else [item_id for item_id in requested if item_id not in self._financial_mutations]
        )
        if missing:
            self._financial_mutations.update(
                self._fetch_many(
                    missing,
                    fetch_many=getattr(
                        self.client,
                        "fetch_financial_mutations_by_ids",
                        None,
                    ),
                    fetch_one=self.client.get_financial_mutation,
                )
            )
        return {
            item_id: self._financial_mutations[item_id]
            for item_id in requested
            if item_id in self._financial_mutations
        }

    def documents(
        self,
        kind: str,
        ids: Iterable[str],
        *,
        refresh: bool = False,
    ) -> dict[str, dict[str, Any]]:
        normalized_kind = str(kind).strip()
        requested = _unique_ids(ids)
        missing = [
            item_id
            for item_id in requested
            if refresh or (normalized_kind, item_id) not in self._documents
        ]
        if missing:
            fetched = self._fetch_many(
                missing,
                fetch_many=(
                    (
                        lambda group: self.client.fetch_documents_by_ids(
                            normalized_kind,
                            group,
                        )
                    )
                    if hasattr(self.client, "fetch_documents_by_ids")
                    else None
                ),
                fetch_one=lambda item_id: self.client.get_document(
                    normalized_kind,
                    item_id,
                ),
            )
            for item_id, record in fetched.items():
                self._documents[(normalized_kind, item_id)] = record
        return {
            item_id: self._documents[(normalized_kind, item_id)]
            for item_id in requested
            if (normalized_kind, item_id) in self._documents
        }

    def sales_invoices(
        self,
        ids: Iterable[str],
        *,
        refresh: bool = False,
    ) -> dict[str, dict[str, Any]]:
        requested = _unique_ids(ids)
        missing = (
            requested
            if refresh
            else [item_id for item_id in requested if item_id not in self._sales_invoices]
        )
        if missing:
            self._sales_invoices.update(
                self._fetch_many(
                    missing,
                    fetch_many=getattr(
                        self.client,
                        "fetch_sales_invoices_by_ids",
                        None,
                    ),
                    fetch_one=self.client.get_sales_invoice,
                )
            )
        return {
            item_id: self._sales_invoices[item_id]
            for item_id in requested
            if item_id in self._sales_invoices
        }

    @staticmethod
    def _fetch_many(
        ids: list[str],
        *,
        fetch_many: Any,
        fetch_one: Any,
    ) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        if callable(fetch_many):
            for group in chunked(ids, 100):
                for record in fetch_many(group):
                    item_id = str(record.get("id") or "")
                    if item_id:
                        records[item_id] = record
        else:
            for item_id in ids:
                record = fetch_one(item_id)
                records[str(record.get("id") or item_id)] = record

        unresolved = [item_id for item_id in ids if item_id not in records]
        # Some synchronization endpoints can omit inaccessible/deleted ids.
        # Fetch individually to distinguish that case and preserve the existing
        # precise Moneybird error.
        for item_id in unresolved:
            record = fetch_one(item_id)
            records[str(record.get("id") or item_id)] = record
        still_missing = [item_id for item_id in ids if item_id not in records]
        if still_missing:
            raise MoneybirdError(
                "Moneybird did not return requested record(s): "
                + ", ".join(still_missing)
            )
        return records
