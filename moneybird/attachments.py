"""Turn downloaded attachment bytes into something a model can read.

The download itself lives on :class:`moneybird.client.MoneybirdClient`
(``download_attachment``); this module is the pure extraction layer. PDF text
extraction uses ``pypdf`` when it is installed (the ``moneybird-mcp[pdf]``
extra) and degrades to a clear note when it is not — OCR for scanned documents
is deliberately out of scope (see docs/reading_pdf_attachments.md).
"""
from __future__ import annotations

import io
import re
from typing import Any

PDF_MAGIC = b"%PDF"
DEFAULT_MAX_TEXT_CHARS = 40_000


def looks_like_pdf(data: bytes) -> bool:
    return data[:1024].lstrip().startswith(PDF_MAGIC)


def extract_pdf_text(
    data: bytes,
    *,
    max_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> dict[str, Any]:
    """Best-effort text-layer extraction from PDF bytes.

    Returns ``{"available": bool, ...}``; when unavailable, ``note`` explains
    why (not a PDF, pypdf missing, parse failure, or a scan without a text
    layer) so the caller can relay the reason instead of guessing.
    """
    if not looks_like_pdf(data):
        return {
            "available": False,
            "note": "The attachment is not a PDF; no text was extracted.",
        }
    try:
        from pypdf import PdfReader
    except ImportError:
        return {
            "available": False,
            "note": (
                "pypdf is not installed, so the PDF text layer cannot be read. "
                "Install the extra: pip install 'moneybird-mcp[pdf]'."
            ),
        }
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # pypdf raises many exception types on malformed files
        return {"available": False, "note": f"The PDF could not be parsed: {exc}"}

    text = "\n\n".join(part.strip() for part in pages if part.strip()).strip()
    if not text:
        return {
            "available": False,
            "page_count": len(pages),
            "note": (
                "The PDF has no text layer (likely a scanned document); "
                "OCR is not built into this server."
            ),
        }
    truncated = len(text) > max_chars
    return {
        "available": True,
        "page_count": len(pages),
        "text": text[:max_chars],
        "truncated": truncated,
    }


def safe_attachment_filename(filename: str, fallback: str = "attachment") -> str:
    """Reduce an attachment filename to a safe basename for local storage."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(filename or "").strip()).strip("._")
    return name or fallback
