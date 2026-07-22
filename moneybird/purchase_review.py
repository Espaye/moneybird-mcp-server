"""Read-only review of purchase documents against supplier history.

This module owns history retrieval and advisory anomaly detection. It never
builds a write payload and never changes a Moneybird document. Reconciliation
uses only :func:`list_documents_for_contact` when it needs a reference invoice.
"""
from __future__ import annotations

from collections import Counter
import re
from typing import Any
import unicodedata

from .config import MoneybirdError
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


def _line_ledger(detail: dict[str, Any]) -> str:
    return str(detail.get("ledger_account_id") or "")


def _line_tax(detail: dict[str, Any]) -> str:
    return str(detail.get("tax_rate_id") or "")


def _document_order_key(document: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(document.get("date") or ""),
        str(document.get("created_at") or ""),
        str(document.get("id") or ""),
    )


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
    unfiltered list can default to the current book year. Prefer versioned sync for
    complete history, with pagination as a compatibility fallback. ``limit`` caps
    matching documents, not documents examined.
    """
    wanted_contact_id = str(contact_id or "").strip()
    if not wanted_contact_id:
        return [], {
            "pages_scanned": 0,
            "documents_examined": 0,
            "history_scan_truncated": False,
        }

    result_limit = max(1, min(int(limit), 100))
    if hasattr(client, "list_document_versions") and hasattr(
        client, "fetch_documents_by_ids"
    ):
        version_filter = f"period:{period}" if period else ""
        versions = client.list_document_versions(kind, filter=version_filter)
        ids = [str(item.get("id") or "") for item in versions if item.get("id")]
        matches: list[dict[str, Any]] = []
        examined = 0
        batches_scanned = 0
        reached_limit = False
        for id_batch in chunked(ids, 100):
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
            "history_scan_truncated": reached_limit and examined < len(ids),
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
        frozenset(_line_ledger(detail) for detail in (doc.get("details") or []))
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
                        _line_ledger(previous),
                        _line_tax(previous),
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
        current_ledger = _line_ledger(detail)
        current_tax = _line_tax(detail)
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
                f"ledger {current_ledger or '(empty)'} instead of historical {expected_ledger}"
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

    patterns = {
        contact_key: _canonical_pattern(supplier_documents)
        for contact_key, supplier_documents in by_contact.items()
    }
    flagged: list[dict[str, Any]] = []
    for doc in documents:
        contact_key = str((doc.get("contact") or {}).get("id") or "")
        pattern = patterns.get(contact_key)
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
            ledgers = frozenset(_line_ledger(detail) for detail in details)
            if ledgers != pattern["modal_ledgers"] and pattern["modal_ledgers"]:
                missing = pattern["modal_ledgers"] - ledgers
                if missing:
                    reasons.append(
                        f"missing usual ledger account(s): {', '.join(sorted(missing))}"
                    )
            if bool(doc.get("prices_are_incl_tax")) != pattern[
                "modal_prices_are_incl_tax"
            ]:
                reasons.append(
                    f"prices_are_incl_tax={bool(doc.get('prices_are_incl_tax'))}, "
                    f"supplier usually {pattern['modal_prices_are_incl_tax']}"
                )

        if include_description_mapping_checks:
            older_documents = [
                previous
                for previous in by_contact.get(contact_key, [])
                if _document_order_key(previous) < _document_order_key(doc)
            ]
            reasons.extend(_description_mapping_reasons(doc, older_documents))

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
