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


# Upper bound on any single retry sleep. Moneybird's Retry-After header is
# sometimes an absolute epoch timestamp rather than delta-seconds; without a cap
# that would make the client sleep for decades. 60s is plenty for a 429/503.
MAX_RETRY_DELAY_SECONDS = 60.0


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
    "language": "language",
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
    "cash_flow": "cash_flow",
    "tax": "tax",
    "debtors": "debtors",
    "debtors_aging": "debtors_aging",
    "creditors": "creditors",
    "creditors_aging": "creditors_aging",
    "revenue_by_contact": "revenue_by_contact",
    "revenue_by_project": "revenue_by_project",
    "expenses_by_contact": "expenses_by_contact",
    "expenses_by_project": "expenses_by_project",
    "journal_entries": "journal_entries",
    "subscriptions": "subscriptions",
    "assets": "assets",
}


# The aging reports take a reference date (period_until) instead of a period range.
REPORT_PERIOD_PARAM_OVERRIDES = {
    "debtors_aging": "period_until",
    "creditors_aging": "period_until",
}


# Reports that support page/per_page pagination.
PAGINATED_REPORTS = {
    "debtors",
    "debtors_aging",
    "creditors",
    "creditors_aging",
    "revenue_by_contact",
    "revenue_by_project",
    "expenses_by_contact",
    "expenses_by_project",
    "journal_entries",
}


# Valid booking_type values for linking a financial mutation to a booking.
FINANCIAL_MUTATION_LINK_BOOKING_TYPES = {
    "SalesInvoice",
    "Document",
    "LedgerAccount",
    "PaymentTransactionBatch",
    "PurchaseTransaction",
    "NewPurchaseInvoice",
    "NewReceipt",
    "PaymentTransaction",
    "PurchaseTransactionBatch",
    "ExternalSalesInvoice",
    "Payment",
    "VatDocument",
}


# Valid booking_type values for unlinking a booking from a financial mutation.
FINANCIAL_MUTATION_UNLINK_BOOKING_TYPES = {"Payment", "LedgerAccountBooking"}


# Document kinds that support register_payment (general journals do not carry payments).
PAYABLE_DOCUMENT_KINDS = {"sales_invoice", "purchase_invoice", "receipt"}


LEDGER_ACCOUNT_REFERENCE_FIELDS = {"ledger_account_id", "ledger_account_name"}


DOCUMENT_POSTABLE_ACCOUNT_TYPES = {"expenses", "direct_costs", "other_income_expenses"}




class MoneybirdError(RuntimeError):
    """Raised when Moneybird rejects a request or configuration is incomplete."""




def data_dir() -> Path:
    """Directory for server state (approvals DB, audit logs, sync caches).

    Defaults to the current working directory for backward compatibility with
    existing deployments; set ``MONEYBIRD_MCP_DATA_DIR`` to move state out of
    the repo/cwd (recommended for anything beyond local development). Read at
    call time, not import time, so tests and long-running processes can
    redirect state without re-importing.
    """
    override = os.environ.get("MONEYBIRD_MCP_DATA_DIR", "").strip()
    if not override:
        return Path(".")
    path = Path(override).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path




def _apply_env_file(env_path: Path) -> None:
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_local_env() -> None:
    """Load .env into the environment so scripts/tests work without manual export.

    Looks first in the current working directory, then alongside the package
    (the project root). This makes the loader independent of where Python is
    invoked from: a one-off script no longer has to be run from the repo root,
    and importing ``moneybird`` is enough to pick up credentials. Existing
    environment variables (e.g. per-request headers, CI secrets) always win
    because values are applied with ``setdefault``.
    """
    seen: set[Path] = set()
    candidates = [
        Path(".env"),
        Path(__file__).resolve().parent.parent / ".env",
    ]
    for env_path in candidates:
        resolved = env_path.resolve()
        if resolved in seen or not env_path.exists():
            continue
        seen.add(resolved)
        _apply_env_file(env_path)


load_local_env()
