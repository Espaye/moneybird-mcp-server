"""Read-only review of purchase documents against supplier history.

This module owns history retrieval and advisory anomaly detection. It never
builds a write payload and never changes a Moneybird document. Reconciliation
uses only :func:`list_documents_for_contact` when it needs a reference invoice.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Any

from . import rate_budget
from .config import MoneybirdError
from .document_lines import line_ledger_account_id, line_tax_rate_id
from .formatting import chunked, normalize_document_kind

_SUPPORTED_KINDS = {"purchase_invoice", "receipt"}
_DESCRIPTION_STOPWORDS = {
    "aan",
    "de",
    "een",
    "en",
    "factuur",
    "het",
    "in",
    "januari",
    "februari",
    "maart",
    "april",
    "mei",
    "juni",
    "juli",
    "augustus",
    "september",
    "oktober",
    "november",
    "december",
    "met",
    "nota",
    "op",
    "termijnnota",
    "van",
    "voor",
}


def _ledger_label(
    ledger_account_id: str,
    ledger_accounts: dict[str, dict[str, Any]],
) -> str:
    account = ledger_accounts.get(str(ledger_account_id))
    if not account:
        return str(ledger_account_id) or "(empty)"
    label = " ".join(
        part
        for part in (
            str(account.get("account_id") or "").strip(),
            str(account.get("name") or "").strip(),
        )
        if part
    )
    return label or str(ledger_account_id)


def _document_order_key(document: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(document.get("date") or ""),
        str(document.get("created_at") or ""),
        str(document.get("id") or ""),
    )


def _indexed_document_ids_for_contact(
    client: Any,
    kind: str,
    *,
    contact_id: str,
) -> list[str] | None:
    """Document ids for one contact from the local sync index, newest first.

    Returns ``None`` — meaning "the index cannot answer this, scan instead" — when
    the index is absent, belongs to another administration, predates the stored
    ``contact_id`` facet, or has no record of this contact at all. That last case
    matters: an empty *list* would wrongly assert the supplier has no history.
    """
    from .config import DOCUMENT_KIND_CONFIG
    from .credentials import (
        CREDENTIAL_MODE_HOSTED_REQUEST_ONLY,
        get_credential_mode,
    )
    from .sync import RECORD_SCHEMA_VERSION, load_sync_index

    if get_credential_mode() == CREDENTIAL_MODE_HOSTED_REQUEST_ONLY:
        return None
    administration_id = getattr(client, "administration_id", None)
    if not administration_id:
        return None
    bucket = (DOCUMENT_KIND_CONFIG.get(kind) or {}).get("collection_name")
    if not bucket:
        return None
    try:
        index = load_sync_index(administration_id)
    except (OSError, ValueError):
        return None
    if (
        str(index.get("administration_id") or "") != str(administration_id)
        or index.get("record_schema_version") != RECORD_SCHEMA_VERSION
    ):
        return None
    records = (index.get(bucket) or {}).get("records") or {}
    if not records:
        return None
    matches = [
        record
        for record in records.values()
        if str(record.get("contact_id") or "") == contact_id
    ]
    if not matches:
        return None
    matches.sort(key=lambda record: str(record.get("date") or ""), reverse=True)
    return [
        str(record.get("id") or "").split(":", 1)[-1]
        for record in matches
        if record.get("id")
    ]


def list_documents_for_contact(
    client: Any,
    kind: str,
    *,
    contact_id: str,
    period: str = "",
    limit: int = 100,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return bounded supplier history plus metadata describing its coverage.

    Moneybird's purchase-document endpoint has no reliable contact filter and its
    unfiltered list can default to the current book year, so finding one
    supplier's invoices otherwise means fetching every document and filtering
    client-side. Strategies, cheapest first:

    1. the local sync index, which stores each document's ``contact_id`` and
       therefore names the exact ids to fetch (one request instead of one per
       hundred documents in the whole administration);
    2. the versioned sync feed, scanned newest-first;
    3. plain pagination, for a client without the sync endpoints.

    ``limit`` caps matching documents, not documents examined.
    """
    wanted_contact_id = str(contact_id or "").strip()
    if not wanted_contact_id:
        return [], {
            "pages_scanned": 0,
            "documents_examined": 0,
            "history_scan_truncated": False,
        }

    result_limit = max(1, min(int(limit), 100))

    indexed_ids = _indexed_document_ids_for_contact(
        client,
        kind,
        contact_id=wanted_contact_id,
    )
    if indexed_ids is not None and hasattr(client, "fetch_documents_by_ids"):
        documents: list[dict[str, Any]] = []
        for id_batch in chunked(indexed_ids[: result_limit * 2], 100):
            documents.extend(client.fetch_documents_by_ids(kind, id_batch))
        # The index is a point-in-time snapshot, so re-filter what came back
        # rather than trusting it: a document may have been reassigned to another
        # contact since the last sync.
        documents = [
            document
            for document in documents
            if str(
                (document.get("contact") or {}).get("id")
                or document.get("contact_id")
                or ""
            )
            == wanted_contact_id
        ]
        documents.sort(key=_document_order_key, reverse=True)
        return documents[:result_limit], {
            "history_source": "sync_index",
            "pages_scanned": max(1, (len(indexed_ids) + 99) // 100),
            "documents_examined": len(documents),
            "history_scan_truncated": len(indexed_ids) > result_limit * 2,
        }

    if hasattr(client, "list_document_versions") and hasattr(
        client, "fetch_documents_by_ids"
    ):
        version_filter = f"period:{period}" if period else ""
        versions = client.list_document_versions(kind, filter=version_filter)
        ids = [str(item.get("id") or "") for item in versions if item.get("id")]
        # Newest ids last in Moneybird's version feed, and supplier history is
        # only ever read newest-first. Scanning from the end finds a supplier's
        # recent invoices in the first batch or two instead of paging through
        # every document in the administration to reach them.
        ids.reverse()
        matches: list[dict[str, Any]] = []
        examined = 0
        batches_scanned = 0
        reached_limit = False
        budget_exhausted = False
        for id_batch in chunked(ids, 100):
            # Each batch is one request against a 150-per-5-minutes per-IP budget.
            # An unfiltered scan of a large administration can consume most of it,
            # so stop and say so rather than starving the rest of the task.
            if (
                batches_scanned
                and rate_budget.affordable_batches("general") == 0
            ):
                budget_exhausted = True
                break
            batch = client.fetch_documents_by_ids(kind, id_batch)
            batches_scanned += 1
            examined += len(batch)
            matches.extend(
                document
                for document in batch
                if str(
                    (document.get("contact") or {}).get("id")
                    or document.get("contact_id")
                    or ""
                )
                == wanted_contact_id
            )
            if len(matches) >= result_limit:
                reached_limit = True
                break

        matches.sort(key=_document_order_key, reverse=True)
        return matches[:result_limit], {
            "history_source": "synchronization",
            "pages_scanned": batches_scanned,
            "documents_examined": examined,
            "history_scan_truncated": (
                budget_exhausted or (reached_limit and examined < len(ids))
            ),
            "history_scan_stopped_for_rate_budget": budget_exhausted,
        }

    page_size = 100
    page = 1
    matches: list[dict[str, Any]] = []
    examined = 0
    prior_page_ids: tuple[str, ...] | None = None
    truncated = False

    while len(matches) < result_limit:
        batch = client.list_documents(
            kind,
            limit=page_size,
            page=page,
            period=period,
        )
        if not batch:
            break

        page_ids = tuple(str(document.get("id") or "") for document in batch)
        if page_ids == prior_page_ids:
            truncated = True
            break
        prior_page_ids = page_ids
        examined += len(batch)
        matches.extend(
            document
            for document in batch
            if str(
                (document.get("contact") or {}).get("id")
                or document.get("contact_id")
                or ""
            )
            == wanted_contact_id
        )

        if len(batch) < page_size:
            break
        page += 1

    reached_limit = len(matches) >= result_limit
    return matches[:result_limit], {
        "history_source": "paginated_list_fallback",
        "pages_scanned": page,
        "documents_examined": examined,
        "history_scan_truncated": truncated or reached_limit,
    }


def _canonical_pattern(documents: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the modal booking pattern, or ``None`` for insufficient history."""
    if len(documents) < 2:
        return None

    line_counts = Counter(len(doc.get("details") or []) for doc in documents)
    ledger_sets = Counter(
        frozenset(line_ledger_account_id(detail) for detail in (doc.get("details") or []))
        for doc in documents
    )
    incl_flags = Counter(bool(doc.get("prices_are_incl_tax")) for doc in documents)
    modal_line_count = line_counts.most_common(1)[0][0]
    example = next(
        (
            doc
            for doc in sorted(documents, key=_document_order_key, reverse=True)
            if len(doc.get("details") or []) == modal_line_count
        ),
        None,
    )
    return {
        "modal_line_count": modal_line_count,
        "modal_ledgers": ledger_sets.most_common(1)[0][0],
        "modal_prices_are_incl_tax": incl_flags.most_common(1)[0][0],
        "example_document_id": str(example.get("id")) if example else "",
    }


def _description_tokens(description: Any) -> frozenset[str]:
    text = unicodedata.normalize("NFKD", str(description or ""))
    ascii_text = text.encode("ascii", "ignore").decode("ascii").casefold()
    return frozenset(
        token
        for token in re.findall(r"[a-z]+", ascii_text)
        if token not in _DESCRIPTION_STOPWORDS and len(token) > 1
    )


def _description_mapping_reasons(
    document: dict[str, Any],
    older_documents: list[dict[str, Any]],
    ledger_accounts: dict[str, dict[str, Any]],
) -> list[str]:
    """Advise when a familiar description has a different ledger or tax mapping."""
    historical_lines = [
        line for older in older_documents for line in (older.get("details") or [])
    ]
    reasons: list[str] = []

    for detail in document.get("details") or []:
        tokens = _description_tokens(detail.get("description"))
        if len(tokens) < 2:
            continue

        similar_mappings: list[tuple[str, str, str]] = []
        for previous in historical_lines:
            previous_tokens = _description_tokens(previous.get("description"))
            if len(previous_tokens) < 2:
                continue
            overlap_count = len(tokens & previous_tokens)
            overlap = overlap_count / min(len(tokens), len(previous_tokens))
            if overlap_count >= 2 and overlap >= 0.60:
                similar_mappings.append(
                    (
                        line_ledger_account_id(previous),
                        line_tax_rate_id(previous),
                        str(previous.get("description") or ""),
                    )
                )

        if not similar_mappings:
            continue

        mapping_counts = Counter(
            (ledger, tax) for ledger, tax, _description in similar_mappings
        )
        ranked = mapping_counts.most_common()
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            continue
        (expected_ledger, expected_tax), _count = ranked[0]
        current_ledger = line_ledger_account_id(detail)
        current_tax = line_tax_rate_id(detail)
        if (current_ledger, current_tax) == (expected_ledger, expected_tax):
            continue

        example = next(
            description
            for ledger, tax, description in similar_mappings
            if (ledger, tax) == (expected_ledger, expected_tax)
        )
        mismatches: list[str] = []
        if current_ledger != expected_ledger:
            mismatches.append(
                f"ledger {_ledger_label(current_ledger, ledger_accounts)} instead "
                f"of historical {_ledger_label(expected_ledger, ledger_accounts)}"
            )
        if current_tax != expected_tax:
            mismatches.append(
                f"tax {current_tax or '(empty)'} instead of historical {expected_tax}"
            )
        reasons.append(
            f"line '{str(detail.get('description') or '')}' resembles historical "
            f"'{example}' but uses {' and '.join(mismatches)}"
        )

    return reasons


def scan_purchase_invoices_for_attention(
    client: Any,
    *,
    kind: str = "purchase_invoice",
    period: str = "",
    limit: int = 100,
    contact_id: str = "",
    include_description_mapping_checks: bool = True,
) -> dict[str, Any]:
    """Flag documents that are new or deviate from their supplier's pattern.

    This operation is read-only. Description similarity is advisory and can be
    disabled independently from deterministic state and pattern checks.
    """
    normalized_kind = normalize_document_kind(kind)
    if normalized_kind not in _SUPPORTED_KINDS:
        raise MoneybirdError(
            "Attention scan supports purchase_invoice and receipt documents only."
        )

    if contact_id:
        documents, scan_metadata = list_documents_for_contact(
            client,
            normalized_kind,
            contact_id=contact_id,
            period=period,
            limit=limit,
        )
    else:
        documents = client.list_documents(
            normalized_kind,
            limit=limit,
            page=1,
            period=period,
        )
        scan_metadata = {
            "history_source": "single_list_page",
            "pages_scanned": 1,
            "documents_examined": len(documents),
            "history_scan_truncated": len(documents) >= limit,
        }

    if any("details" not in doc for doc in documents):
        documents = [
            doc
            if "details" in doc
            else client.get_document(normalized_kind, str(doc.get("id")))
            for doc in documents
        ]

    by_contact: dict[str, list[dict[str, Any]]] = {}
    for doc in documents:
        contact_key = str((doc.get("contact") or {}).get("id") or "")
        by_contact.setdefault(contact_key, []).append(doc)

    ledger_accounts = {
        str(account.get("id") or ""): account
        for account in client.list_ledger_accounts()
        if account.get("id")
    }
    flagged: list[dict[str, Any]] = []
    for doc in documents:
        contact_key = str((doc.get("contact") or {}).get("id") or "")
        # "Usually" requires at least two genuinely historical documents. The
        # invoice under review must never vote for the pattern used to judge it.
        older_documents = [
            previous
            for previous in by_contact.get(contact_key, [])
            if _document_order_key(previous) < _document_order_key(doc)
        ]
        pattern = _canonical_pattern(older_documents)
        details = doc.get("details") or []
        reasons: list[str] = []

        if str(doc.get("state") or "") == "new":
            reasons.append("state is 'new' (not booked yet)")

        if pattern is not None:
            if pattern["modal_line_count"] > 1 and len(details) < pattern["modal_line_count"]:
                reasons.append(
                    f"has {len(details)} line(s); this supplier usually has "
                    f"{pattern['modal_line_count']}"
                )
            ledgers = frozenset(line_ledger_account_id(detail) for detail in details)
            if ledgers != pattern["modal_ledgers"] and pattern["modal_ledgers"]:
                missing = pattern["modal_ledgers"] - ledgers
                if missing:
                    reasons.append(
                        "missing usual ledger account(s): "
                        + ", ".join(
                            _ledger_label(ledger_id, ledger_accounts)
                            for ledger_id in sorted(missing)
                        )
                    )
            if bool(doc.get("prices_are_incl_tax")) != pattern[
                "modal_prices_are_incl_tax"
            ]:
                reasons.append(
                    f"prices_are_incl_tax={bool(doc.get('prices_are_incl_tax'))}, "
                    f"supplier usually {pattern['modal_prices_are_incl_tax']}"
                )

        if include_description_mapping_checks:
            reasons.extend(
                _description_mapping_reasons(
                    doc,
                    older_documents,
                    ledger_accounts,
                )
            )

        if not reasons:
            continue

        suggestion = ""
        if (
            pattern
            and pattern["example_document_id"]
            and pattern["example_document_id"] != str(doc.get("id"))
        ):
            suggestion = pattern["example_document_id"]

        flagged.append(
            {
                "document_id": str(doc.get("id")),
                "document_kind": normalized_kind,
                "date": doc.get("date"),
                "reference": doc.get("reference"),
                "contact": (doc.get("contact") or {}).get("company_name")
                or (doc.get("contact") or {}).get("name"),
                "state": doc.get("state"),
                "total_price_incl_tax": doc.get("total_price_incl_tax"),
                "line_count": len(details),
                "reasons": reasons,
                "suggested_reference_document_id": suggestion,
            }
        )

    flagged.sort(key=lambda item: str(item.get("date") or ""), reverse=True)
    return {
        "flagged": flagged,
        "count": len(flagged),
        "scanned": len(documents),
        "period": period,
        "description_mapping_checks_included": include_description_mapping_checks,
        **scan_metadata,
    }
