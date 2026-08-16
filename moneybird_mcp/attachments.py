"""Turn downloaded attachment bytes into something a model can read.

The download itself lives on :class:`moneybird_mcp.client.MoneybirdClient`
(``download_attachment``); this module is the pure extraction layer. PDF text
extraction uses ``pypdf`` when it is installed (the ``moneybird-mcp[pdf]``
extra) and degrades to a clear note when it is not — OCR for scanned documents
is deliberately out of scope (see docs/reading_pdf_attachments.md).
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from typing import Any

PDF_MAGIC = b"%PDF"
_WORKER_KILL_GRACE_SECONDS = 5.0
DEFAULT_MAX_TEXT_CHARS = 40_000
DEFAULT_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_PDF_PAGES = 100
DEFAULT_PDF_PARSE_TIMEOUT_SECONDS = 10.0
DEFAULT_PDF_WORKER_MEMORY_BYTES = 256 * 1024 * 1024
PDF_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "application/octet-stream",
        "binary/octet-stream",
    }
)


def looks_like_pdf(data: bytes) -> bool:
    return data[:1024].lstrip().startswith(PDF_MAGIC)


def _extract_pdf_text_in_process(
    data: bytes,
    *,
    max_chars: int = DEFAULT_MAX_TEXT_CHARS,
    max_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_pages: int = DEFAULT_MAX_PDF_PAGES,
    content_type: str | None = None,
) -> dict[str, Any]:
    """Best-effort text-layer extraction from PDF bytes.

    Returns ``{"available": bool, ...}``; when unavailable, ``note`` explains
    why (not a PDF, pypdf missing, parse failure, or a scan without a text
    layer) so the caller can relay the reason instead of guessing.
    """
    if max_bytes < 1 or max_pages < 1 or max_chars < 1:
        raise ValueError("Attachment byte, page, and text limits must be positive")
    if len(data) > max_bytes:
        return {
            "available": False,
            "untrusted_content": True,
            "note": (
                f"The attachment is too large to parse safely "
                f"({len(data)} bytes; limit {max_bytes})."
            ),
        }
    normalized_content_type = str(content_type or "").partition(";")[0].strip().lower()
    if normalized_content_type and normalized_content_type not in PDF_CONTENT_TYPES:
        return {
            "available": False,
            "untrusted_content": True,
            "note": (
                "The attachment content type is not allowed for PDF extraction: "
                f"{normalized_content_type}."
            ),
        }
    if not looks_like_pdf(data):
        return {
            "available": False,
            "untrusted_content": True,
            "note": "The attachment is not a PDF; no text was extracted.",
        }
    try:
        from pypdf import PdfReader
    except ImportError:
        return {
            "available": False,
            "untrusted_content": True,
            "note": (
                "pypdf is not installed, so the PDF text layer cannot be read. "
                "Install the extra: pip install 'moneybird-mcp[pdf]'."
            ),
        }
    try:
        reader = PdfReader(io.BytesIO(data))
        page_count = len(reader.pages)
        parts: list[str] = []
        extracted_pages = 0
        collected_chars = 0
        for page in reader.pages[:max_pages]:
            part = (page.extract_text() or "").strip()
            extracted_pages += 1
            if part:
                parts.append(part)
                collected_chars += len(part)
            if collected_chars >= max_chars:
                break
    except Exception:  # pypdf raises many exception types on malformed files
        return {
            "available": False,
            "untrusted_content": True,
            "note": "The PDF could not be parsed safely.",
        }

    text = "\n\n".join(parts).strip()
    pages_truncated = page_count > extracted_pages
    if not text:
        return {
            "available": False,
            "untrusted_content": True,
            "page_count": page_count,
            "pages_examined": extracted_pages,
            "pages_truncated": pages_truncated,
            "note": (
                "The PDF has no text layer (likely a scanned document); "
                "OCR is not built into this server."
            ),
        }
    truncated = len(text) > max_chars or pages_truncated
    return {
        "available": True,
        "untrusted_content": True,
        "page_count": page_count,
        "pages_examined": extracted_pages,
        "pages_truncated": pages_truncated,
        "text": text[:max_chars],
        "truncated": truncated,
    }


_WINDOWS_JOB_HANDLE: int | None = None


def _apply_worker_memory_limit(max_memory_bytes: int) -> None:
    """Apply a hard address-space/process-memory cap inside the parser worker."""

    if os.name != "nt":
        import resource

        resource.setrlimit(
            resource.RLIMIT_AS,
            (max_memory_bytes, max_memory_bytes),
        )
        return

    # Windows has no ``resource`` module. Assign this worker to a Job Object
    # with JOB_OBJECT_LIMIT_PROCESS_MEMORY instead.
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
    ]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    information = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    information.BasicLimitInformation.LimitFlags = 0x00000100
    information.ProcessMemoryLimit = max_memory_bytes
    if not kernel32.SetInformationJobObject(
        job,
        9,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
    if not kernel32.AssignProcessToJobObject(
        job,
        kernel32.GetCurrentProcess(),
    ):
        raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
    global _WINDOWS_JOB_HANDLE
    _WINDOWS_JOB_HANDLE = int(job)


def _worker_command() -> list[str]:
    if not sys.executable:
        raise OSError("No Python interpreter is available to isolate PDF parsing.")
    return [sys.executable, "-m", "moneybird_mcp._pdf_worker"]


def _worker_environment() -> dict[str, str]:
    """Guarantee the worker can import this package, whatever the server's cwd."""

    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{package_root}{os.pathsep}{existing}" if existing else package_root
    )
    return environment


