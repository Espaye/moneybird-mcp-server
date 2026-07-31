"""Thin HTTP client for the Moneybird REST API, with retry/backoff."""
from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .attachments import DEFAULT_MAX_ATTACHMENT_BYTES
from .config import (
    BASE_URL,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_RETRY_DELAY_SECONDS,
    PAGINATED_REPORTS,
    REPORT_ENDPOINTS,
    REPORT_PERIOD_PARAM_OVERRIDES,
    RETRYABLE_HTTP_STATUS_CODES,
    MoneybirdError,
)
from .credentials import resolve_credentials, set_active_administration_id
from .formatting import (
    build_filter_string,
    document_kind_config,
)
from .http_transport import get_shared_http_client
from .telemetry import (
    current_tool_name,
    current_trace_id,
    normalize_endpoint,
    record_api_call,
    set_current_tenant_scope,
    tenant_scope_for_token,
)

logger = logging.getLogger("moneybird_mcp")

def retry_delay_seconds(
    *,
    attempt: int,
    retry_after_header: str | None = None,
) -> float:
    delay = DEFAULT_RETRY_BACKOFF_SECONDS * (2**attempt)
    if retry_after_header:
        hint = _parse_retry_after(retry_after_header)
        if hint is not None and hint > 0:
            delay = hint
    # Always cap: Retry-After is per the HTTP spec either delta-seconds or an
    # HTTP-date, but servers sometimes send an absolute epoch timestamp, which as
    # a raw delay would block the client for decades.
    return min(delay, MAX_RETRY_DELAY_SECONDS)


def _parse_retry_after(value: str) -> float | None:
    """Interpret a Retry-After header as a number of seconds to wait, or None.

    Handles delta-seconds, an HTTP-date, and the malformed-but-seen case of an
    absolute epoch timestamp (treated as seconds-from-now). The caller caps the
    result, so even an unexpected value can never cause a runaway sleep.
    """
    text = value.strip()
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if dt is None:
            return None
        return (dt - datetime.now(dt.tzinfo)).total_seconds()
    # A "delay" larger than a day is almost certainly an absolute epoch
    # timestamp rather than delta-seconds; convert it to a relative delay.
    if parsed > 86400:
        return parsed - time.time()
    return parsed




def is_retryable_http_status(status_code: int) -> bool:
    return status_code in RETRYABLE_HTTP_STATUS_CODES


class _StopRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse automatic redirects so the caller can re-request without credentials.

    urllib's default handler forwards all original headers — including
    Authorization — to the redirect target. Attachment downloads redirect to a
    signed third-party storage URL, which must never see the Moneybird token.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection whose TCP destination is a prevalidated numeric address."""

    def __init__(
        self,
        host: str,
        pinned_addresses: tuple[str, ...],
        *args,
        **kwargs,
    ) -> None:
        kwargs.pop("check_hostname", None)
        super().__init__(host, *args, **kwargs)
        self._pinned_addresses = pinned_addresses

    def connect(self) -> None:
        last_error: OSError | None = None
        for address in self._pinned_addresses:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            raw_socket = socket.socket(family, socket.SOCK_STREAM)
            try:
                raw_socket.settimeout(self.timeout)
                if self.source_address:
                    raw_socket.bind(self.source_address)
                target = (
                    (address, self.port, 0, 0)
                    if family == socket.AF_INET6
                    else (address, self.port)
                )
                raw_socket.connect(target)
                self.sock = self._context.wrap_socket(
                    raw_socket,
                    server_hostname=self.host,
                )
                return
            except OSError as exc:
                last_error = exc
                raw_socket.close()
        if last_error is not None:
            raise last_error
        raise OSError("No validated attachment address was available.")


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib handler that preserves TLS hostname checks while pinning TCP DNS."""

    def __init__(self, pinned_addresses: tuple[str, ...]) -> None:
        super().__init__(context=ssl.create_default_context())
        self._pinned_addresses = pinned_addresses

    def https_open(self, request):
        def connection_factory(host, **kwargs):
            return _PinnedHTTPSConnection(
                host,
                self._pinned_addresses,
                **kwargs,
            )

        return self.do_open(
            connection_factory,
            request,
            context=self._context,
        )


_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}

_ASCII_NUMERIC_ID = re.compile(r"[0-9]+", re.ASCII)
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_MAX_API_PATH_LENGTH = 2048

