"""Constants, shared configuration, and explicit environment-file loading."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("moneybird_mcp")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

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


# Statuses where Moneybird answered that it refused the request outright, so the
# request it refused cannot have changed anything. A write that fails this way is
# closed as failed rather than left unresolved.
#
# 409 Conflict is deliberately absent: it can mean the record already exists,
# which is exactly the case where something may have been created already.
# Retryable statuses are absent because they mean "no answer yet", not "no".
DEFINITIVE_REJECTION_HTTP_STATUS_CODES = {
    400,  # malformed request
    401,  # unauthenticated
    403,  # forbidden
    404,  # target does not exist
    405,  # method not allowed
    406,  # not acceptable
    410,  # gone
    415,  # unsupported media type
    422,  # validation rejected (Moneybird's usual write refusal)
}


# Cap on how much of a Moneybird error body is quoted back. Enough for the field
# list of a validation failure, short enough that an unexpected body cannot flood
# a tool result or the audit log.
MAX_ERROR_DETAIL_CHARS = 800


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


# Reports Moneybird refuses for any period longer than a month
# ("Period cannot exceed 1 month"). The limit is a maximum, not a calendar
# month: 20260401..20260430 is accepted, 202604..202606 is not, and no
# parameter lifts it. A quarter has to be fetched month by month and summed.
MONTH_CAPPED_REPORTS = {
    "cash_flow",
    "tax",
    "debtors",
    "creditors",
}


# Symbolic periods that always span more than one month, so they can be
# rejected for the capped reports without resolving them against today.
MULTI_MONTH_PERIOD_SYMBOLS = {
    "this_quarter",
    "prev_quarter",
    "last_quarter",
    "this_year",
    "prev_year",
    "last_year",
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

# The guarded MCP write only exposes link types whose exact target can be
# proven by an independent financial-mutation GET after dispatch. Other API
# enum values create or fan out records whose identity is not recoverable from
# that response and therefore remain available only at the raw client layer.
VERIFIABLE_FINANCIAL_MUTATION_LINK_BOOKING_TYPES = {
    "SalesInvoice",
    "Document",
    "LedgerAccount",
}


# Valid booking_type values for unlinking a booking from a financial mutation.
FINANCIAL_MUTATION_UNLINK_BOOKING_TYPES = {"Payment", "LedgerAccountBooking"}


# Document kinds that support register_payment (general journals do not carry payments).
PAYABLE_DOCUMENT_KINDS = {"sales_invoice", "purchase_invoice", "receipt"}


LEDGER_ACCOUNT_REFERENCE_FIELDS = {"ledger_account_id", "ledger_account_name"}


DOCUMENT_POSTABLE_ACCOUNT_TYPES = {"expenses", "direct_costs", "other_income_expenses"}




class MoneybirdError(RuntimeError):
    """Raised when Moneybird rejects a request or configuration is incomplete."""


class MoneybirdHTTPError(MoneybirdError):
    """A Moneybird response that carries a status code and its reported reason.

    Two callers need more than the message text. A user (or an agent acting for
    one) needs the field-level reason Moneybird gave, because ``HTTP 422`` alone
    is not something anyone can correct. And write classification needs the
    status code, because a refusal Moneybird answered with is proof that the
    refused request changed nothing — which is the difference between a closed
    failure and an unresolved entry in the audit trail.

    Subclasses MoneybirdError so every existing ``except MoneybirdError`` keeps
    working unchanged.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        reported: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reported = reported

    @property
    def is_definitive_rejection(self) -> bool:
        """True when Moneybird answered "no", so nothing can have been applied."""
        return self.status_code in DEFINITIVE_REJECTION_HTTP_STATUS_CODES




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
    try:
        os.chmod(path, 0o700)
    except (NotImplementedError, OSError):
        # Windows ACLs and some mounted filesystems do not implement POSIX mode
        # bits. Operators must still restrict the directory ACL there.
        pass
    return path


def harden_private_file(path: Path) -> None:
    """Best-effort owner-only mode for local files containing financial data."""
    try:
        os.chmod(path, 0o600)
    except (FileNotFoundError, NotImplementedError, OSError):
        # Creation races, Windows ACL semantics, and unusual filesystems can
        # prevent chmod. Callers still rely on the containing directory ACL.
        pass




def load_env_file(path: str | Path) -> Path:
    """Load one operator-selected environment file without overriding its parent.

    Importing :mod:`moneybird` never calls this function. Runnable entrypoints may
    call it only after parsing an explicit ``--env-file PATH`` argument and before
    importing modules that consume security-sensitive configuration.
    """
    env_path = Path(path).expanduser().resolve(strict=True)
    if not env_path.is_file():
        raise MoneybirdError(f"Environment file is not a regular file: {env_path}")

    parsed_values: list[tuple[str, str]] = []
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not _ENVIRONMENT_NAME.fullmatch(key):
            raise MoneybirdError(
                f"Environment file contains an invalid variable name: {key!r}"
            )
        value = value.strip().strip('"').strip("'")
        parsed_values.append((key, value))

    for key, value in parsed_values:
        os.environ.setdefault(key, value)
    return env_path
