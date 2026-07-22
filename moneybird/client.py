"""Thin HTTP client for the Moneybird REST API, with retry/backoff."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

from .config import (
    BASE_URL,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_RETRY_DELAY_SECONDS,
    MoneybirdError,
    PAGINATED_REPORTS,
    REPORT_ENDPOINTS,
    REPORT_PERIOD_PARAM_OVERRIDES,
    RETRYABLE_HTTP_STATUS_CODES,
)
from .credentials import resolve_credentials, set_active_administration_id
from .formatting import (
    build_filter_string,
    document_kind_config,
)

import logging
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


_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}




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
                "MONEYBIRD_ACCESS_TOKEN is missing. Set it in your environment or .env file."
            )

        self.token = token
        self.base_url = BASE_URL.rstrip("/")
        self.timeout = DEFAULT_TIMEOUT_SECONDS
        if administration_id:
            self.administration_id = administration_id
        elif require_administration:
            self.administration_id = self._auto_select_administration()
        else:
            self.administration_id = None

    def _request(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        *,
        retry_safe: bool | None = None,
    ) -> Any:
        method = method.upper()
        if retry_safe is None:
            retry_safe = method in {"GET", "HEAD", "OPTIONS"}
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query, doseq=True)}"
        serialized_body = json.dumps(body).encode("utf-8") if body is not None else None

        for attempt in range(DEFAULT_RETRY_ATTEMPTS + 1):
            request = urllib.request.Request(url=url, method=method)
            request.add_header("Authorization", f"Bearer {self.token}")
            request.add_header("Accept", "application/json")
            if serialized_body is not None:
                request.data = serialized_body
                request.add_header("Content-Type", "application/json")

            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = response.read().decode("utf-8")
                return json.loads(payload) if payload else None
            except urllib.error.HTTPError as exc:
                body_text = exc.read().decode("utf-8", errors="replace")
                if (
                    retry_safe
                    and attempt < DEFAULT_RETRY_ATTEMPTS
                    and is_retryable_http_status(exc.code)
                ):
                    delay = retry_delay_seconds(
                        attempt=attempt,
                        retry_after_header=exc.headers.get("Retry-After"),
                    )
                    logger.warning(
                        "Retrying Moneybird %s %s after HTTP %s in %.1fs (attempt %s/%s)",
                        method,
                        path,
                        exc.code,
                        delay,
                        attempt + 1,
                        DEFAULT_RETRY_ATTEMPTS,
                    )
                    time.sleep(delay)
                    continue
                retry_note = (
                    " Automatic retry was disabled because this write may already have "
                    "been processed; reconcile the record before retrying."
                    if not retry_safe and is_retryable_http_status(exc.code)
                    else ""
                )
                raise MoneybirdError(
                    f"Moneybird returned HTTP {exc.code} for {path}: {body_text}{retry_note}"
                ) from exc
            except urllib.error.URLError as exc:
                if retry_safe and attempt < DEFAULT_RETRY_ATTEMPTS:
                    delay = retry_delay_seconds(attempt=attempt)
                    logger.warning(
                        "Retrying Moneybird %s %s after network error in %.1fs (attempt %s/%s): %s",
                        method,
                        path,
                        delay,
                        attempt + 1,
                        DEFAULT_RETRY_ATTEMPTS,
                        exc.reason,
                    )
                    time.sleep(delay)
                    continue
                retry_note = (
                    " The write result is ambiguous; reconcile Moneybird before retrying."
                    if not retry_safe
                    else ""
                )
                raise MoneybirdError(
                    f"Could not reach Moneybird: {exc.reason}.{retry_note}"
                ) from exc

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

    def list_contacts(self, *, limit: int = 10, page: int = 1) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/{self.administration_id}/contacts.json",
            {"per_page": max(1, min(limit, 100)), "page": max(1, page)},
        )

    def get_contact(self, contact_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/{self.administration_id}/contacts/{contact_id}.json",
        )

    def get_contact_by_customer_id(self, customer_id: str) -> dict[str, Any]:
        encoded_customer_id = urllib.parse.quote(str(customer_id), safe="")
        return self._request(
            "GET",
            f"/{self.administration_id}/contacts/customer_id/{encoded_customer_id}.json",
        )

    def create_contact(self, contact: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/{self.administration_id}/contacts.json",
            body={"contact": contact},
        )

    def update_contact(self, contact_id: str, contact: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/{self.administration_id}/contacts/{contact_id}.json",
            body={"contact": contact},
        )

    def archive_contact(self, contact_id: str) -> None:
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
        return self._request(
            "PATCH",
            f"/{self.administration_id}/sales_invoices/{sales_invoice_id}/send_invoice.json",
            body={"sales_invoice_sending": sales_invoice_sending},
        )

    def pause_sales_invoice(self, sales_invoice_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/{self.administration_id}/sales_invoices/{sales_invoice_id}/pause.json",
        )

    def resume_sales_invoice(self, sales_invoice_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/{self.administration_id}/sales_invoices/{sales_invoice_id}/resume.json",
        )

    def get_sales_invoice_by_reference(self, reference: str) -> dict[str, Any]:
        encoded_reference = urllib.parse.quote(str(reference), safe="")
        return self._request(
            "GET",
            f"/{self.administration_id}/sales_invoices/find_by_reference/{encoded_reference}.json",
        )

    def get_sales_invoice_by_invoice_id(self, invoice_id: str) -> dict[str, Any]:
        encoded_invoice_id = urllib.parse.quote(str(invoice_id), safe="")
        return self._request(
            "GET",
            f"/{self.administration_id}/sales_invoices/find_by_invoice_id/{encoded_invoice_id}.json",
        )

    def get_recurring_sales_invoice(self, recurring_sales_invoice_id: str) -> dict[str, Any]:
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
        wanted = str(financial_account_id)
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
        """Perform a read-only GET against an arbitrary Moneybird endpoint.

        ``path`` is treated as relative to the configured administration
        (e.g. ``"estimates"`` or ``"time_entries/123"``). Paths targeting
        ``administrations`` are served from the API root. A ``?query`` suffix in
        ``path`` is merged into the ``query`` mapping. Only GET is performed, so
        this can never mutate data.
        """
        raw = str(path).strip()
        if "?" in raw:
            raw, _, query_string = raw.partition("?")
            merged = dict(urllib.parse.parse_qsl(query_string))
            if query:
                merged.update(query)
            query = merged
        raw = raw.strip("/")
        if not raw:
            raise MoneybirdError("raw_get requires a non-empty path.")
        if raw == "administrations" or raw.startswith("administrations/"):
            endpoint = f"/{raw}"
        elif self.administration_id and raw.startswith(f"{self.administration_id}/"):
            endpoint = f"/{raw}"
        else:
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
        url = f"{self.base_url}{path}"
        opener = urllib.request.build_opener(_StopRedirects)
        for attempt in range(DEFAULT_RETRY_ATTEMPTS + 1):
            request = urllib.request.Request(url=url, method=method)
            request.add_header("Authorization", f"Bearer {self.token}")
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    content_type = response.headers.get("Content-Type") or "application/octet-stream"
                    return response.read(), content_type
            except urllib.error.HTTPError as exc:
                if exc.code in _REDIRECT_STATUS_CODES:
                    location = exc.headers.get("Location")
                    if not location:
                        raise MoneybirdError(
                            f"Moneybird redirected {path} without a Location header."
                        ) from exc
                    signed_url = urllib.parse.urljoin(url, location)
                    signed_request = urllib.request.Request(url=signed_url, method="GET")
                    try:
                        with urllib.request.urlopen(
                            signed_request, timeout=self.timeout
                        ) as response:
                            content_type = (
                                response.headers.get("Content-Type")
                                or "application/octet-stream"
                            )
                            return response.read(), content_type
                    except urllib.error.HTTPError as signed_exc:
                        raise MoneybirdError(
                            f"Attachment storage returned HTTP {signed_exc.code} for {path}."
                        ) from signed_exc
                if attempt < DEFAULT_RETRY_ATTEMPTS and is_retryable_http_status(exc.code):
                    time.sleep(
                        retry_delay_seconds(
                            attempt=attempt,
                            retry_after_header=exc.headers.get("Retry-After"),
                        )
                    )
                    continue
                body_text = exc.read().decode("utf-8", errors="replace")
                raise MoneybirdError(
                    f"Moneybird returned HTTP {exc.code} for {path}: {body_text}"
                ) from exc
            except urllib.error.URLError as exc:
                if attempt < DEFAULT_RETRY_ATTEMPTS:
                    time.sleep(retry_delay_seconds(attempt=attempt))
                    continue
                raise MoneybirdError(f"Could not reach Moneybird: {exc.reason}.") from exc

    def download_attachment(
        self,
        kind: str,
        document_id: str,
        attachment_id: str,
    ) -> tuple[bytes, str]:
        """Download a document attachment's raw bytes; returns (data, content_type)."""
        config = document_kind_config(kind)
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
        return self._request(
            "PATCH",
            f"/{self.administration_id}/financial_mutations/{mutation_id}/link_booking.json",
            body=booking,
        )

    def unlink_financial_mutation_booking(
        self,
        mutation_id: str,
        *,
        booking_type: str,
        booking_id: str,
    ) -> Any:
        return self._request(
            "DELETE",
            f"/{self.administration_id}/financial_mutations/{mutation_id}/unlink_booking.json",
            body={"booking_type": booking_type, "booking_id": booking_id},
        )

    def duplicate_sales_invoice_to_credit_invoice(
        self,
        sales_invoice_id: str,
    ) -> dict[str, Any]:
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
    # Credentials are resolved per request: an X-Moneybird-Token header (multi-tenant)
    # takes precedence, falling back to the environment for single-user / local use.
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
