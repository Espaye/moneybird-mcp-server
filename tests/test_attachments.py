"""Attachment download + PDF text extraction: client redirect safety, extraction, tool."""
from __future__ import annotations

import email.message
import io
import os
import tempfile
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
        self.assertIn("not a PDF", result["note"])
        self.assertFalse(looks_like_pdf(b"plain text"))

    def test_text_layer_is_extracted(self) -> None:
        if not _pypdf_available():
            self.skipTest("pypdf not installed")
        result = extract_pdf_text(_minimal_pdf("Stroom 40,00 Gas 60,00"))
        self.assertTrue(result["available"], result.get("note"))
        self.assertIn("Stroom 40,00", result["text"])
        self.assertEqual(result["page_count"], 1)
        self.assertFalse(result["truncated"])

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

        def read(self) -> bytes:
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def test_redirect_drops_authorization_header(self) -> None:
        from moneybird.client import MoneybirdClient

        client = MoneybirdClient("secret-token", "admin-1")
        redirect_headers = email.message.Message()
        redirect_headers["Location"] = "https://storage.example/signed/abc?sig=1"
        first_requests: list[urllib.request.Request] = []
        signed_requests: list[urllib.request.Request] = []

        class FakeOpener:
            def open(self, request, timeout=None):
                first_requests.append(request)
                raise urllib.error.HTTPError(
                    request.full_url, 302, "Found", redirect_headers, io.BytesIO(b"")
                )

        def fake_urlopen(request, timeout=None):
            signed_requests.append(request)
            return BinaryRequestRedirectTests._FakeResponse(b"%PDF-fake", "application/pdf")

        with (
            mock.patch.object(urllib.request, "build_opener", return_value=FakeOpener()),
            mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen),
        ):
            data, content_type = client.download_attachment(
                "purchase_invoice", "doc-1", "att-1"
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

    def test_direct_response_returns_bytes(self) -> None:
        from moneybird.client import MoneybirdClient

        client = MoneybirdClient("secret-token", "admin-1")

        class FakeOpener:
            def open(self, request, timeout=None):
                return BinaryRequestRedirectTests._FakeResponse(
                    b"raw-bytes", "application/pdf"
                )

        with mock.patch.object(urllib.request, "build_opener", return_value=FakeOpener()):
            data, content_type = client.download_attachment(
                "purchase_invoice", "doc-1", "att-1"
            )
        self.assertEqual(data, b"raw-bytes")
        self.assertEqual(content_type, "application/pdf")


class ReadDocumentAttachmentToolTests(unittest.TestCase):
    class FakeClient:
        administration_id = "admin"

        def __init__(self, attachments: list[dict] | None = None) -> None:
            self.attachments = (
                attachments
                if attachments is not None
                else [
                    {
                        "id": "att-1",
                        "filename": "Termijnnota juli.pdf",
                        "content_type": "application/pdf",
                        "size": 999,
                    }
                ]
            )

        def get_document(self, kind, document_id):
            return {
                "id": document_id,
                "reference": "1168011272",
                "attachments": self.attachments,
            }

        def download_attachment(self, kind, document_id, attachment_id):
            return _minimal_pdf("Stroom 40,00"), "application/pdf"

    def _call(self, fake, **kwargs):
        from moneybird import tools
        from moneybird.tools import _context as tool_context
        from moneybird.credentials import set_active_administration_id

        def get_fake_client(*args, **_kwargs):
            set_active_administration_id(fake.administration_id)
            return fake

        with (
            mock.patch.object(tool_context, "get_client", side_effect=get_fake_client),
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"MONEYBIRD_MCP_DATA_DIR": tmp}),
        ):
            result = tools.read_document_attachment(**kwargs)
            saved = result.get("saved_path")
            if saved:
                # Assert while the temp dir still exists.
                self.assertTrue(os.path.exists(saved))
                result["_saved_file_size"] = os.path.getsize(saved)
        return result

    def test_single_attachment_is_downloaded_and_saved(self) -> None:
        result = self._call(self.FakeClient(), document_id="doc-1")
        self.assertEqual(result["attachment"]["id"], "att-1")
        self.assertEqual(result["content_type"], "application/pdf")
        self.assertIn("Termijnnota_juli.pdf", result["saved_path"])
        self.assertGreater(result["_saved_file_size"], 0)
        if _pypdf_available():
            self.assertTrue(result["text"]["available"])
            self.assertIn("Stroom 40,00", result["text"]["text"])

    def test_multiple_attachments_without_id_returns_listing(self) -> None:
        fake = self.FakeClient(
            attachments=[
                {"id": "att-1", "filename": "a.pdf", "content_type": "application/pdf", "size": 1},
                {"id": "att-2", "filename": "b.pdf", "content_type": "application/pdf", "size": 2},
            ]
        )
        result = self._call(fake, document_id="doc-1")
        self.assertEqual(len(result["attachments"]), 2)
        self.assertIn("attachment_id", result["note"])
        self.assertNotIn("saved_path", result)

    def test_no_attachments_is_reported(self) -> None:
        result = self._call(self.FakeClient(attachments=[]), document_id="doc-1")
        self.assertEqual(result["attachments"], [])
        self.assertIn("no attachments", result["note"])

    def test_unknown_attachment_id_returns_listing(self) -> None:
        result = self._call(
            self.FakeClient(), document_id="doc-1", attachment_id="does-not-exist"
        )
        self.assertIn("not found", result["note"])
        self.assertEqual(result["attachments"][0]["id"], "att-1")


if __name__ == "__main__":
    unittest.main()
