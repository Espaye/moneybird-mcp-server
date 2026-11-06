"""The supported surface for applications that embed this package's client.

An embedder is a service that talks to Moneybird through this package's client,
OAuth and formatting helpers but never serves this package's MCP tools: it has
its own request handling, approval flow and write path. It is a different
consumer from an extension (see :mod:`moneybird_mcp.api`), which adds tools to
this server and so runs inside its approval kernel.

Importing this module loads no tools. That is the property an embedder relies
on: importing :mod:`moneybird_mcp.tools` runs the extension loader, which
registers every installed extension's write specs and approval executors in the
importing process, and an embedder must not end up with a second write path it
does not control. ``tests/test_embed_boundary.py`` proves the property in a
fresh interpreter.

What an embedder needs from the client:

- :class:`MoneybirdClient` takes ``http_client`` and ``retry_attempts``. Pass a
  wrapper around :func:`get_shared_http_client` to observe or restrict every
  JSON API request, and ``retry_attempts=0`` to own the retry policy, instead
  of replacing module globals.
- :meth:`MoneybirdClient.request` sends one request confined to the client's
  administration. It is the supported replacement for the private
  ``_request``.

Everything in :data:`__all__` is public API under semantic versioning, the
same as :mod:`moneybird_mcp.api`. :data:`EMBED_API_VERSION` increases when a
name is removed or changes meaning; adding a name does not change it, so an
embedder that needs a newer name declares the package version that added it.
``tests/test_embed_boundary.py`` pins the list.
"""
from __future__ import annotations

from .capabilities import CapabilityMode, capability_mode, require_write_capability
from .client import MoneybirdClient, validate_moneybird_id
from .config import (
    BASE_URL,
    MONTH_CAPPED_REPORTS,
    UNPAID_DOCUMENT_STATES,
    UNPAID_SALES_INVOICE_STATES,
    MoneybirdError,
    MoneybirdHTTPError,
)
from .credentials import (
    CREDENTIAL_MODE_HOSTED_REQUEST_ONLY,
    get_credential_mode,
    resolve_credentials,
)
from .formatting import (
    MAX_BANK_DESCRIPTION_CHARS,
    MONEYBIRD_DOCUMENTED_DEFAULT_PERIODS,
    bank_description,
    build_filter_string,
    compact_document_summary,
    compact_ledger_account_summary,
    contact_invoice_email,
    describe_effective_period,
    document_contact_title,
    matches_query,
    normalize_list_period,
    normalize_text,
    report_period_months,
)
from .http_transport import HTTPClient, get_shared_http_client
from .oauth import (
    build_authorize_url,
    exchange_authorization_code,
    parse_authorization_callback,
    refresh_access_token,
)
from .oauth_scopes import CAPABILITY_SCOPES, INCIDENTAL_ACCESS, missing_scopes
from .oauth_store import OAuthConnection
from .telemetry import normalize_endpoint
from .vat_settlement import month_periods
from .write_contracts import build_patch_precondition, compare_controlled_fields

EMBED_API_VERSION = 1

__all__ = [
    "BASE_URL",
    "CAPABILITY_SCOPES",
    "CREDENTIAL_MODE_HOSTED_REQUEST_ONLY",
    "EMBED_API_VERSION",
    "HTTPClient",
    "INCIDENTAL_ACCESS",
    "MAX_BANK_DESCRIPTION_CHARS",
    "MONEYBIRD_DOCUMENTED_DEFAULT_PERIODS",
    "MONTH_CAPPED_REPORTS",
    "UNPAID_DOCUMENT_STATES",
    "UNPAID_SALES_INVOICE_STATES",
    "CapabilityMode",
    "MoneybirdClient",
    "MoneybirdError",
    "MoneybirdHTTPError",
    "OAuthConnection",
    "bank_description",
    "build_authorize_url",
    "build_filter_string",
    "build_patch_precondition",
    "capability_mode",
    "compact_document_summary",
    "compact_ledger_account_summary",
    "compare_controlled_fields",
    "contact_invoice_email",
    "describe_effective_period",
    "document_contact_title",
    "exchange_authorization_code",
    "get_credential_mode",
    "get_shared_http_client",
    "matches_query",
    "missing_scopes",
    "month_periods",
    "normalize_endpoint",
    "normalize_list_period",
    "normalize_text",
    "parse_authorization_callback",
    "refresh_access_token",
    "report_period_months",
    "require_write_capability",
    "resolve_credentials",
    "validate_moneybird_id",
]
