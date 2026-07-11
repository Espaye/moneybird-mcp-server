"""Purchase-side document reads: purchase invoices, receipts, general journal documents."""
from __future__ import annotations

from typing import Any

from ..config import (
    READ_ONLY_ANNOTATIONS,
)
from ..formatting import (
    compact_document_summary,
    compact_general_journal_summary,
)
from ._registry import mcp
from . import _context as ctx


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_purchase_invoices(
    limit: int = 10,
    page: int = 1,
    filter: str = "",
    period: str = "",
) -> dict[str, Any]:
    """Use this when you need a compact list of Moneybird purchase invoices."""
    client = ctx.get_client()
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
    client = ctx.get_client()
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
    client = ctx.get_client()
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


