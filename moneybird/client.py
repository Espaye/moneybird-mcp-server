"""Thin HTTP client for the Moneybird REST API, with retry/backoff."""
from __future__ import annotations

import json
import time
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import (
    BASE_URL,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    MoneybirdError,
    REPORT_ENDPOINTS,
    RETRYABLE_HTTP_STATUS_CODES,
)
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
    if retry_after_header:
        try:
            parsed = float(retry_after_header)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            pass
    return DEFAULT_RETRY_BACKOFF_SECONDS * (2**attempt)




def is_retryable_http_status(status_code: int) -> bool:
    return status_code in RETRYABLE_HTTP_STATUS_CODES




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
    ) -> Any:
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
                if attempt < DEFAULT_RETRY_ATTEMPTS and is_retryable_http_status(exc.code):
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
                raise MoneybirdError(
                    f"Moneybird returned HTTP {exc.code} for {path}: {body_text}"
                ) from exc
            except urllib.error.URLError as exc:
                if attempt < DEFAULT_RETRY_ATTEMPTS:
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
                raise MoneybirdError(f"Could not reach Moneybird: {exc.reason}") from exc

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
        return self._request(
            "GET",
            f"/{self.administration_id}/financial_accounts/{financial_account_id}.json",
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
        )

    def get_report(self, report_name: str, *, period: str) -> dict[str, Any]:
        endpoint = REPORT_ENDPOINTS.get(str(report_name).strip())
        if not endpoint:
            supported = ", ".join(sorted(REPORT_ENDPOINTS))
            raise MoneybirdError(
                f"Unsupported report '{report_name}'. Use one of: {supported}."
            )
        return self._request(
            "GET",
            f"/{self.administration_id}/reports/{endpoint}.json",
            query={"period": period},
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
    return MoneybirdClient(
        token=os.environ.get("MONEYBIRD_ACCESS_TOKEN", "").strip(),
        administration_id=os.environ.get("MONEYBIRD_ADMINISTRATION_ID", "").strip() or None,
        require_administration=require_administration,
    )
