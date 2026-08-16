"""Disposable PDF-parsing worker, started as a plain subprocess.

The parent (:func:`moneybird_mcp.attachments.extract_pdf_text`) writes one JSON
options line followed by the raw attachment bytes on stdin, and reads a single
JSON result object from stdout. Nothing else may be written to stdout.

This is deliberately *not* a ``multiprocessing`` worker: on Windows,
``multiprocessing``'s spawn transport re-runs the parent's ``__main__`` module
inside the child and hands the payload over a small pipe whose read end the
parent keeps open, so a child that stalls while bootstrapping wedges the parent
in ``Process.start()`` forever — with no timeout, because the parser deadline
only starts once ``start()`` returns. A subprocess with ``communicate(timeout=)``
bounds the whole exchange instead.
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    stdin = sys.stdin.buffer
    header = stdin.readline()
    if not header:
        return 2
    options = json.loads(header.decode("utf-8"))
    max_memory_bytes = int(options.pop("max_memory_bytes"))
    data = stdin.read()

    from moneybird_mcp.attachments import (
        _apply_worker_memory_limit,
        _extract_pdf_text_in_process,
    )

    _apply_worker_memory_limit(max_memory_bytes)
    result = _extract_pdf_text_in_process(data, **options)
    sys.stdout.buffer.write(json.dumps(result).encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
