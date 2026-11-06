"""The embedder surface: what it exports, what it never loads, and the client knobs.

An embedder drives Moneybird through this package's client with its own write
path, so the guarantees it relies on are pinned here rather than left to the
module docstring: importing ``moneybird_mcp.embed`` loads no tools, no approval
executor and no other distribution's write spec, the exported names stay put, and a client built with its own HTTP
client and retry count sends every JSON API request through them.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import unittest
import urllib.error
from unittest import mock

import httpx

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class RecordingHTTPClient:
    """Answers every request with the next queued response and records the call."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append((method, url))
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class EmbedImportTests(unittest.TestCase):
    def test_importing_the_embed_surface_loads_no_tools(self) -> None:
        # A fresh interpreter: by now this suite has imported the tools itself.
        probe = (
            "import json, sys\n"
            "import moneybird_mcp.embed\n"
            "from moneybird_mcp.write_contracts import WRITE_SPEC_REGISTRY\n"
            "print(json.dumps({\n"
            "    'tools': sorted(m for m in sys.modules\n"
            "                    if m == 'moneybird_mcp.tools' or m.startswith('moneybird_mcp.tools.')),\n"
            "    'server': 'moneybird_mcp.server' in sys.modules,\n"
            "    'fastmcp': 'fastmcp' in sys.modules,\n"
            "    'write_spec_origins': list(WRITE_SPEC_REGISTRY.origins()),\n"
            "}))\n"
        )
        completed = subprocess.run(  # noqa: S603 - literal argument list, this interpreter
            [sys.executable, "-c", probe],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            # The core's own write contracts are data and register on import;
            # their executors live in the tools package, which stays unloaded.
            {
                "tools": [],
                "server": False,
                "fastmcp": False,
                "write_spec_origins": ["moneybird-mcp"],
            },
        )


class EmbedSurfaceTests(unittest.TestCase):
    EXPECTED = (
        "BASE_URL",
        "CAPABILITY_SCOPES",
        "CREDENTIAL_MODE_HOSTED_REQUEST_ONLY",
        "CapabilityMode",
        "EMBED_API_VERSION",
        "HTTPClient",
        "INCIDENTAL_ACCESS",
        "MAX_BANK_DESCRIPTION_CHARS",
        "MONEYBIRD_DOCUMENTED_DEFAULT_PERIODS",
        "MONTH_CAPPED_REPORTS",
        "MoneybirdClient",
        "MoneybirdError",
        "MoneybirdHTTPError",
        "OAuthConnection",
        "UNPAID_DOCUMENT_STATES",
        "UNPAID_SALES_INVOICE_STATES",
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
    )

    def test_the_declared_surface_is_exactly_this(self) -> None:
        """Adding a name is a compatible change; removing or renaming one is not."""
        from moneybird_mcp import embed

        self.assertEqual(tuple(sorted(embed.__all__)), tuple(sorted(self.EXPECTED)))
        self.assertEqual(embed.EMBED_API_VERSION, 1)

    def test_every_name_is_the_object_its_owning_module_defines(self) -> None:
        import importlib

        from moneybird_mcp import embed

        for name in embed.__all__:
            if name == "EMBED_API_VERSION":
                continue
            with self.subTest(name=name):
                value = getattr(embed, name)
                owner = getattr(value, "__module__", None)
                if owner is None or not owner.startswith("moneybird_mcp."):
                    continue
                self.assertIs(getattr(importlib.import_module(owner), name), value)


