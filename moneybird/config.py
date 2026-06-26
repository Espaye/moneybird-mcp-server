"""Constants, shared configuration, the MoneybirdError type, and .env loading."""
from __future__ import annotations

import os
from pathlib import Path

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("moneybird_mcp")

BASE_URL = "https://moneybird.com/api/v2"


DEFAULT_TIMEOUT_SECONDS = 20


APPROVAL_TTL_MINUTES = 15


DEFAULT_RETRY_ATTEMPTS = 4


DEFAULT_RETRY_BACKOFF_SECONDS = 1.5


RETRYABLE_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


WRITE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}


PREPARE_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}


MERGE_FIELD_LABELS = {
    "contact_id": "contact",
    "scheduled_send_on": "scheduled_send_on",
    "workflow_id": "workflow",
    "document_style_id": "document_style",
    "identity_id": "identity",
    "currency": "currency",
    "prices_are_incl_tax": "prices_are_incl_tax",
    "discount": "discount",
    "extra_fields": "extra_fields",
}


DOCUMENT_KIND_CONFIG: dict[str, dict[str, str]] = {
    "purchase_invoice": {
        "collection_path": "documents/purchase_invoices",
        "collection_name": "purchase_invoices",
        "record_key": "purchase_invoice",
        "id_prefix": "purchase_invoice",
        "label": "purchase invoice",
    },
    "receipt": {
        "collection_path": "documents/receipts",
        "collection_name": "receipts",
        "record_key": "receipt",
        "id_prefix": "receipt",
        "label": "receipt",
    },
    "general_journal_document": {
        "collection_path": "documents/general_journal_documents",
        "collection_name": "general_journal_documents",
        "record_key": "general_journal_document",
        "id_prefix": "general_journal_document",
        "label": "general journal document",
    },
}


DOCUMENT_KIND_ALIASES = {
    "purchase_invoice": "purchase_invoice",
    "purchase_invoices": "purchase_invoice",
    "receipt": "receipt",
    "receipts": "receipt",
    "general_journal_document": "general_journal_document",
    "general_journal_documents": "general_journal_document",
}


REPORT_ENDPOINTS = {
    "profit_loss": "profit_loss",
    "balance_sheet": "balance_sheet",
    "general_ledger": "general_ledger",
}


LEDGER_ACCOUNT_REFERENCE_FIELDS = {"ledger_account_id", "ledger_account_name"}


DOCUMENT_POSTABLE_ACCOUNT_TYPES = {"expenses", "direct_costs", "other_income_expenses"}




class MoneybirdError(RuntimeError):
    """Raised when Moneybird rejects a request or configuration is incomplete."""




def load_local_env() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_local_env()