def extract_pdf_text(
    data: bytes,
    *,
    max_chars: int = DEFAULT_MAX_TEXT_CHARS,
    max_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
    max_pages: int = DEFAULT_MAX_PDF_PAGES,
    content_type: str | None = None,
    timeout_seconds: float = DEFAULT_PDF_PARSE_TIMEOUT_SECONDS,
    max_memory_bytes: int = DEFAULT_PDF_WORKER_MEMORY_BYTES,
) -> dict[str, Any]:
    """Extract PDF text in a disposable time/memory-bounded process."""

    if (
        max_bytes < 1
        or max_pages < 1
        or max_chars < 1
        or timeout_seconds <= 0
        or max_memory_bytes < 64 * 1024 * 1024
    ):
        raise ValueError(
            "Attachment byte, page, text, timeout, and worker-memory limits "
            "must be positive and memory must be at least 64 MiB"
        )

    # Reject cheap invalid inputs before allocating a child process.
    if len(data) > max_bytes:
        return {
            "available": False,
            "untrusted_content": True,
            "note": (
                f"The attachment is too large to parse safely "
                f"({len(data)} bytes; limit {max_bytes})."
            ),
        }
    normalized_content_type = (
        str(content_type or "").partition(";")[0].strip().lower()
    )
    if normalized_content_type and normalized_content_type not in PDF_CONTENT_TYPES:
        return {
            "available": False,
            "untrusted_content": True,
            "note": (
                "The attachment content type is not allowed for PDF extraction: "
                f"{normalized_content_type}."
            ),
        }
    if not looks_like_pdf(data):
        return {
            "available": False,
            "untrusted_content": True,
            "note": "The attachment is not a PDF; no text was extracted.",
        }

    # The payload is one JSON options line followed by the raw bytes. Sending it
    # through communicate() keeps the whole exchange — process start, write,
    # read, and exit — inside a single deadline. An unresponsive worker can
    # never block the server thread indefinitely.
    payload = json.dumps(
        {
            "max_chars": max_chars,
            "max_bytes": max_bytes,
            "max_pages": max_pages,
            "content_type": content_type,
            "max_memory_bytes": max_memory_bytes,
        }
    ).encode("utf-8") + b"\n" + data

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(
            _worker_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_worker_environment(),
            creationflags=creation_flags,
        )
    except OSError as exc:
        return {
            "available": False,
            "untrusted_content": True,
            "isolation": "worker_process",
            "note": f"The isolated PDF parser could not be started ({exc}).",
        }

    try:
        stdout, _ = process.communicate(payload, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.communicate(timeout=_WORKER_KILL_GRACE_SECONDS)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        return {
            "available": False,
            "untrusted_content": True,
            "isolation": "worker_process",
            "note": "The PDF parser exceeded its time limit and was terminated.",
        }
    except (OSError, ValueError) as exc:
        process.kill()
        return {
            "available": False,
            "untrusted_content": True,
            "isolation": "worker_process",
            "note": f"The isolated PDF parser failed safely ({exc}).",
        }

    try:
        if process.returncode != 0:
            raise ValueError(f"worker exited with code {process.returncode}")
        result = json.loads(stdout.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("worker returned a non-object result")
    except (UnicodeDecodeError, ValueError) as exc:
        return {
            "available": False,
            "untrusted_content": True,
            "isolation": "worker_process",
            "note": f"The isolated PDF parser failed safely ({exc}).",
        }

    return {
        **result,
        "isolation": "worker_process",
        "timeout_seconds": timeout_seconds,
        "worker_memory_limit_bytes": max_memory_bytes,
    }


def safe_attachment_filename(filename: str, fallback: str = "attachment") -> str:
    """Reduce an untrusted attachment name to a safe display/basename value."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(filename or "").strip()).strip("._")
    return name or fallback
