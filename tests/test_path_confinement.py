from __future__ import annotations

import asyncio
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from pydantic import TypeAdapter, ValidationError

import moneybird_mcp.client as client_module
from moneybird_mcp.client import MoneybirdClient
from moneybird_mcp.config import MoneybirdError
from moneybird_mcp.tools import _params, core


class _JsonResponse:
    status_code = 200
    text = '{"ok": true}'
    headers: dict[str, str] = {}


class IdentifierSchemaTests(unittest.TestCase):
    def test_numeric_id_aliases_accept_only_ascii_digits(self) -> None:
        aliases = (
            _params.ContactId,
            _params.SalesInvoiceId,
            _params.FinancialMutationId,
            _params.FinancialAccountId,
        )
        rejected = (
            "",
            " 123",
            "123 ",
            "１２３",
            "../456",
            "%2e%2e",
            "123/456",
            "123\\456",
            "https://example.test",
            "123\x00",
            "123\n",
        )
        for alias in aliases:
            adapter = TypeAdapter(alias)
            self.assertEqual(adapter.validate_python("123456789"), "123456789")
            for value in rejected:
                with self.subTest(alias=alias, value=value):
                    with self.assertRaises(ValidationError):
                        adapter.validate_python(value)

    def test_tool_schemas_expose_id_and_path_constraints(self) -> None:
        from moneybird_mcp.tools import mcp

        archive_schema = asyncio.run(
            mcp.get_tool("prepare_archive_contact")
        ).parameters["properties"]["contact_id"]
        fetch_schema = asyncio.run(mcp.get_tool("fetch")).parameters["properties"]["id"]
        request_schema = asyncio.run(
            mcp.get_tool("moneybird_request")
        ).parameters["properties"]["path"]

        self.assertEqual(archive_schema["pattern"], _params.MONEYBIRD_ID_PATTERN)
        self.assertIn("[0-9]+", fetch_schema["pattern"])
        self.assertIn("administrations", request_schema["pattern"])


class GenericGetAllowlistTests(unittest.TestCase):
    def test_every_allowlisted_template_is_a_vendored_openapi_get(self) -> None:
        spec_path = (
            Path(__file__).resolve().parent.parent
            / "docs"
            / "moneybird_api_paths.json"
        )
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        documented_gets = {
            re.sub(
                r"\{[^}]+\}",
                "{id}",
                path.removeprefix("/{administration_id}/").removeprefix("/"),
            )
            for path, operations in spec["paths"].items()
            if "GET" in operations
        }
        self.assertEqual(
            client_module._SAFE_GENERIC_GET_TEMPLATES - documented_gets,
            set(),
        )


class AdministrationIdConfinementTests(unittest.TestCase):
    def test_constructor_rejects_non_numeric_administration_ids(self) -> None:
        malicious = (
            "admin",
            "１２３",
            "../456",
            "%2e%2e",
            "123/456",
            "123\\456",
            "https://example.test",
            "//example.test",
            "123\x00",
        )
        for value in malicious:
            with self.subTest(value=value):
                with self.assertRaises(MoneybirdError):
                    MoneybirdClient("token", value)

    def test_auto_selected_administration_id_is_validated(self) -> None:
        with mock.patch.object(
            MoneybirdClient,
            "list_administrations",
            return_value=[{"id": "../456", "name": "malicious"}],
        ):
            with self.assertRaises(MoneybirdError):
                MoneybirdClient("token", None)

    def test_numeric_administration_id_is_kept_canonical(self) -> None:
        client = MoneybirdClient("token", "00123")
        self.assertEqual(client.administration_id, "00123")


class ClientPathConfinementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MoneybirdClient("token", "123")

    def test_record_ids_fail_before_the_http_transport(self) -> None:
        pooled_client = mock.Mock()
        calls = (
            lambda: self.client.get_contact("../../456/contacts/789"),
            lambda: self.client.get_sales_invoice("%2e%2e"),
            lambda: self.client.get_document("purchase_invoice", "１２３"),
            lambda: self.client.download_attachment(
                "purchase_invoice",
                "123",
                "1\\2",
            ),
            lambda: self.client.fetch_contacts_by_ids(["123", "../456"]),
        )
        with mock.patch.object(
            client_module,
            "get_shared_http_client",
            return_value=pooled_client,
        ):
            for invoke in calls:
                with self.subTest(invoke=invoke):
                    with self.assertRaises(MoneybirdError):
                        invoke()
        pooled_client.request.assert_not_called()

    def test_private_request_guard_rejects_normalization_and_tenant_escape(self) -> None:
        pooled_client = mock.Mock()
        malicious_paths = (
            "/123/../456/contacts.json",
            "/123/%2e%2e/456/contacts.json",
            "/123/%252e%252e/456/contacts.json",
            "/456/contacts.json",
            "//example.test/contacts.json",
            "https://example.test/contacts.json",
            "/123\\456\\contacts.json",
            "/123/contacts.json?admin=456",
            "/123/contacts.json#fragment",
            "/123/contacts/\x00.json",
            "/123/contacts/%GG.json",
        )
        with mock.patch.object(
            client_module,
            "get_shared_http_client",
            return_value=pooled_client,
        ):
            for path in malicious_paths:
                with self.subTest(path=path):
                    with self.assertRaises(MoneybirdError):
                        self.client._request("GET", path)
        pooled_client.request.assert_not_called()

    def test_generic_get_accepts_allowlisted_relative_templates(self) -> None:
        pooled_client = mock.Mock()
        pooled_client.request.return_value = _JsonResponse()
        with mock.patch.object(
            client_module,
            "get_shared_http_client",
            return_value=pooled_client,
        ):
            result = self.client.raw_get(
                "time_entries/456.json",
                query={"filter": "state:open"},
            )

        self.assertEqual(result, {"ok": True})
        requested_url = pooled_client.request.call_args.args[1]
        self.assertEqual(
            requested_url,
            f"{self.client.base_url}/123/time_entries/456.json?filter=state%3Aopen",
        )

    def test_generic_get_rejects_url_and_path_smuggling(self) -> None:
        pooled_client = mock.Mock()
        malicious_paths = (
            "../456/contacts",
            "%2e%2e/456/contacts",
            "%252e%252e/456/contacts",
            "123/contacts",
            "456/contacts",
            "/contacts",
            "//example.test/contacts",
            "https://example.test/contacts",
            "contacts\\123",
            "contacts/１２３",
            "contacts/123?admin=456",
            "contacts/123#fragment",
            "contacts/\x00",
            "contacts/123/extra",
            "administrations/123",
            "sales_invoices/find_by_reference/INV-1",
        )
        with mock.patch.object(
            client_module,
            "get_shared_http_client",
            return_value=pooled_client,
        ):
            for path in malicious_paths:
                with self.subTest(path=path):
                    with self.assertRaises(MoneybirdError):
                        self.client.raw_get(path)
        pooled_client.request.assert_not_called()

    def test_administrations_is_the_only_root_generic_route(self) -> None:
        client = MoneybirdClient("token", None, require_administration=False)
        pooled_client = mock.Mock()
        pooled_client.request.return_value = _JsonResponse()
        with mock.patch.object(
            client_module,
            "get_shared_http_client",
            return_value=pooled_client,
        ):
            self.assertEqual(client.raw_get("administrations"), {"ok": True})
            with self.assertRaises(MoneybirdError):
                client.raw_get("contacts")

        requested_url = pooled_client.request.call_args_list[0].args[1]
        self.assertEqual(
            requested_url,
            f"{client.base_url}/administrations.json",
        )
        self.assertEqual(pooled_client.request.call_count, 1)

    def test_human_lookup_values_are_encoded_as_route_data(self) -> None:
        pooled_client = mock.Mock()
        pooled_client.request.return_value = _JsonResponse()
        with mock.patch.object(
            client_module,
            "get_shared_http_client",
            return_value=pooled_client,
        ):
            self.client.get_contact_by_customer_id("CUST/42 #é")
            self.client.get_sales_invoice_by_reference("INV/2026 #é")

        urls = [call.args[1] for call in pooled_client.request.call_args_list]
        self.assertEqual(
            urls[0],
            (
                f"{self.client.base_url}/123/contacts/customer_id/"
                "CUST%2F42%20%23%C3%A9.json"
            ),
        )
        self.assertEqual(
            urls[1],
            (
                f"{self.client.base_url}/123/sales_invoices/find_by_reference/"
                "INV%2F2026%20%23%C3%A9.json"
            ),
        )

    def test_encoded_human_lookup_cannot_introduce_dot_segments(self) -> None:
        pooled_client = mock.Mock()
        with mock.patch.object(
            client_module,
            "get_shared_http_client",
            return_value=pooled_client,
        ):
            with self.assertRaises(MoneybirdError):
                self.client.get_contact_by_customer_id("../../456/contacts")
        pooled_client.request.assert_not_called()


class CorePathValidationTests(unittest.TestCase):
    def test_moneybird_request_rejects_before_resolving_credentials(self) -> None:
        with mock.patch.object(core.ctx, "get_client") as get_client:
            with self.assertRaises(MoneybirdError):
                core.moneybird_request("../456/contacts")
        get_client.assert_not_called()

    def test_administration_root_decision_uses_the_validated_route(self) -> None:
        fake_client = mock.Mock()
        fake_client.raw_get.return_value = [{"id": "123"}]
        with mock.patch.object(
            core.ctx,
            "get_client",
            return_value=fake_client,
        ) as get_client:
            result = core.moneybird_request("administrations.json")

        get_client.assert_called_once_with(require_administration=False)
        fake_client.raw_get.assert_called_once_with("administrations", query=None)
        self.assertEqual(result["path"], "administrations")

    def test_fetch_rejects_non_numeric_record_id_before_client_resolution(self) -> None:
        with mock.patch.object(core.ctx, "get_client") as get_client:
            with self.assertRaises(MoneybirdError):
                core.fetch("contact:../../456")
        get_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
