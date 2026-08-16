# Reading purchase-invoice PDF attachments

Status: **implemented for local and authenticated single-user deployments; refused
in `hosted_request_only` mode.** The MCP tool is
`read_document_attachment` in `moneybird_mcp/tools/purchases.py`. Install the optional
parser with `moneybird-mcp[pdf]`.

## Intended use

`prepare_reconcile_purchase_invoice` can scale a reference invoice's line split to a
new total, but that split is still an assumption. A user can instead inspect the text
from the actual invoice, determine exact line amounts, and pass those values as
`desired_lines`.

Text extraction is evidence for the user or model to inspect. It does not infer
ledger or tax choices, approve a change, or write to Moneybird. Any later mutation
still goes through the normal prepare/execute path and its action-specific
postcondition check.

## Current data path and limits

The attachment metadata comes from the purchase invoice's `attachments` array. The
binary endpoint is:

```text
GET /{administration_id}/documents/purchase_invoices/{document_id}/attachments/{attachment_id}/download
```

The client deliberately handles binary responses separately from JSON API calls. It
validates public HTTPS redirect targets, pins the validated DNS address through the
TLS connection while retaining normal hostname verification, and does not forward
the Moneybird bearer token to a signed storage host.

The implementation then:

- enforces a 20 MiB limit using both declared size and streamed bytes;
- validates the advertised content type and PDF magic bytes;
- parses at most 100 pages;
- parses in a disposable worker subprocess (`moneybird_mcp._pdf_worker`) with a
  10-second wall-clock timeout and 256 MiB process-memory limit;
- returns at most 40,000 text characters;
- keeps the downloaded bytes and extracted text in memory and does not retain an
  attachment file.

If `pypdf` is not installed, the tool returns a clear missing-extra result. Scanned
documents with no useful text layer are not OCRed.

## Deployment boundary

Attachment download and parsing are disabled before client access in
`hosted_request_only` mode. The local parser has per-document process isolation,
timeout, and memory containment, but request-context operation has no shared
capacity, abuse, or retention controls for this surface.

For local or `network_single_user` use, parsing happens outside the server process.
The worker is started with `subprocess`, not `multiprocessing`: the spawn transport
re-runs the server's `__main__` inside the child and hands the payload over a pipe
whose read end the parent retains, so a child that stalls while bootstrapping wedges
the server thread before any deadline applies. The parent now writes the options and
bytes, reads the JSON result, and kills a worker that exceeds its deadline — all
under one timeout that also covers process start. Unix uses an address-space limit
and Windows uses a process-memory-limited Job Object. Parser failures fail closed
without returning partial text. Continue to treat untrusted PDFs as an input
risk and keep `pypdf` patched.

Older versions wrote attachments under the data directory. The current tool does
not use or automatically delete those legacy files; operators should review them
under their own retention and deletion policy.

## Deliberately out of scope

- OCR and image-based extraction.
- Automatic bookkeeping writes from parsed values.
- Inferring ledger accounts, tax rates, or line-item semantics.
- Request-context parsing without shared capacity, backpressure, abuse, and lifecycle controls.
