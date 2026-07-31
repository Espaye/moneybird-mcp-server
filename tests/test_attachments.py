"""Attachment download + PDF text extraction: client redirect safety, extraction, tool."""
from __future__ import annotations

import email.message
import io
import socket
import unittest
import urllib.error
import urllib.request
from unittest import mock

from moneybird.attachments import (
    extract_pdf_text,
    looks_like_pdf,
    safe_attachment_filename,
)


def _pypdf_available() -> bool:
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True


def _minimal_pdf(text: str) -> bytes:
    """Build a tiny single-page PDF with a real text layer (no dependencies)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{index} 0 obj\n".encode("ascii"))
        out.write(body)
        out.write(b"\nendobj\n")
    xref_position = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode("ascii"))
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_position}\n%%EOF\n".encode("ascii")
    )
    return out.getvalue()


class ExtractPdfTextTests(unittest.TestCase):
    def test_non_pdf_bytes_are_rejected_without_pypdf(self) -> None:
        result = extract_pdf_text(b"GIF89a not a pdf")
        self.assertFalse(result["available"])
        self.assertTrue(result["untrusted_content"])
        self.assertIn("not a PDF", result["note"])
        self.assertFalse(looks_like_pdf(b"plain text"))

    def test_oversized_bytes_are_rejected_before_parsing(self) -> None:
        data = _minimal_pdf("sensitive")
        result = extract_pdf_text(data, max_bytes=len(data) - 1)
        self.assertFalse(result["available"])
        self.assertTrue(result["untrusted_content"])
        self.assertIn("too large", result["note"])

    def test_unexpected_content_type_is_rejected_before_parsing(self) -> None:
        result = extract_pdf_text(
            _minimal_pdf("sensitive"),
            content_type="text/html; charset=utf-8",
        )
        self.assertFalse(result["available"])
        self.assertTrue(result["untrusted_content"])
        self.assertIn("content type", result["note"])

    def test_limits_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            extract_pdf_text(_minimal_pdf("x"), max_pages=0)

    def test_text_layer_is_extracted(self) -> None:
        if not _pypdf_available():
            self.skipTest("pypdf not installed")
        result = extract_pdf_text(_minimal_pdf("Stroom 40,00 Gas 60,00"))
        self.assertTrue(result["available"], result.get("note"))
        self.assertIn("Stroom 40,00", result["text"])
        self.assertEqual(result["page_count"], 1)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["isolation"], "worker_process")
        self.assertEqual(result["timeout_seconds"], 10.0)
        self.assertEqual(result["worker_memory_limit_bytes"], 256 * 1024 * 1024)

    def test_long_text_is_truncated(self) -> None:
        if not _pypdf_available():
            self.skipTest("pypdf not installed")
        result = extract_pdf_text(_minimal_pdf("A" * 200), max_chars=50)
        self.assertTrue(result["available"])
        self.assertEqual(len(result["text"]), 50)
        self.assertTrue(result["truncated"])

    def test_malformed_pdf_reports_parse_failure(self) -> None:
        if not _pypdf_available():
            self.skipTest("pypdf not installed")
        result = extract_pdf_text(b"%PDF-1.4 garbage without structure")
        self.assertFalse(result["available"])
        self.assertIn("note", result)

    def test_parser_worker_is_terminated_at_deadline(self) -> None:
        result = extract_pdf_text(
            _minimal_pdf("deadline"),
            timeout_seconds=0.000001,
        )
        self.assertFalse(result["available"])
        self.assertEqual(result["isolation"], "worker_process")
        self.assertIn("time limit", result["note"])

    def test_parser_worker_failure_is_closed_and_joined(self) -> None:
        parent_connection = mock.Mock()
        parent_connection.poll.return_value = True
        parent_connection.recv.side_effect = EOFError
        child_connection = mock.Mock()
        process = mock.Mock()
        process.exitcode = 17
        process.is_alive.return_value = False
        context = mock.Mock()
        context.Pipe.return_value = (parent_connection, child_connection)
        context.Process.return_value = process

        with mock.patch(
            "moneybird.attachments.multiprocessing.get_context",
            return_value=context,
        ):
            result = extract_pdf_text(_minimal_pdf("worker failure"))

        self.assertFalse(result["available"])
        self.assertEqual(result["isolation"], "worker_process")
        self.assertIn("failed safely", result["note"])
        process.start.assert_called_once()
        parent_connection.close.assert_called_once()
        child_connection.close.assert_called_once()
        process.join.assert_called_once_with(2)

    def test_safe_attachment_filename(self) -> None:
        self.assertEqual(
            safe_attachment_filename("Termijnnota juli / 2026 €.pdf"),
            "Termijnnota_juli_2026_.pdf",
        )
        self.assertEqual(safe_attachment_filename(""), "attachment")
        self.assertEqual(safe_attachment_filename("../../etc/passwd"), "etc_passwd")


class BinaryRequestRedirectTests(unittest.TestCase):
    """The signed-storage redirect must be fetched WITHOUT the bearer token."""

    class _FakeResponse:
        def __init__(self, data: bytes, content_type: str) -> None:
            self.headers = {"Content-Type": content_type}
            self._data = data

        def read(self, amount: int | None = None) -> bytes:
            if amount is None:
                return self._data
            return self._data[:amount]

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def test_redirect_drops_authorization_header(self) -> None:
        from moneybird.client import MoneybirdClient

        client = MoneybirdClient("secret-token", "123")
        redirect_headers = email.message.Message()
        redirect_headers["Location"] = "https://storage.example/signed/abc?sig=1"
        first_requests: list[urllib.request.Request] = []
        signed_requests: list[urllib.request.Request] = []

        class FakeOpener:
            def open(self, request, timeout=None):
                if request.has_header("Authorization"):
                    first_requests.append(request)
                    raise urllib.error.HTTPError(
                        request.full_url,
                        302,
                        "Found",
                        redirect_headers,
                        io.BytesIO(b""),
                    )
                signed_requests.append(request)
                return BinaryRequestRedirectTests._FakeResponse(
                    b"%PDF-fake",
                    "application/pdf",
                )

        with (
            mock.patch.object(
                urllib.request,
                "build_opener",
                return_value=FakeOpener(),
            ) as build_opener,
            mock.patch(
                "moneybird.client.socket.getaddrinfo",
                return_value=[
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("93.184.216.34", 443),
                    )
                ],
            ),
        ):
            data, content_type = client.download_attachment(
                "purchase_invoice", "456", "789"
            )

        self.assertEqual(data, b"%PDF-fake")
        self.assertEqual(content_type, "application/pdf")
        self.assertTrue(first_requests[0].has_header("Authorization"))
        self.assertEqual(
            signed_requests[0].full_url, "https://storage.example/signed/abc?sig=1"
        )
        self.assertFalse(
            signed_requests[0].has_header("Authorization"),
            "bearer token must not be forwarded to the storage host",
        )
        self.assertEqual(build_opener.call_count, 2)
        signed_handlers = build_opener.call_args_list[1].args
        pinned_handler = next(
            handler
            for handler in signed_handlers
            if handler.__class__.__name__ == "_PinnedHTTPSHandler"
        )
        self.assertEqual(
            pinned_handler._pinned_addresses,
            ("93.184.216.34",),
        )

    def test_second_redirect_from_signed_storage_is_refused(self) -> None:
        from moneybird.client import MoneybirdClient
        from moneybird.config import MoneybirdError

        client = MoneybirdClient("secret-token", "123")
        first_headers = email.message.Message()
        first_headers["Location"] = "https://storage.example/signed/abc?sig=1"
        second_headers = email.message.Message()
        second_headers["Location"] = "https://127.0.0.1/private"

        class FakeOpener:
            def open(self, request, timeout=None):
                headers = (
                    first_headers if request.has_header("Authorization") else second_headers
                )
                raise urllib.error.HTTPError(
                    request.full_url,
                    302,
                    "Found",
                    headers,
                    io.BytesIO(b""),
                )

        with (
            mock.patch.object(urllib.request, "build_opener", return_value=FakeOpener()),
            mock.patch(
                "moneybird.client.socket.getaddrinfo",
                return_value=[
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("93.184.216.34", 443),
                    )
                ],
            ),
        ):
            with self.assertRaisesRegex(MoneybirdError, "second redirect"):
                client.download_attachment("purchase_invoice", "456", "789")

    def test_bounded_reader_rejects_declared_and_streamed_overflow(self) -> None:
        from moneybird.client import _read_bounded_response
        from moneybird.config import MoneybirdError

        declared = self._FakeResponse(b"1234", "application/pdf")
        declared.headers["Content-Length"] = "5"
        with self.assertRaisesRegex(MoneybirdError, "download limit"):
            _read_bounded_response(declared, max_bytes=4)

        streamed = self._FakeResponse(b"12345", "application/pdf")
        with self.assertRaisesRegex(MoneybirdError, "download limit"):
            _read_bounded_response(streamed, max_bytes=4)

    def test_redirect_policy_rejects_non_https_and_private_addresses(self) -> None:
        from moneybird.client import _validated_attachment_redirect
        from moneybird.config import MoneybirdError

        with self.assertRaisesRegex(MoneybirdError, "credential-free HTTPS"):
            _validated_attachment_redirect(
                "https://moneybird.com/api/v2/123/file",
                "http://storage.example/file",
            )
        with mock.patch(
            "moneybird.client.socket.getaddrinfo",
            return_value=[
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("127.0.0.1", 443),
                )
            ],
        ):
            with self.assertRaisesRegex(MoneybirdError, "non-public"):
                _validated_attachment_redirect(
                    "https://moneybird.com/api/v2/123/file",
                    "https://storage.example/file",
                )

    def test_pinned_connection_uses_numeric_tcp_target_and_original_tls_name(self) -> None:
        from moneybird.client import _PinnedHTTPSConnection

        raw_socket = mock.Mock()
        tls_socket = object()
        context = mock.Mock()
        context.wrap_socket.return_value = tls_socket
        connection = _PinnedHTTPSConnection(
            "storage.example",
            ("93.184.216.34",),
            context=context,
            timeout=7,
        )

        with mock.patch("moneybird.client.socket.socket", return_value=raw_socket):
            connection.connect()

        raw_socket.connect.assert_called_once_with(("93.184.216.34", 443))
        context.wrap_socket.assert_called_once_with(
            raw_socket,
            server_hostname="storage.example",
        )
        self.assertIs(connection.sock, tls_socket)

    def test_pinned_handler_uses_supported_https_handler_api(self) -> None:
        from moneybird.client import _PinnedHTTPSConnection, _PinnedHTTPSHandler

        handler = _PinnedHTTPSHandler(("93.184.216.34",))
        request = urllib.request.Request("https://storage.example/file")
        with mock.patch.object(handler, "do_open", return_value="response") as do_open:
            self.assertEqual(handler.https_open(request), "response")

        factory = do_open.call_args.args[0]
        connection = factory("storage.example", timeout=5)
        self.assertIsInstance(connection, _PinnedHTTPSConnection)
        self.assertEqual(connection._pinned_addresses, ("93.184.216.34",))
        self.assertTrue(do_open.call_args.kwargs["context"].check_hostname)

    def test_mixed_public_private_dns_answers_fail_closed(self) -> None:
        from moneybird.client import _validated_attachment_redirect_target
        from moneybird.config import MoneybirdError

        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ]
        with (
            mock.patch("moneybird.client.socket.getaddrinfo", return_value=answers),
            self.assertRaisesRegex(MoneybirdError, "non-public"),
        ):
            _validated_attachment_redirect_target(
                "https://moneybird.com/api/v2/123/file",
                "https://storage.example/file",
            )

    def test_non_public_and_malformed_dns_answers_fail_closed(self) -> None:
        from moneybird.client import _validated_attachment_redirect_target
        from moneybird.config import MoneybirdError

        addresses = [
            "127.0.0.1",
            "10.0.0.1",
            "169.254.1.2",
            "192.0.2.1",
            "224.0.0.1",
            "::1",
            "fe80::1",
            "fec0::1",
            "ff02::1",
            "4000::1",
            "not-an-address",
        ]
        for address in addresses:
            with self.subTest(address=address):
                answers = [
                    (
                        socket.AF_INET6 if ":" in address else socket.AF_INET,
                        socket.SOCK_STREAM,
                        6,
                        "",
                        (address, 443),
                    )
                ]
                with (
                    mock.patch(
                        "moneybird.client.socket.getaddrinfo",
                        return_value=answers,
                    ),
                    self.assertRaises(MoneybirdError),
                ):
                    _validated_attachment_redirect_target(
                        "https://moneybird.com/api/v2/123/file",
                        "https://storage.example/file",
                    )

    def test_redirect_rejects_credentials_fragment_and_nondefault_port(self) -> None:
        from moneybird.client import _validated_attachment_redirect_target
        from moneybird.config import MoneybirdError

        locations = [
            "http://storage.example/file",
            "file:///tmp/file.pdf",
            "https://user:pass@storage.example/file",
            "https://storage.example/file#fragment",
            "https://storage.example:8443/file",
            "https://storage.example:not-a-port/file",
        ]
        for location in locations:
            with (
                self.subTest(location=location),
                self.assertRaises(MoneybirdError),
            ):
                _validated_attachment_redirect_target(
                    "https://moneybird.com/api/v2/123/file",
                    location,
                )

    def test_direct_response_returns_bytes(self) -> None:
        from moneybird.client import MoneybirdClient

        client = MoneybirdClient("secret-token", "123")

        class FakeOpener:
            def open(self, request, timeout=None):
                return BinaryRequestRedirectTests._FakeResponse(
                    b"raw-bytes", "application/pdf"
                )

        with mock.patch.object(urllib.request, "build_opener", return_value=FakeOpener()):
            data, content_type = client.download_attachment(
                "purchase_invoice", "456", "789"
            )
        self.assertEqual(data, b"raw-bytes")
        self.assertEqual(content_type, "application/pdf")


class ReadDocumentAttachmentToolTests(unittest.TestCase):
    class FakeClient:
        administration_id = "123"

        def __init__(self, attachments: list[dict] | None = None) -> None:
            self.attachments = (
                attachments
                if attachments is not None
                else [
                    {
                        "id": "789",
                        "filename": "Termijnnota juli.pdf",
                        "content_type": "application/pdf",
                        "size": 999,
                    }
                ]
            )

        def get_document(self, kind, document_id):
            return {
                "id": document_id,
                "reference": "1000000002",
                "attachments": self.attachments,
            }

        def download_attachment(self, kind, document_id, attachment_id):
            return _minimal_pdf("Stroom 40,00"), "application/pdf"

    def _call(self, fake, **kwargs):
        from moneybird import tools
        from moneybird.credentials import set_active_administration_id
        from moneybird.tools import _context as tool_context

        def get_fake_client(*args, **_kwargs):
            set_active_administration_id(fake.administration_id)
            return fake

        with mock.patch.object(
            tool_context,
            "get_client",
            side_effect=get_fake_client,
        ):
            result = tools.read_document_attachment(**kwargs)
        return result

    def test_single_attachment_is_downloaded_without_retention(self) -> None:
        result = self._call(self.FakeClient(), document_id="456")
        self.assertEqual(result["attachment"]["id"], "789")
        self.assertEqual(result["content_type"], "application/pdf")
        self.assertEqual(result["retention"], "none")
        self.assertNotIn("saved_path", result)
        self.assertTrue(result["text"]["untrusted_content"])
        if _pypdf_available():
            self.assertTrue(result["text"]["available"])
            self.assertIn("Stroom 40,00", result["text"]["text"])

    def test_hosted_mode_refuses_pdf_parsing_without_capacity_controls(self) -> None:
        import os

        from moneybird import tools
        from moneybird.config import MoneybirdError
        from moneybird.credentials import CREDENTIAL_MODE_ENV
        from moneybird.tools import _context as tool_context

        with (
            mock.patch.dict(
                os.environ,
                {CREDENTIAL_MODE_ENV: "hosted_request_only"},
                clear=False,
            ),
            mock.patch.object(tool_context, "get_client") as get_client,
        ):
            with self.assertRaisesRegex(MoneybirdError, "durable capacity"):
                tools.read_document_attachment(document_id="456")
        get_client.assert_not_called()

    def test_multiple_attachments_without_id_returns_listing(self) -> None:
        fake = self.FakeClient(
            attachments=[
                {"id": "789", "filename": "a.pdf", "content_type": "application/pdf", "size": 1},
                {"id": "790", "filename": "b.pdf", "content_type": "application/pdf", "size": 2},
            ]
        )
        result = self._call(fake, document_id="456")
        self.assertEqual(len(result["attachments"]), 2)
        self.assertIn("attachment_id", result["note"])
        self.assertNotIn("saved_path", result)

    def test_declared_oversize_is_rejected_before_download(self) -> None:
        from moneybird.attachments import DEFAULT_MAX_ATTACHMENT_BYTES
        from moneybird.config import MoneybirdError

        fake = self.FakeClient(
            attachments=[
                {
                    "id": "789",
                    "filename": "large.pdf",
                    "content_type": "application/pdf",
                    "size": DEFAULT_MAX_ATTACHMENT_BYTES + 1,
                }
            ]
        )
        with (
            mock.patch.object(fake, "download_attachment") as download,
            self.assertRaisesRegex(MoneybirdError, "byte limit"),
        ):
            self._call(fake, document_id="456")
        download.assert_not_called()

    def test_no_attachments_is_reported(self) -> None:
        result = self._call(self.FakeClient(attachments=[]), document_id="456")
        self.assertEqual(result["attachments"], [])
        self.assertIn("no attachments", result["note"])

    def test_unknown_attachment_id_returns_listing(self) -> None:
        result = self._call(
            self.FakeClient(), document_id="456", attachment_id="999"
        )
        self.assertIn("not found", result["note"])
        self.assertEqual(result["attachments"][0]["id"], "789")


if __name__ == "__main__":
    unittest.main()
