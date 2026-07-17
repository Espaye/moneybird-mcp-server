# Reading a purchase-invoice PDF attachment

Status: **implemented.** The MCP tool is `read_document_attachment`
(`moneybird/tools/purchases.py`): it downloads the attachment via
`MoneybirdClient.download_attachment`, saves the file under the data dir
(`attachments/`), and returns the PDF's text layer when `pypdf` is installed
(the `moneybird-mcp[pdf]` extra). This note remains as the design record —
why it exists, the API path, and what is deliberately left out (OCR).

## Why it matters

`prepare_reconcile_purchase_invoice` reproduces a supplier's booking by scaling a
reference invoice's lines to the target total. When the totals differ, the per-line
split (e.g. stroom vs gas) is an **assumption** copied from the reference — it is
flagged as a warning in the preview. The only way to remove that assumption is to
read the numbers off the actual invoice PDF. Those bytes are retrievable today;
what is missing is the parsing/OCR step, which is deliberately out of scope for the
server.

## The API path (already available)

Every purchase invoice returned by `get_document("purchase_invoice", id)` carries
an `attachments` array. Each entry looks like:

```json
{
  "id": "492726866741823091",
  "filename": "Termijnspecificatie - Notanummer 1168011272.pdf",
  "content_type": "application/pdf",
  "size": 73113
}
```

The file bytes are served by (confirmed present in the OpenAPI spec, exposed as a
generic GET in `docs/moneybird_api_coverage.md`):

```
GET /{administration_id}/documents/purchase_invoices/{document_id}/attachments/{attachment_id}/download
```

`raw_get()` is **not** the right call here: it appends `.json` and JSON-decodes the
body, whereas this endpoint returns raw binary. Fetch it directly with the client's
token instead:

```python
import urllib.request
from moneybird.client import get_client

client = get_client()
doc = client.get_document("purchase_invoice", "492726866733434481")
attachment = doc["attachments"][0]                     # pick the PDF you want

url = (
    f"{client.base_url}/{client.administration_id}"
    f"/documents/purchase_invoices/{doc['id']}"
    f"/attachments/{attachment['id']}/download"
)
request = urllib.request.Request(url)
request.add_header("Authorization", f"Bearer {client.token}")
with urllib.request.urlopen(request, timeout=client.timeout) as response:
    pdf_bytes = response.read()                         # the raw PDF
```

Caveat: Moneybird may answer this endpoint with a `302` redirect to a signed
storage URL that does **not** accept the `Authorization` header. `urllib` follows
redirects automatically; if a redirect ever 401s, re-request the `Location` URL
*without* the bearer header.

## How it is implemented

1. `MoneybirdClient._binary_request` fetches the endpoint without JSON decoding,
   refuses automatic redirects, and re-requests the signed `Location` URL
   **without** the Authorization header (the bearer token must never reach the
   storage host). `download_attachment(kind, document_id, attachment_id)` wraps it;
   the path is checked against `docs/moneybird_api_paths.json` like every other
   endpoint (`tests/test_client_spec_conformance.py` also scans `_binary_request`).
2. Extraction is read-only and separate: `moneybird/attachments.py::extract_pdf_text`
   reads the text layer with `pypdf` when installed and otherwise explains what is
   missing. Results are surfaced for confirmation — never auto-written.
3. Any resulting change goes through `prepare_reconcile_purchase_invoice`, so the
   total-preservation check and the approval flow still apply.

## What is deliberately left out

- **OCR.** Scanned invoices without a text layer report a clear note instead;
  OCR would add heavy dependencies and stays out of the server.
- **A direct write from parsed values.** Extracted amounts feed the existing
  `prepare_reconcile_purchase_invoice` flow (an explicit `target_total` and/or a
  hand-picked reference) — no new write machinery.
