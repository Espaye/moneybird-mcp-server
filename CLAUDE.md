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
- **OAuth**: `moneybird/oauth.py` implements the authorization-code flow (app credentials in
  `.env` as `MONEYBIRD_OAUTH_CLIENT_ID`/`MONEYBIRD_OAUTH_CLIENT_SECRET`; interactive login via
  `python scripts/oauth_login.py`, out-of-band redirect). Tokens persist in
  `moneybird_oauth_tokens.json` in the data dir and are used automatically when
  `MONEYBIRD_ACCESS_TOKEN` is absent (resolution order: request header → env → OAuth store).

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

- **Meterverbruikfacturen hebben een first-class flow.** Gebruik
  `prepare_meter_usage_sales_invoices` met begin/eindstanden of `usage_kwh`; de tool rekent
  verbruik na, kan lage/uitgesloten meters overslaan, hergebruikt de laatste passende
  meterregel voor tarief/btw/grootboek en kan alles in één batch inplannen. Na akkoord:
  `meter_usage_sales_invoices_from_approval`; de uitvoering verifieert status, datum en
  totaal automatisch. Voor bestaande concepten gebruik je
  `prepare_batch_schedule_sales_invoices` → `batch_schedule_sales_invoices_from_approval`.

- **Suppliers invoice *us*.** A vendor like Vitens (water), KPN, etc. has **0 sales
  invoices**; its documents are **purchase invoices** (or receipts). Look under
  `list_documents("purchase_invoice", ...)` / `("receipt", ...)`, then filter by
  `contact.id`. Sales-invoice filters take `contact_id`; the document endpoints don't, so
  fetch a period and filter client-side on the contact.
- **Moneybird's boekingsregels vullen ook inkoopfacturen automatisch in — inconsistent.**
  Dezelfde regels die bankmutaties boeken, vullen inkomende inkoopfacturen in, maar niet
  betrouwbaar: dezelfde leverancier komt de ene maand met de vaste meerregelige splitsing binnen
  en de volgende als één verzamelregel, nog in status `new`, soms met `prices_are_incl_tax`
  omgedraaid. De regel zelf zie/repareer je niet (niet in de API), alleen het resultaat. Gebruik
  `review_purchase_invoices` om afwijkers te vinden (nog `new`, minder regels dan gebruikelijk,
  ontbrekende grootboeken, of een afwijkende btw-vlag) en `prepare_reconcile_purchase_invoice`
  → `reconcile_purchase_invoice_from_approval` om de vaste boeking van een goede referentiefactuur
  te reproduceren. Regelprijzen worden naar het doeltotaal geschaald zodat het documenttotaal tot
  op de cent gelijk blijft; wijken de totalen af, dan is de regel-voor-regel-splitsing een (in de
  preview gemarkeerde) aanname. De echte splitsing lees je van de factuur-PDF met
  `read_document_attachment` (tekstlaag via pypdf; ontwerp in
  `docs/reading_pdf_attachments.md`). (Logica: `moneybird/purchase_reconcile.py`.)
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
- **Sommige rapporten zijn maand-gebonden**: `cash_flow`, `tax`, `debtors` en `creditors`
  accepteren maximaal één maand (`this_month`, `202606`); de `*_aging`-rapporten willen een
  hele maand als peildatum. Alleen `profit_loss`, `balance_sheet`, `general_ledger` en de
  `*_by_contact`/`*_by_project`-rapporten slikken `this_year`. (Live geverifieerd:
  `{"error":"Period cannot exceed 1 month"}`.)
- **Betalingen en bankkoppeling hebben eigen guarded tools**: `prepare_register_payment`
  (verkoopfactuur/inkoopfactuur/bon), `prepare_link_bank_mutation_booking` /
  `prepare_unlink_bank_mutation_booking` (bankmutatie ↔ factuur/document/grootboekcategorie)
  en `prepare_create_credit_invoice`. Gebruik die, geen handmatige constructies.

## API coverage reference