# JSON-returning GET routes copied from the vendored Moneybird API snapshot in
# docs/moneybird_api_paths.json.  Deliberately excluded:
# - binary download routes (raw_get parses JSON);
# - routes whose path parameter is a human identifier/reference (the typed
#   helpers below encode those values as one route segment);
# - every non-GET operation.
#
# ``{id}`` always means an ASCII-numeric Moneybird record id.  Keeping the
# generic escape hatch to an explicit list makes a future spec addition opt-in
# instead of silently turning caller input into an arbitrary URL.
_SAFE_GENERIC_GET_TEMPLATES = frozenset(
    {
        "administrations",
        "assets",
        "assets/{id}",
        "contacts",
        "contacts/{id}",
        "contacts/{id}/additional_charges",
        "contacts/{id}/contact_people/{id}",
        "contacts/{id}/moneybird_payments_mandate",
        "contacts/filter",
        "contacts/synchronization",
        "custom_fields",
        "customer_contact_portal/{id}",
        "customer_contact_portal/{id}/invoices",
        "customer_contact_portal/{id}/subscriptions/{id}",
        "document_styles",
        "documents/general_documents",
        "documents/general_documents/{id}",
        "documents/general_documents/synchronization",
        "documents/general_journal_documents",
        "documents/general_journal_documents/{id}",
        "documents/general_journal_documents/synchronization",
        "documents/purchase_invoices",
        "documents/purchase_invoices/{id}",
        "documents/purchase_invoices/synchronization",
        "documents/receipts",
        "documents/receipts/{id}",
        "documents/receipts/synchronization",
        "documents/typeless_documents",
        "documents/typeless_documents/{id}",
        "documents/typeless_documents/synchronization",
        "downloads",
        "estimates",
        "estimates/{id}",
        "estimates/synchronization",
        "external_sales_invoices",
        "external_sales_invoices/{id}",
        "external_sales_invoices/synchronization",
        "financial_accounts",
        "financial_mutations",
        "financial_mutations/{id}",
        "financial_mutations/synchronization",
        "identities",
        "identities/{id}",
        "identities/default",
        "ledger_accounts",
        "ledger_accounts/{id}",
        "payments/{id}",
        "products",
        "products/{id}",
        "projects",
        "projects/{id}",
        "purchase_transactions",
        "purchase_transactions/{id}",
        "recurring_sales_invoices",
        "recurring_sales_invoices/{id}",
        "recurring_sales_invoices/synchronization",
        "reports/assets",
        "reports/balance_sheet",
        "reports/cash_flow",
        "reports/creditors",
        "reports/creditors_aging",
        "reports/debtors",
        "reports/debtors_aging",
        "reports/expenses_by_contact",
        "reports/expenses_by_project",
        "reports/general_ledger",
        "reports/journal_entries",
        "reports/ledger_accounts/{id}",
        "reports/profit_loss",
        "reports/revenue_by_contact",
        "reports/revenue_by_project",
        "reports/subscriptions",
        "reports/tax",
        "sales_invoices",
        "sales_invoices/{id}",
        "sales_invoices/synchronization",
        "subscription_templates",
        "subscriptions",
        "subscriptions/{id}",
        "subscriptions/{id}/additional_charges",
        "task_list_groups/{id}",
        "task_list_tasks/{id}",
        "task_list_templates",
        "task_list_templates/{id}",
        "task_lists",
        "task_lists/{id}",
        "tax_rates",
        "time_entries",
        "time_entries/{id}",
        "users",
        "verifications",
        "webhooks",
        "workflows",
        "workflows/{id}",
    }
)


def validate_moneybird_id(value: Any, field_name: str = "id") -> str:
    """Return an ASCII-numeric Moneybird id or fail before URL construction."""
    text = str(value)
    if not _ASCII_NUMERIC_ID.fullmatch(text):
        raise MoneybirdError(
            f"{field_name} must be a non-empty Moneybird id containing ASCII digits only."
        )
    return text


def _encode_human_route_segment(value: Any, field_name: str) -> str:
    """Encode a human identifier as data in exactly one URL path segment."""
    text = str(value)
    if not text:
        raise MoneybirdError(f"{field_name} is required.")
    if _CONTROL_CHARACTER.search(text) or "\\" in text:
        raise MoneybirdError(f"{field_name} contains an unsafe character.")
    # quote(..., safe="") keeps the route structure fixed while preserving
    # ordinary customer numbers/references containing spaces, slashes or Unicode.
    return urllib.parse.quote(text, safe="", encoding="utf-8", errors="strict")


def _generic_template_matches(candidate: str, template: str) -> bool:
    candidate_segments = candidate.split("/")
    template_segments = template.split("/")
    if len(candidate_segments) != len(template_segments):
        return False
    return all(
        _ASCII_NUMERIC_ID.fullmatch(actual) is not None
        if expected == "{id}"
        else actual == expected
        for actual, expected in zip(candidate_segments, template_segments)
    )


def normalize_generic_get_path(path: str) -> str:
    """Validate and canonicalize a relative, JSON-returning generic GET route."""
    if not isinstance(path, str):
        raise MoneybirdError("Generic GET path must be a string.")
    if not path or path != path.strip():
        raise MoneybirdError(
            "Generic GET path must be non-empty and have no surrounding whitespace."
        )
    if len(path) > _MAX_API_PATH_LENGTH:
        raise MoneybirdError("Generic GET path is too long.")
    if _CONTROL_CHARACTER.search(path):
        raise MoneybirdError("Generic GET path contains a control character.")
    if not path.isascii():
        raise MoneybirdError("Generic GET path must contain ASCII route characters only.")
    if (
        path.startswith("/")
        or "\\" in path
        or "?" in path
        or "#" in path
        or ":" in path
        or "%" in path
    ):
        raise MoneybirdError(
            "Generic GET path must be a relative route without an authority, scheme, "
            "query string, fragment, backslash, or percent-encoded segment."
        )

    candidate = path.removesuffix(".json")
    segments = candidate.split("/")
    if (
        not candidate
        or any(not segment for segment in segments)
        or any(segment in {".", ".."} for segment in segments)
    ):
        raise MoneybirdError("Generic GET path contains an empty or dot segment.")
    if _ASCII_NUMERIC_ID.fullmatch(segments[0]):
        raise MoneybirdError(
            "Generic GET path must be relative to the configured administration; "
            "do not include an administration id."
        )
    if not any(
        _generic_template_matches(candidate, template)
        for template in _SAFE_GENERIC_GET_TEMPLATES
    ):
        raise MoneybirdError(
            "Unsupported generic GET path. Use a JSON-returning route from the "
            "server allowlist, such as 'estimates', 'time_entries/123', "
            "'documents/purchase_invoices', or 'administrations'."
        )
    return candidate


def generic_get_requires_administration(path: str) -> bool:
    """Validate ``path`` and report whether it is administration-scoped."""
    return normalize_generic_get_path(path) != "administrations"