class EmbedderClientTests(unittest.TestCase):
    def test_requests_go_through_the_client_passed_in_not_the_shared_pool(self) -> None:
        import moneybird_mcp.client as client_module

        transport = RecordingHTTPClient(httpx.Response(200, json=[{"id": "1"}]))
        client = client_module.MoneybirdClient("token", "123", http_client=transport)
        with mock.patch.object(client_module, "get_shared_http_client") as shared:
            result = client.request("GET", "/123/contacts.json", {"page": 2})

        shared.assert_not_called()
        self.assertEqual(result, [{"id": "1"}])
        self.assertEqual(len(transport.calls), 1)
        method, url = transport.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/123/contacts.json?page=2"), url)

    def test_administration_auto_selection_already_uses_the_client_passed_in(self) -> None:
        import moneybird_mcp.client as client_module

        transport = RecordingHTTPClient(httpx.Response(200, json=[{"id": "123", "name": "Only"}]))
        with mock.patch.object(client_module, "get_shared_http_client") as shared:
            client = client_module.MoneybirdClient("token", None, http_client=transport)

        shared.assert_not_called()
        self.assertEqual(client.administration_id, "123")
        self.assertTrue(transport.calls[0][1].endswith("/administrations.json"))

    def test_zero_retry_attempts_sends_a_retryable_read_once(self) -> None:
        import moneybird_mcp.client as client_module

        transport = RecordingHTTPClient(httpx.Response(503, text="busy"))
        client = client_module.MoneybirdClient(
            "token", "123", http_client=transport, retry_attempts=0
        )
        with mock.patch.object(client_module.time, "sleep") as sleep:
            with self.assertRaises(client_module.MoneybirdHTTPError):
                client.request("GET", "/123/contacts.json")

        self.assertEqual(len(transport.calls), 1)
        sleep.assert_not_called()

    def test_a_per_client_retry_count_overrides_the_module_default(self) -> None:
        import moneybird_mcp.client as client_module

        transport = RecordingHTTPClient(
            httpx.Response(503, text="busy"),
            httpx.Response(503, text="busy"),
            httpx.Response(200, json={"ok": True}),
        )
        client = client_module.MoneybirdClient(
            "token", "123", http_client=transport, retry_attempts=2
        )
        with (
            mock.patch.object(client_module, "DEFAULT_RETRY_ATTEMPTS", 0),
            mock.patch.object(client_module.time, "sleep"),
        ):
            self.assertEqual(client.request("GET", "/123/contacts.json"), {"ok": True})

        self.assertEqual(len(transport.calls), 3)

    def test_without_a_retry_count_the_module_default_is_read_at_request_time(self) -> None:
        import moneybird_mcp.client as client_module

        transport = RecordingHTTPClient(httpx.Response(503, text="busy"))
        client = client_module.MoneybirdClient("token", "123", http_client=transport)
        with (
            mock.patch.object(client_module, "DEFAULT_RETRY_ATTEMPTS", 1),
            mock.patch.object(client_module.time, "sleep"),
            self.assertRaises(client_module.MoneybirdHTTPError),
        ):
            client.request("GET", "/123/contacts.json")

        self.assertEqual(len(transport.calls), 2)

    def test_a_client_built_without_init_still_uses_the_shared_pool(self) -> None:
        import moneybird_mcp.client as client_module

        client = client_module.MoneybirdClient.__new__(client_module.MoneybirdClient)
        pooled = RecordingHTTPClient(httpx.Response(200, json={}))
        with mock.patch.object(client_module, "get_shared_http_client", return_value=pooled):
            self.assertIs(client._transport(), pooled)
        self.assertEqual(client._retry_limit(), client_module.DEFAULT_RETRY_ATTEMPTS)

    def test_the_retry_count_must_be_a_non_negative_integer(self) -> None:
        import moneybird_mcp.client as client_module

        for invalid in (-1, True, 1.0, "1"):
            with self.subTest(retry_attempts=invalid):
                with self.assertRaises(client_module.MoneybirdError):
                    client_module.MoneybirdClient("token", "123", retry_attempts=invalid)

    def test_request_refuses_a_path_outside_the_administration_before_sending(self) -> None:
        import moneybird_mcp.client as client_module

        transport = RecordingHTTPClient(httpx.Response(200, json={}))
        client = client_module.MoneybirdClient("token", "123", http_client=transport)
        with self.assertRaises(client_module.MoneybirdError):
            client.request("GET", "/456/contacts.json")
        self.assertEqual(transport.calls, [])

    def test_request_does_not_retry_an_ambiguous_write(self) -> None:
        import moneybird_mcp.client as client_module

        class LostResponse(RecordingHTTPClient):
            def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
                self.calls.append((method, url))
                raise httpx.ConnectError("lost response")

        transport = LostResponse()
        client = client_module.MoneybirdClient(
            "token", "123", http_client=transport, retry_attempts=3
        )
        with self.assertRaises(client_module.MoneybirdError) as raised:
            client.request("POST", "/123/contacts.json", body={"contact": {}})
        self.assertEqual(len(transport.calls), 1)
        self.assertIn("ambiguous", str(raised.exception))

    def test_attachment_downloads_honour_the_per_client_retry_count(self) -> None:
        import moneybird_mcp.client as client_module

        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError("unreachable")
        client = client_module.MoneybirdClient("token", "123", retry_attempts=0)
        with (
            mock.patch.object(client_module.urllib.request, "build_opener", return_value=opener),
            mock.patch.object(client_module.time, "sleep") as sleep,
            self.assertRaises(client_module.MoneybirdError),
        ):
            client._binary_request("GET", "/123/documents/1/attachments/2/download")

        self.assertEqual(opener.open.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