`docs/moneybird_api_coverage.md` is the generated catalogue of **all 296 operations** in the
official Moneybird OpenAPI spec, annotated with what this server covers (dedicated tool,
`moneybird_request` read, or not exposed). Check it before wrapping a new endpoint or before
answering "does the API support X?". Its header explains how to regenerate it from the spec
asset bundled on developer.moneybird.com.

## Where things live

- `moneybird/tools/` — MCP tool definitions, split by domain (`sales.py`, `bank.py`,
  `payments.py`, `contacts.py`, `ledger.py`, `purchases.py`, `reference.py`,
  `reports.py`, `core.py`, `sales_batches.py`). `_registry.py` holds the FastMCP
  instance + server instructions; `_context.py` is the patchable indirection tests use
  (`mock.patch.object(moneybird.tools._context, "get_client", ...)`); `_writes.py` is
  the shared write machinery — new guarded writes use `stage_write` +
  `run_approved_write`, don't hand-roll the approval/audit plumbing. `_params.py` holds
  the shared `Annotated[..., Field(...)]` parameter types (Limit, Period, ApprovalId,
  ReportName, ...) that give MCP clients per-parameter descriptions and enums — use them
  in new tool signatures; its Literal enums are kept in sync with `config.py` by
  `tests/test_tool_params.py`.
- `moneybird/server.py` — the runnable entrypoint (`build_config` + `main`). The
  `moneybird-mcp` console script (see `pyproject.toml`) defaults to **stdio** for local
  MCP clients and defaults server state to `~/.moneybird-mcp`; `python
  moneybird_mcp_server.py` keeps the legacy SSE default for existing deployments.
  `mcpb/` + `scripts/build_mcpb.py` build the Claude Desktop extension bundle
  (`dist/*.mcpb`; platform-specific because dependencies are vendored into it).
- `moneybird/client.py` — HTTP client + endpoint methods. Every endpoint it calls is
  checked against `docs/moneybird_api_paths.json` by
  `tests/test_client_spec_conformance.py`, so a typo'd path fails the suite.
- `moneybird/purchase_reconcile.py` — supplier-pattern logic behind the purchase tools:
  `scan_purchase_invoices_for_attention` (the `review_purchase_invoices` detector) and
  `build_reconcile_purchase_invoice` (reproduces a reference invoice's split, scales prices to
  the target total, maps onto existing lines). Pure functions over a client; tested in
  `tests/test_purchase_reconcile.py`. PDF-reading design note: `docs/reading_pdf_attachments.md`.
- `moneybird/config.py` — constants, `MoneybirdError`, `.env` loading, and `data_dir()`
  (where approvals DB / audit logs / sync caches live; override with
  `MONEYBIRD_MCP_DATA_DIR`). Approvals are persisted in SQLite and survive restarts.
- `moneybird/search_fts.py` — SQLite FTS5 layer derived from the JSON sync index (the
  durable store stays JSON; the FTS file is a rebuildable cache keyed on `updated_at`).
  `search` tries FTS (AND then OR prefix match, bm25-ranked), then substring, then live.
- `moneybird/playbooks/boekhoud_playbook.md` — btw rules, categorization, consistency
  checklist, bank-mutation diagnosis. Read it before a bookkeeping task.
- `scripts/` — runnable read-only/reclassify scripts (good examples of the patterns above).
- `docs/releasing.md` — the release checklist (version bump in pyproject **and**
  mcpb/manifest.json, build, twine check, PyPI upload, mcpb bundle).
- `docs/hosted_gateway_design.md` — architecture for the hosted web-app product
  (gateway owns users/tokens and injects the tenant headers; this server stays
  unmodified). Read it before any hosted/multi-tenant work.
- `README.md` — setup, deployment, ChatGPT connection, tool descriptions.

## Tests

`python -m pytest -q` from the repo root. All tests should pass; when adding an MCP prompt,
also update `test_register_guidance_registers_prompts_and_resource` (it pins the exact
prompt-name set).