def _decoded_path_layers(path: str) -> list[str]:
    """Return bounded repeated-decode views used to catch encoded traversal."""
    layers = [path]
    current = path
    for _ in range(5):
        try:
            decoded = urllib.parse.unquote(current, encoding="utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise MoneybirdError("Moneybird API path contains invalid percent encoding.") from exc
        if decoded == current:
            return layers
        layers.append(decoded)
        current = decoded
    try:
        decoded_once_more = urllib.parse.unquote(
            current,
            encoding="utf-8",
            errors="strict",
        )
    except UnicodeDecodeError as exc:
        raise MoneybirdError("Moneybird API path contains invalid percent encoding.") from exc
    if decoded_once_more != current:
        raise MoneybirdError("Moneybird API path contains excessive nested encoding.")
    return layers


def _read_bounded_response(
    response: Any,
    *,
    max_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
) -> bytes:
    """Read at most ``max_bytes`` and reject dishonest/missing-size overflows."""
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            declared = int(content_length)
        except (TypeError, ValueError) as exc:
            raise MoneybirdError(
                "Attachment response has an invalid Content-Length header."
            ) from exc
        if declared < 0 or declared > max_bytes:
            raise MoneybirdError(
                f"Attachment exceeds the {max_bytes}-byte download limit."
            )
    data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise MoneybirdError(
            f"Attachment exceeds the {max_bytes}-byte download limit."
        )
    return data


def _validated_attachment_redirect_target(
    base_url: str,
    location: str,
) -> tuple[str, tuple[str, ...]]:
    """Resolve one signed redirect and return its validated, pinned addresses."""
    signed_url = urllib.parse.urljoin(base_url, location)
    parsed = urllib.parse.urlsplit(signed_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise MoneybirdError("Attachment redirect contains an invalid port.") from exc
    hostname = parsed.hostname or ""
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or not hostname.isascii()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise MoneybirdError(
            "Attachment redirect must be credential-free HTTPS on the default port."
        )

    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                hostname,
                443,
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as exc:
        raise MoneybirdError(
            "Attachment redirect hostname could not be resolved safely."
        ) from exc
    if not addresses:
        raise MoneybirdError("Attachment redirect hostname resolved to no addresses.")
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as exc:
            raise MoneybirdError(
                "Attachment redirect resolved to an invalid address."
            ) from exc
        if (
            not parsed_address.is_global
            or parsed_address.is_multicast
            or parsed_address.is_reserved
            or getattr(parsed_address, "is_site_local", False)
        ):
            raise MoneybirdError(
                "Attachment redirect to a non-public network address was refused."
            )
    return signed_url, tuple(sorted(addresses))


def _validated_attachment_redirect(base_url: str, location: str) -> str:
    """Backward-compatible URL-only view of the strict redirect validator."""
    return _validated_attachment_redirect_target(base_url, location)[0]




class MoneybirdClient:
    def __init__(
        self,
        token: str,
        administration_id: str | None,
        *,
        require_administration: bool = True,
    ) -> None:
        if not token:
            raise MoneybirdError(
                "MONEYBIRD_ACCESS_TOKEN is missing. Supply it in the parent "
                "environment or select a file explicitly with --env-file PATH."
            )

        self.token = token
        self.telemetry_tenant_scope = tenant_scope_for_token(token)
        set_current_tenant_scope(self.telemetry_tenant_scope)
        self.base_url = BASE_URL.rstrip("/")
        self.timeout = DEFAULT_TIMEOUT_SECONDS
        if administration_id:
            self.administration_id = validate_moneybird_id(
                administration_id,
                "administration_id",
            )
        elif require_administration:
            self.administration_id = validate_moneybird_id(
                self._auto_select_administration(),
                "administration_id",
            )
        else:
            self.administration_id = None

    def _confine_api_path(self, path: str) -> str:
        """Reject any path that could normalize outside this client's tenant."""
        if not isinstance(path, str) or not path:
            raise MoneybirdError("Moneybird API path must be a non-empty string.")
        if len(path) > _MAX_API_PATH_LENGTH:
            raise MoneybirdError("Moneybird API path is too long.")
        if (
            not path.startswith("/")
            or path.startswith("//")
            or "?" in path
            or "#" in path
            or ":" in path
            or "\\" in path
            or _CONTROL_CHARACTER.search(path)
            or _INVALID_PERCENT_ESCAPE.search(path)
            or not path.isascii()
        ):
            raise MoneybirdError(
                "Moneybird API path must be an absolute API path without an authority, "
                "scheme, query string, fragment, backslash, malformed escape, "
                "control character, or raw Unicode."
            )

        for decoded in _decoded_path_layers(path):
            if (
                not decoded.startswith("/")
                or decoded.startswith("//")
                or "\\" in decoded
                or _CONTROL_CHARACTER.search(decoded)
            ):
                raise MoneybirdError(
                    "Moneybird API path contains an encoded authority, backslash, "
                    "or control character."
                )
            segments = decoded[1:].split("/")
            if any(not segment for segment in segments):
                raise MoneybirdError("Moneybird API path contains an empty segment.")
            logical_segments = list(segments)
            if logical_segments[-1].endswith(".json"):
                logical_segments[-1] = logical_segments[-1][:-5]
            if any(segment in {".", ".."} for segment in logical_segments):
                raise MoneybirdError(
                    "Moneybird API path contains a dot or encoded-dot segment."
                )

            if segments == ["administrations.json"]:
                continue
            if self.administration_id is None:
                raise MoneybirdError(
                    "An administration id is required for this Moneybird API path."
                )
            if segments[0] != self.administration_id:
                raise MoneybirdError(
                    "Moneybird API path is outside the configured administration."
                )
        return path

    def _request(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        *,
        retry_safe: bool | None = None,
        operation: str | None = None,
    ) -> Any:
        method = method.upper()
        if retry_safe is None:
            retry_safe = method in {"GET", "HEAD", "OPTIONS"}
        path = self._confine_api_path(path)
        # Runtime paths contain administration and record ids. Keep those out of
        # metrics, retry logs, and user-visible transport errors unless the
        # caller supplied an even more specific static operation label.
        operation = operation or normalize_endpoint(path)
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query, doseq=True)}"
        serialized_body = json.dumps(body).encode("utf-8") if body is not None else None
        trace_id = current_trace_id()
        tool_name = current_tool_name()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if serialized_body is not None:
            headers["Content-Type"] = "application/json"

        for attempt in range(DEFAULT_RETRY_ATTEMPTS + 1):
            attempt_started = time.perf_counter()
            try:
                response = get_shared_http_client().request(
                    method,
                    url,
                    headers=headers,
                    content=serialized_body,
                    timeout=self.timeout,
                )
                response_status = response.status_code
                payload = response.text
                record_api_call(
                    method=method,
                    operation=operation,
                    status=response_status,
                    duration_seconds=time.perf_counter() - attempt_started,
                    retry=attempt,
                    trace_id=trace_id,
                    tool_name=tool_name,
                    tenant_scope=self.telemetry_tenant_scope,
                )
                if response_status < 300:
                    return json.loads(payload) if payload else None
                if (
                    retry_safe
                    and attempt < DEFAULT_RETRY_ATTEMPTS
                    and is_retryable_http_status(response_status)
                ):
                    delay = retry_delay_seconds(
                        attempt=attempt,
                        retry_after_header=response.headers.get("Retry-After"),
                    )
                    logger.warning(
                        "Retrying Moneybird %s %s after HTTP %s in %.1fs (attempt %s/%s)",
                        method,
                        operation,
                        response_status,
                        delay,
                        attempt + 1,
                        DEFAULT_RETRY_ATTEMPTS,
                    )
                    time.sleep(delay)
                    continue
                retry_note = (
                    " Automatic retry was disabled because this write may already have "
                    "been processed; reconcile the record before retrying."
                    if not retry_safe and is_retryable_http_status(response_status)
                    else ""
                )
                raise MoneybirdError(
                    f"Moneybird returned HTTP {response_status} for operation "
                    f"{operation}.{retry_note}"
                )
            except httpx.TransportError:
                record_api_call(
                    method=method,
                    operation=operation,
                    status="network_error",
                    duration_seconds=time.perf_counter() - attempt_started,
                    retry=attempt,
                    trace_id=trace_id,
                    tool_name=tool_name,
                    tenant_scope=self.telemetry_tenant_scope,
                )
                if retry_safe and attempt < DEFAULT_RETRY_ATTEMPTS:
                    delay = retry_delay_seconds(attempt=attempt)
                    logger.warning(
                        "Retrying Moneybird %s %s after network error in %.1fs (attempt %s/%s)",
                        method,
                        operation,
                        delay,
                        attempt + 1,
                        DEFAULT_RETRY_ATTEMPTS,
                    )
                    time.sleep(delay)
                    continue
                retry_note = (
                    " The write result is ambiguous; reconcile Moneybird before retrying."
                    if not retry_safe
                    else ""
                )
                raise MoneybirdError(
                    f"Could not reach Moneybird for operation {operation}.{retry_note}"
                ) from None

    def _auto_select_administration(self) -> str:
        administrations = self.list_administrations()
        if len(administrations) == 1:
            return str(administrations[0]["id"])

        names = ", ".join(
            f'{item.get("name", "unnamed")} ({item["id"]})' for item in administrations
        )
        raise MoneybirdError(
            "MONEYBIRD_ADMINISTRATION_ID is missing and auto-selection is ambiguous. "
            f"Choose one of: {names}"
        )

    def list_administrations(self) -> list[dict[str, Any]]:
        return self._request("GET", "/administrations.json")

    def require_current_administration_access(self) -> dict[str, Any]:
        """Revalidate token membership before reading administration-scoped cache.

        A caller-selected administration id is not proof that this token may access
        it. This deliberately performs a live root request each time a durable
        financial cache is about to be used, bounding revocation/membership removal
        to the availability of Moneybird's administration listing.
        """
        if self.administration_id is None:
            raise MoneybirdError(
                "An administration id is required before accessing cached data."
            )
        administrations = self.list_administrations()
        for administration in administrations:
            candidate = administration.get("id")
            if candidate is None:
                continue
            try:
                candidate_id = validate_moneybird_id(
                    candidate,
                    "Moneybird administration id",
                )
            except MoneybirdError:
                continue
            if candidate_id == self.administration_id:
                return administration
        raise MoneybirdError(
            "The active Moneybird token does not currently have access to the "
            "selected administration. Cached data was not read."
        )

    def list_contacts(self, *, limit: int = 10, page: int = 1) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/{self.administration_id}/contacts.json",
            {"per_page": max(1, min(limit, 100)), "page": max(1, page)},
        )

    def get_contact(self, contact_id: str) -> dict[str, Any]:
        contact_id = validate_moneybird_id(contact_id, "contact_id")
        return self._request(
            "GET",
            f"/{self.administration_id}/contacts/{contact_id}.json",
        )

    def get_contact_by_customer_id(self, customer_id: str) -> dict[str, Any]:
        encoded_customer_id = _encode_human_route_segment(customer_id, "customer_id")
        return self._request(
            "GET",
            f"/{self.administration_id}/contacts/customer_id/{encoded_customer_id}.json",
            operation="/:administration/contacts/customer_id/:customer_id.json",
        )

    def create_contact(self, contact: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/{self.administration_id}/contacts.json",
            body={"contact": contact},
        )

    def update_contact(self, contact_id: str, contact: dict[str, Any]) -> dict[str, Any]:
        contact_id = validate_moneybird_id(contact_id, "contact_id")
        return self._request(
            "PATCH",
            f"/{self.administration_id}/contacts/{contact_id}.json",
            body={"contact": contact},
        )

    def archive_contact(self, contact_id: str) -> None:
        contact_id = validate_moneybird_id(contact_id, "contact_id")
        self._request(
            "PATCH",
            f"/{self.administration_id}/contacts/{contact_id}/archive.json",
        )

    def list_sales_invoices(
        self,
        *,
        limit: int = 10,
        page: int = 1,
        state: str = "all",
        reference: str = "",
        contact_id: str = "",
        period: str = "",
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {
            "per_page": max(1, min(limit, 100)),
            "page": max(1, page),
        }
        filter_parts: list[str] = []
        if state != "all":
            filter_parts.append(f"state:{state}")
        elif reference or contact_id or period:
            filter_parts.append("state:all")
        if reference:
            filter_parts.append(f"reference:{reference}")
        if contact_id:
            contact_id = validate_moneybird_id(contact_id, "contact_id")
            filter_parts.append(f"contact_id:{contact_id}")
        if period:
            filter_parts.append(f"period:{period}")
        if filter_parts:
            query["filter"] = ",".join(filter_parts)

        return self._request(
            "GET",
            f"/{self.administration_id}/sales_invoices.json",
            query,
        )

    def get_sales_invoice(self, invoice_id: str) -> dict[str, Any]:
        invoice_id = validate_moneybird_id(invoice_id, "sales_invoice_id")
        return self._request(
            "GET",
            f"/{self.administration_id}/sales_invoices/{invoice_id}.json",
        )

    def create_sales_invoice(self, sales_invoice: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/{self.administration_id}/sales_invoices.json",
            body={"sales_invoice": sales_invoice},
        )

    def update_sales_invoice(
        self,
        sales_invoice_id: str,
        sales_invoice: dict[str, Any],
    ) -> dict[str, Any]:
        sales_invoice_id = validate_moneybird_id(
            sales_invoice_id,
            "sales_invoice_id",
        )
        return self._request(
            "PATCH",
            f"/{self.administration_id}/sales_invoices/{sales_invoice_id}.json",
            body={"sales_invoice": sales_invoice},
        )

    def send_sales_invoice(
        self,
        sales_invoice_id: str,
        sales_invoice_sending: dict[str, Any],
    ) -> dict[str, Any]:
        sales_invoice_id = validate_moneybird_id(
            sales_invoice_id,
            "sales_invoice_id",
        )
        return self._request(
            "PATCH",
            f"/{self.administration_id}/sales_invoices/{sales_invoice_id}/send_invoice.json",
            body={"sales_invoice_sending": sales_invoice_sending},
        )

    def pause_sales_invoice(self, sales_invoice_id: str) -> dict[str, Any]:
        sales_invoice_id = validate_moneybird_id(
            sales_invoice_id,
            "sales_invoice_id",
        )
        return self._request(
            "POST",
            f"/{self.administration_id}/sales_invoices/{sales_invoice_id}/pause.json",
        )

    def resume_sales_invoice(self, sales_invoice_id: str) -> dict[str, Any]:
        sales_invoice_id = validate_moneybird_id(
            sales_invoice_id,
            "sales_invoice_id",
        )
        return self._request(
            "POST",
            f"/{self.administration_id}/sales_invoices/{sales_invoice_id}/resume.json",
        )

    def get_sales_invoice_by_reference(self, reference: str) -> dict[str, Any]:
        encoded_reference = _encode_human_route_segment(reference, "reference")
        return self._request(
            "GET",
            f"/{self.administration_id}/sales_invoices/find_by_reference/{encoded_reference}.json",
            operation=(
                "/:administration/sales_invoices/"
                "find_by_reference/:reference.json"
            ),
        )

    def get_sales_invoice_by_invoice_id(self, invoice_id: str) -> dict[str, Any]:
        encoded_invoice_id = _encode_human_route_segment(invoice_id, "invoice_id")
        return self._request(
            "GET",
            f"/{self.administration_id}/sales_invoices/find_by_invoice_id/{encoded_invoice_id}.json",
            operation=(
                "/:administration/sales_invoices/"
                "find_by_invoice_id/:invoice_id.json"
            ),
        )

    def get_recurring_sales_invoice(self, recurring_sales_invoice_id: str) -> dict[str, Any]:
        recurring_sales_invoice_id = validate_moneybird_id(
            recurring_sales_invoice_id,
            "recurring_sales_invoice_id",
        )
        return self._request(
            "GET",
            f"/{self.administration_id}/recurring_sales_invoices/{recurring_sales_invoice_id}.json",
        )

    def list_recurring_sales_invoice_versions(self) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/{self.administration_id}/recurring_sales_invoices/synchronization.json",
        )

    def fetch_recurring_sales_invoices_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        ids = [
            validate_moneybird_id(item, "recurring_sales_invoice_id")
            for item in ids
        ]
        return self._request(
            "POST",
            f"/{self.administration_id}/recurring_sales_invoices/synchronization.json",
            body={"ids": ids},
            retry_safe=True,
        )

    def list_products(self, *, limit: int = 25, page: int = 1) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/{self.administration_id}/products.json",
            {"per_page": max(1, min(limit, 100)), "page": max(1, page)},
        )

    def list_tax_rates(self) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/{self.administration_id}/tax_rates.json",
        )

    def list_ledger_accounts(self) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/{self.administration_id}/ledger_accounts.json",
        )

    def get_ledger_account(self, ledger_account_id: str) -> dict[str, Any]:
        ledger_account_id = validate_moneybird_id(
            ledger_account_id,
            "ledger_account_id",
        )
        return self._request(
            "GET",
            f"/{self.administration_id}/ledger_accounts/{ledger_account_id}.json",
        )

    def list_financial_accounts(
        self,
        *,
        limit: int = 25,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/{self.administration_id}/financial_accounts.json",
            {"per_page": max(1, min(limit, 100)), "page": max(1, page)},
        )

    def get_financial_account(self, financial_account_id: str) -> dict[str, Any]:
        # The API documents no GET /financial_accounts/{id} (it returns 404 live),
        # so fetch the list and select the record client-side.
        wanted = validate_moneybird_id(
            financial_account_id,
            "financial_account_id",
        )
        for account in self.list_financial_accounts(limit=100):
            if str(account.get("id")) == wanted:
                return account
        raise MoneybirdError(
            f"Financial account {financial_account_id} not found in this administration."
        )

    def list_projects(
        self,
        *,
        limit: int = 25,
        page: int = 1,
        state: str = "",
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {
            "per_page": max(1, min(limit, 100)),
            "page": max(1, page),
        }
        if state:
            query["filter"] = f"state:{state}"
        return self._request(
            "GET",
            f"/{self.administration_id}/projects.json",
            query,
        )

    def list_time_entries(
        self,
        *,
        limit: int = 25,
        page: int = 1,
        filter: str = "",
        period: str = "",
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {
            "per_page": max(1, min(limit, 100)),
            "page": max(1, page),
        }
        filter_string = build_filter_string(filter=filter, period=period)
        if filter_string:
            query["filter"] = filter_string
        return self._request(
            "GET",
            f"/{self.administration_id}/time_entries.json",
            query,
        )

    def raw_get(self, path: str, query: dict[str, Any] | None = None) -> Any:
        """Perform a GET against an allowlisted, JSON-returning Moneybird route.

        ``path`` must be relative to the configured administration. Query
        parameters belong in ``query`` rather than in the path. Human identifiers
        with route-sensitive characters use the typed lookup helpers instead.
        """
        raw = normalize_generic_get_path(path)
        if raw == "administrations":
            endpoint = "/administrations"
        else:
            if self.administration_id is None:
                raise MoneybirdError(
                    "An administration id is required for this generic GET path."
                )
            endpoint = f"/{self.administration_id}/{raw}"
        if not endpoint.endswith(".json"):
            endpoint = f"{endpoint}.json"
        return self._request("GET", endpoint, query=query)

    def list_contact_versions(self) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/{self.administration_id}/contacts/synchronization.json",
        )

    def fetch_contacts_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        ids = [validate_moneybird_id(item, "contact_id") for item in ids]
        return self._request(
            "POST",
            f"/{self.administration_id}/contacts/synchronization.json",
            body={"ids": ids},
            retry_safe=True,
        )

    def list_sales_invoice_versions(self, *, filter: str = "") -> list[dict[str, Any]]:
        query = {"filter": filter} if filter else None
        return self._request(
            "GET",
            f"/{self.administration_id}/sales_invoices/synchronization.json",
            query=query,
        )

    def fetch_sales_invoices_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        ids = [validate_moneybird_id(item, "sales_invoice_id") for item in ids]
        return self._request(
            "POST",
            f"/{self.administration_id}/sales_invoices/synchronization.json",
            body={"ids": ids},
            retry_safe=True,
        )

    def list_documents(
        self,
        kind: str,
        *,
        limit: int = 10,
        page: int = 1,
        filter: str = "",
        period: str = "",
    ) -> list[dict[str, Any]]:
        config = document_kind_config(kind)
        query: dict[str, Any] = {
            "per_page": max(1, min(limit, 100)),
            "page": max(1, page),
        }
        filter_string = build_filter_string(filter=filter, period=period)
        if filter_string:
            query["filter"] = filter_string
        return self._request(
            "GET",
            f"/{self.administration_id}/{config['collection_path']}.json",
            query=query,
        )

    def get_document(self, kind: str, document_id: str) -> dict[str, Any]:
        config = document_kind_config(kind)
        document_id = validate_moneybird_id(document_id, "document_id")
        return self._request(
            "GET",
            f"/{self.administration_id}/{config['collection_path']}/{document_id}.json",
        )

    def get_document_by_reference(self, kind: str, reference: str) -> dict[str, Any]:
        """Return the one document whose reference exactly matches ``reference``.

        Moneybird has dedicated find-by-reference endpoints for sales invoices but
        not for purchase documents. The document collection does support a
        server-side ``reference:`` filter, so use that instead of a broad search
        across contacts, documents, and financial mutations.
        """
        normalized_reference = str(reference or "").strip()
        if not normalized_reference:
            raise MoneybirdError("reference is required.")

        candidates = self.list_documents(
            kind,
            limit=100,
            page=1,
            filter=f"reference:{normalized_reference}",
        )
        exact = [
            document
            for document in candidates
            if str(document.get("reference") or "").strip() == normalized_reference
        ]
        if not exact:
            raise MoneybirdError(
                f"No {kind} with reference '{normalized_reference}' was found."
            )
        if len(exact) > 1:
            ids = ", ".join(str(document.get("id") or "") for document in exact)
            raise MoneybirdError(
                f"Reference '{normalized_reference}' matches multiple {kind} documents "
                f"({ids}); use the internal document id."
            )

        document = exact[0]
        if "details" not in document or "attachments" not in document:
            return self.get_document(kind, str(document.get("id")))
        return document

    def update_document(
        self,
        kind: str,
        document_id: str,
        document: dict[str, Any],
    ) -> dict[str, Any]:
        config = document_kind_config(kind)
        document_id = validate_moneybird_id(document_id, "document_id")
        return self._request(
            "PATCH",
            f"/{self.administration_id}/{config['collection_path']}/{document_id}.json",
            body={config["record_key"]: document},
        )

    def list_document_versions(
        self,
        kind: str,
        *,
        filter: str = "",
    ) -> list[dict[str, Any]]:
        config = document_kind_config(kind)
        query = {"filter": filter} if filter else None
        return self._request(
            "GET",
            f"/{self.administration_id}/{config['collection_path']}/synchronization.json",
            query=query,
        )

    def fetch_documents_by_ids(self, kind: str, ids: list[str]) -> list[dict[str, Any]]:
        config = document_kind_config(kind)
        ids = [validate_moneybird_id(item, "document_id") for item in ids]
        return self._request(
            "POST",
            f"/{self.administration_id}/{config['collection_path']}/synchronization.json",
            body={"ids": ids},
            retry_safe=True,
        )

    def _binary_request(self, method: str, path: str) -> tuple[bytes, str]:
        """Fetch a non-JSON endpoint (attachment download); returns (bytes, content_type).

        Moneybird serves attachment bytes either directly or via a redirect to a
        short-lived signed storage URL. The redirect is followed manually and
        *without* the Authorization header: the signed URL doesn't need it, and
        the bearer token must not leak to the storage host (which may also
        reject authorized requests outright).
        """
        path = self._confine_api_path(path)
        url = f"{self.base_url}{path}"
        opener = urllib.request.build_opener(_StopRedirects)
        for attempt in range(DEFAULT_RETRY_ATTEMPTS + 1):
            request = urllib.request.Request(url=url, method=method)
            request.add_header("Authorization", f"Bearer {self.token}")
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    content_type = response.headers.get("Content-Type") or "application/octet-stream"
                    return _read_bounded_response(response), content_type
            except urllib.error.HTTPError as exc:
                if exc.code in _REDIRECT_STATUS_CODES:
                    location = exc.headers.get("Location")
                    if not location:
                        raise MoneybirdError(
                            "Moneybird redirected an attachment request without a "
                            "Location header."
                        ) from exc
                    signed_url, pinned_addresses = (
                        _validated_attachment_redirect_target(url, location)
                    )
                    signed_request = urllib.request.Request(url=signed_url, method="GET")
                    signed_opener = urllib.request.build_opener(
                        urllib.request.ProxyHandler({}),
                        _PinnedHTTPSHandler(pinned_addresses),
                        _StopRedirects,
                    )
                    try:
                        # Use the same no-redirect opener for the signed fetch.
                        # A second redirect would otherwise bypass the HTTPS,
                        # port, credential, DNS, and public-address checks above.
                        with signed_opener.open(
                            signed_request,
                            timeout=self.timeout,
                        ) as response:
                            content_type = (
                                response.headers.get("Content-Type")
                                or "application/octet-stream"
                            )
                            return _read_bounded_response(response), content_type
                    except urllib.error.HTTPError as signed_exc:
                        if signed_exc.code in _REDIRECT_STATUS_CODES:
                            raise MoneybirdError(
                                "Attachment storage attempted an unvalidated second "
                                "redirect, which was refused."
                            ) from None
                        raise MoneybirdError(
                            f"Attachment storage returned HTTP {signed_exc.code}."
                        ) from None
                if attempt < DEFAULT_RETRY_ATTEMPTS and is_retryable_http_status(exc.code):
                    time.sleep(
                        retry_delay_seconds(
                            attempt=attempt,
                            retry_after_header=exc.headers.get("Retry-After"),
                        )
                    )
                    continue
                raise MoneybirdError(
                    f"Moneybird returned HTTP {exc.code} for an attachment request."
                ) from None
            except urllib.error.URLError:
                if attempt < DEFAULT_RETRY_ATTEMPTS:
                    time.sleep(retry_delay_seconds(attempt=attempt))
                    continue
                raise MoneybirdError(
                    "Could not reach Moneybird for an attachment request."
                ) from None

    def download_attachment(
        self,
        kind: str,
        document_id: str,
        attachment_id: str,
    ) -> tuple[bytes, str]:
        """Download a document attachment's raw bytes; returns (data, content_type)."""
        config = document_kind_config(kind)
        document_id = validate_moneybird_id(document_id, "document_id")
        attachment_id = validate_moneybird_id(attachment_id, "attachment_id")
        return self._binary_request(
            "GET",
            f"/{self.administration_id}/{config['collection_path']}/{document_id}/attachments/{attachment_id}/download",
        )

    def list_financial_mutations(
        self,
        *,
        limit: int = 10,
        page: int = 1,
        filter: str = "",
        period: str = "",
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {
            "per_page": max(1, min(limit, 100)),
            "page": max(1, page),
        }
        filter_string = build_filter_string(filter=filter, period=period)
        if filter_string:
            query["filter"] = filter_string
        return self._request(
            "GET",
            f"/{self.administration_id}/financial_mutations.json",
            query=query,
        )

    def get_financial_mutation(self, mutation_id: str) -> dict[str, Any]:
        mutation_id = validate_moneybird_id(
            mutation_id,
            "financial_mutation_id",
        )
        return self._request(
            "GET",
            f"/{self.administration_id}/financial_mutations/{mutation_id}.json",
        )

    def list_financial_mutation_versions(
        self,
        *,
        filter: str = "",
    ) -> list[dict[str, Any]]:
        query = {"filter": filter} if filter else None
        return self._request(
            "GET",
            f"/{self.administration_id}/financial_mutations/synchronization.json",
            query=query,
        )

    def fetch_financial_mutations_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        ids = [
            validate_moneybird_id(item, "financial_mutation_id")
            for item in ids
        ]
        return self._request(
            "POST",
            f"/{self.administration_id}/financial_mutations/synchronization.json",
            body={"ids": ids},
            retry_safe=True,
        )

    def get_report(
        self,
        report_name: str,
        *,
        period: str,
        page: int | None = None,
        extra_query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = str(report_name).strip()
        endpoint = REPORT_ENDPOINTS.get(name)
        if not endpoint:
            supported = ", ".join(sorted(REPORT_ENDPOINTS))
            raise MoneybirdError(
                f"Unsupported report '{report_name}'. Use one of: {supported}."
            )
        period_param = REPORT_PERIOD_PARAM_OVERRIDES.get(name, "period")
        query: dict[str, Any] = {period_param: period}
        if page is not None:
            if name not in PAGINATED_REPORTS:
                raise MoneybirdError(
                    f"Report '{name}' does not support pagination."
                )
            query["page"] = max(1, page)
        if extra_query:
            query.update(extra_query)
        return self._request(
            "GET",
            f"/{self.administration_id}/reports/{endpoint}.json",
            query=query,
        )

    def register_sales_invoice_payment(
        self,
        sales_invoice_id: str,
        payment: dict[str, Any],
    ) -> dict[str, Any]:
        sales_invoice_id = validate_moneybird_id(
            sales_invoice_id,
            "sales_invoice_id",
        )
        return self._request(
            "PATCH",
            f"/{self.administration_id}/sales_invoices/{sales_invoice_id}/register_payment.json",
            body={"payment": payment},
        )

    def register_document_payment(
        self,
        kind: str,
        document_id: str,
        payment: dict[str, Any],
    ) -> dict[str, Any]:
        config = document_kind_config(kind)
        if config["record_key"] not in {"purchase_invoice", "receipt"}:
            raise MoneybirdError(
                f"Documents of kind '{kind}' do not support payment registration."
            )
        document_id = validate_moneybird_id(document_id, "document_id")
        return self._request(
            "PATCH",
            f"/{self.administration_id}/{config['collection_path']}/{document_id}/register_payment.json",
            body={"payment": payment},
        )

    def link_financial_mutation_booking(
        self,
        mutation_id: str,
        booking: dict[str, Any],
    ) -> Any:
        # The public MCP tools use the signed ``price`` returned on payments and
        # ledger-account bookings. Moneybird expects a magnitude and derives the
        # sign from the mutation. Direct ledger bookings use ``price_base``;
        # invoice/document payments use ``price`` in the booking's currency.
        request_booking = dict(booking)
        if "booking_id" in request_booking:
            request_booking["booking_id"] = validate_moneybird_id(
                request_booking["booking_id"],
                "booking_id",
            )
        if "price" in request_booking and "price_base" not in request_booking:
            price = request_booking.pop("price")
            amount_field = (
                "price"
                if request_booking.get("booking_type")
                in {"SalesInvoice", "Document", "ExternalSalesInvoice", "VatDocument"}
                else "price_base"
            )
            request_booking[amount_field] = format(
                abs(Decimal(str(price))),
                "f",
            )
        mutation_id = validate_moneybird_id(
            mutation_id,
            "financial_mutation_id",
        )
        return self._request(
            "PATCH",
            f"/{self.administration_id}/financial_mutations/{mutation_id}/link_booking.json",
            body=request_booking,
        )

    def unlink_financial_mutation_booking(
        self,
        mutation_id: str,
        *,
        booking_type: str,
        booking_id: str,
    ) -> Any:
        mutation_id = validate_moneybird_id(
            mutation_id,
            "financial_mutation_id",
        )
        booking_id = validate_moneybird_id(booking_id, "booking_id")
        return self._request(
            "DELETE",
            f"/{self.administration_id}/financial_mutations/{mutation_id}/unlink_booking.json",
            body={"booking_type": booking_type, "booking_id": booking_id},
        )

    def duplicate_sales_invoice_to_credit_invoice(
        self,
        sales_invoice_id: str,
    ) -> dict[str, Any]:
        sales_invoice_id = validate_moneybird_id(
            sales_invoice_id,
            "sales_invoice_id",
        )
        return self._request(
            "PATCH",
            f"/{self.administration_id}/sales_invoices/{sales_invoice_id}/duplicate_creditinvoice.json",
        )

    def list_estimates(
        self,
        *,
        limit: int = 10,
        page: int = 1,
        filter: str = "",
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {
            "per_page": max(1, min(limit, 100)),
            "page": max(1, page),
        }
        if filter:
            query["filter"] = filter
        return self._request(
            "GET",
            f"/{self.administration_id}/estimates.json",
            query=query,
        )

    def get_estimate(self, estimate_id: str) -> dict[str, Any]:
        estimate_id = validate_moneybird_id(estimate_id, "estimate_id")
        return self._request(
            "GET",
            f"/{self.administration_id}/estimates/{estimate_id}.json",
        )

    def list_recurring_sales_invoices(
        self,
        *,
        limit: int = 10,
        page: int = 1,
        filter: str = "",
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {
            "per_page": max(1, min(limit, 100)),
            "page": max(1, page),
        }
        if filter:
            query["filter"] = filter
        return self._request(
            "GET",
            f"/{self.administration_id}/recurring_sales_invoices.json",
            query=query,
        )

    def create_ledger_account(
        self,
        ledger_account: dict[str, Any],
        *,
        rgs_code: str = "",
    ) -> dict[str, Any]:
        body = {"ledger_account": ledger_account}
        if rgs_code:
            body["rgs_code"] = rgs_code
        return self._request(
            "POST",
            f"/{self.administration_id}/ledger_accounts.json",
            body=body,
        )

    def create_general_journal_document(
        self,
        general_journal_document: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/{self.administration_id}/documents/general_journal_documents.json",
            body={"general_journal_document": general_journal_document},
        )




def get_client(*, require_administration: bool = True) -> MoneybirdClient:
    # The active deployment mode decides whether credentials come from a local
    # source or from trusted request context; strict network modes never mix them.
    credentials = resolve_credentials()
    client = MoneybirdClient(
        token=credentials.token,
        administration_id=credentials.administration_id,
        require_administration=require_administration,
    )
    # Publish the (possibly auto-selected) administration so lower layers such as the
    # audit log scope themselves to this tenant.
    set_active_administration_id(client.administration_id)
    return client
