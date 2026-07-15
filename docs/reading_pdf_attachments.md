# Reading a purchase-invoice PDF attachment

Status: **not wired up as a tool.** This is a design note so that if someone later
wants an AI to actually read the PDF behind a purchase invoice (for example to
derive the real stroom/gas split of an Eneco *termijnnota* instead of assuming it
from the previous month), the path is already known and nothing has to be
rediscovered.

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

## What is deliberately left out

- **Parsing / OCR.** Turning `pdf_bytes` into structured numbers (a text-layer read
  with `pypdf`, or OCR for scanned invoices) is not implemented and adds heavy
  dependencies. That is the piece to build if this ever becomes a tool.
- **A guarded write from parsed values.** Once real per-line amounts are extracted,
  they would feed the existing `prepare_reconcile_purchase_invoice` flow by passing
  an explicit `target_total` and/or a hand-built reference — no new write machinery
  is needed, only a trustworthy source for the numbers.

## If you turn this into a tool

1. Add a `download_attachment(kind, document_id, attachment_id) -> bytes` method on
   `MoneybirdClient` (mirror `_request`, but return `response.read()` without JSON
   decoding), and check the path against `docs/moneybird_api_paths.json` like every
   other endpoint (`tests/test_client_spec_conformance.py`).
2. Keep extraction read-only and separate: a helper that takes bytes and returns
   candidate line amounts, surfaced for confirmation — never auto-written.
3. Route any resulting change through `prepare_reconcile_purchase_invoice` so the
   total-preservation check and the approval flow still apply.
