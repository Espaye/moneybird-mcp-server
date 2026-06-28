# CLAUDE.md — operating guide for this repo

Guidance for an AI agent working in this repository. The goal: be able to read from
and (carefully) write to the live Moneybird administration **without rediscovering the
setup each time**. Read this before doing live Moneybird work.

## What this is

A Moneybird MCP server (FastMCP). The MCP tools in `moneybird/tools.py` are the surface
a chat model normally calls. When you work *in this repo* those tools are usually **not**
wired up as live MCP tools, so to actually touch the administration you run small Python
scripts that import the package directly (see below).

## Credentials — already handled, don't hand-roll it

- Credentials live in `.env` at the repo root: `MONEYBIRD_ACCESS_TOKEN` and
  `MONEYBIRD_ADMINISTRATION_ID` (also `MCP_HOST` / `MCP_PORT`).
- `moneybird/config.py` calls `load_local_env()` **on import**, which loads that `.env`
  into the environment. As of the cwd-independent fix it looks in the current directory
  *and* next to the package, so **just `import moneybird...` and credentials are present**
  — regardless of where you run Python from. Do not parse `.env` yourself.
- Real env vars always win (`setdefault`), so per-request headers / CI secrets are safe.

## Running a one-off live query or fix

Put throwaway scripts in the scratchpad, not in the repo. Template:

```python
import sys
sys.path.insert(0, r"c:\Users\pretm\moneybird_mcp_server")
from moneybird.client import get_client   # importing the package loads .env for you

client = get_client()                      # uses MONEYBIRD_* from .env
# read-only first:
ledgers = {str(l["id"]): l["name"] for l in client.list_ledger_accounts()}
taxes   = {str(t["id"]): t["name"]  for t in client.list_tax_rates()}
```

`moneybird/client.py::MoneybirdClient` has typed methods for the common endpoints
(`list_contacts`, `list_sales_invoices`, `list_documents("purchase_invoice", ...)`,
`get_document`, `update_document`, reports, etc.) and `raw_get(path)` as a read-only
escape hatch for anything unwrapped.

For a quick sanity sweep of read-only access: `python scripts/healthcheck_readonly.py`.

## Write safety (HARD RULES — same as the server instructions)

1. **Never write without explicit user confirmation.** Workflow: read → show an exact
   preview (ids, before→after values) → wait for a clear "yes" → only then write.
2. Never invent data (amounts, references, dates, ledger/tax ids). Ask or leave blank.
3. **After any change, verify the document total is unchanged to the cent** and say so.
   Re-fetch the record and compare `total_price_incl_tax` before vs after.
4. Writes go through `update_document` / `update_sales_invoice` with
   `details_attributes`. To edit an existing line, include its detail `id` plus only the
   fields you change — Moneybird keeps the rest (description, ledger, tax).

## Domain gotchas learned the hard way

- **Suppliers invoice *us*.** A vendor like Vitens (water), KPN, etc. has **0 sales
  invoices**; its documents are **purchase invoices** (or receipts). Look under
  `list_documents("purchase_invoice", ...)` / `("receipt", ...)`, then filter by
  `contact.id`. Sales-invoice filters take `contact_id`; the document endpoints don't, so
  fetch a period and filter client-side on the contact.
- **Document line prices are entered *incl btw*** in this administration. So a "40% / 60%"
  split is 40% / 60% of the **incl-tax total**, and the invoice total incl = sum of line
  `price` values. `total_price_excl_tax_with_discount` on a line is back-calculated
  (e.g. `price 10.44` at 9% → excl `9.58`). Keep the incl total fixed to preserve the
  payment match; the excl/btw breakdown may legitimately shift.
- **The `amount` field is messy in older data**: values like `"1 x"` or `""` occur and
  Moneybird treats them as `1`. Normalize to `"1"` when asked to make lines consistent;
  it doesn't change any total.
- **Boekingsregels (bank/transaction rules) are not in the API** — see
  `moneybird/playbooks/boekhoud_playbook.md` and the memory note. Don't try to read them.

## Where things live

- `moneybird/tools.py` — MCP tool definitions (read tools, `prepare_*`/`*_from_approval`).
- `moneybird/client.py` — HTTP client + endpoint methods.
- `moneybird/config.py` — constants, `MoneybirdError`, `.env` loading.
- `moneybird/playbooks/boekhoud_playbook.md` — btw rules, categorization, consistency
  checklist, bank-mutation diagnosis. Read it before a bookkeeping task.
- `scripts/` — runnable read-only/reclassify scripts (good examples of the patterns above).
- `README.md` — setup, deployment, ChatGPT connection, tool descriptions.

## Tests

`python -m pytest -q` from the repo root. (Note: one pre-existing failure,
`test_register_guidance_registers_prompts_and_resource`, is stale — it predates the
`diagnose_bankmutatie` prompt and is unrelated to live Moneybird work.)
