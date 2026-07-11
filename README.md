# Moneybird MCP Scaffold

This repo is a minimal MCP bridge between ChatGPT and Moneybird with read tools plus guarded write tools.

It exposes these tools:

- `search`
- `fetch`
- `list_contacts`
- `audit_invoice_delivery_settings`
- `list_sales_invoices`
- `audit_recent_sales_invoice_send_methods`
- `list_purchase_invoices`
- `list_receipts`
- `list_general_journal_documents`
- `list_financial_mutations`
- `list_administrations`
- `get_contact_by_customer_id`
- `list_products`
- `list_tax_rates`
- `list_ledger_accounts`
- `list_financial_accounts`
- `list_projects`
- `list_time_entries`
- `list_estimates`
- `list_recurring_sales_invoices`
- `moneybird_request`
- `get_profit_loss`
- `get_balance_sheet`
- `get_general_ledger`
- `get_financial_report`
- `sync_search_index`
- `search_contacts`
- `get_invoice_defaults_for_contact`
- `prepare_create_ledger_account`
- `create_ledger_account_from_approval`
- `prepare_create_general_journal_document`
- `create_general_journal_document_from_approval`
- `prepare_reclassify_document_lines`
- `reclassify_document_lines_from_approval`
- `prepare_create_contact`
- `create_contact_from_approval`
- `prepare_create_sales_invoice_draft`
- `create_sales_invoice_draft_from_approval`
- `prepare_batch_create_sales_invoices`
- `batch_create_sales_invoices_from_approval`
- `prepare_batch_update_sales_invoices`
- `batch_update_sales_invoices_from_approval`
- `prepare_batch_schedule_sales_invoices`
- `batch_schedule_sales_invoices_from_approval`
- `prepare_meter_usage_sales_invoices`
- `meter_usage_sales_invoices_from_approval`
- `prepare_send_sales_invoice`
- `send_sales_invoice_from_approval`
- `prepare_pause_sales_invoice_workflow`
- `pause_sales_invoice_workflow_from_approval`
- `prepare_resume_sales_invoice_workflow`
- `resume_sales_invoice_workflow_from_approval`
- `prepare_set_contacts_delivery_method_email`
- `set_contacts_delivery_method_email_from_approval`
- `prepare_update_contact`
- `update_contact_from_approval`
- `prepare_archive_contact`
- `archive_contact_from_approval`
- `prepare_register_payment`
- `register_payment_from_approval`
- `prepare_link_bank_mutation_booking`
- `link_bank_mutation_booking_from_approval`
- `prepare_unlink_bank_mutation_booking`
- `unlink_bank_mutation_booking_from_approval`
- `prepare_create_credit_invoice`
- `create_credit_invoice_from_approval`

The first two are the important ones if you want ChatGPT deep research or ChatGPT developer mode to treat Moneybird like a data source. The `prepare_*` and `*_from_approval` pairs are the guarded write path.

## 1. Create a fresh Moneybird token

If you pasted a real token into chat, revoke it first and create a new one.

Moneybird uses a Bearer token for personal API access. You can find the docs here:

- `https://developer.moneybird.com/authentication`
- `https://developer.moneybird.com/integration/getting-started`

## 2. Configure the environment

Copy `.env.example` to `.env` and fill in your values:

```env
MONEYBIRD_ACCESS_TOKEN=mb_xxx
MONEYBIRD_ADMINISTRATION_ID=123456789
MCP_HOST=127.0.0.1
MCP_PORT=8000
MCP_AUTH_TOKEN=
# Optional: where server state lives (approvals DB, audit logs, sync caches).
# Defaults to the working directory; set an absolute path for real deployments.
MONEYBIRD_MCP_DATA_DIR=
# Optional: "sse" (default, endpoint /sse) or "http" (streamable HTTP, endpoint /mcp).
MCP_TRANSPORT=sse
# Optional: OAuth application credentials (register at
# https://moneybird.com/user/applications/new); used by scripts/oauth_login.py as an
# alternative to a personal MONEYBIRD_ACCESS_TOKEN.
MONEYBIRD_OAUTH_CLIENT_ID=
MONEYBIRD_OAUTH_CLIENT_SECRET=
```

`MONEYBIRD_ADMINISTRATION_ID` can be left blank if your token only has access to one administration. If the token can see more than one, the server will ask you to choose one explicitly.

### Network exposure & authentication

- **`MCP_HOST` defaults to `127.0.0.1` (loopback only).** The cloudflared tunnel runs on the same host and connects to localhost, so this does **not** break tunnelling — it just stops the server from listening on every network interface. Only set `MCP_HOST=0.0.0.0` if you genuinely need to bind externally.
- **`MCP_AUTH_TOKEN`** is an optional shared secret. When set, every request to the SSE endpoint must present it as either `Authorization: Bearer <token>` or `X-MCP-Token: <token>`; anything else gets `401 Unauthorized`. When unset, the endpoint is unauthenticated (acceptable only on loopback).
- **Safety guard:** the server *refuses to start* if `MCP_HOST` is non-loopback while `MCP_AUTH_TOKEN` is unset — so you can't accidentally expose unauthenticated bookkeeping data to the network.

### Multi-tenant: serving more than one administration

The server resolves Moneybird credentials **per request**, so one running instance can serve
several users/administrations:

1. **Per-request headers (multi-tenant):** a caller sends its own token on each request:
   - `X-Moneybird-Token: <that user's Moneybird token>`
   - `X-Moneybird-Administration-Id: <that user's administration id>` (optional; omit if the
     token sees only one administration)
2. **Environment (single-user / local):** if no token header is present, the server falls back
   to `MONEYBIRD_ACCESS_TOKEN` / `MONEYBIRD_ADMINISTRATION_ID`. Existing single-user setups keep
   working unchanged.

Notes and limits:

- **The Moneybird token is the tenant boundary.** Send it only over TLS (the cloudflared tunnel
  provides it). `MCP_AUTH_TOKEN`, if set, is a coarse gate in front of the whole server; the
  per-request Moneybird token is what scopes data to a tenant. The token is never logged.
- **The sync cache is per administration** (`.moneybird_sync_index_<administration_id>.json`), so
  tenants never overwrite each other's cache. A pre-existing single-file cache is migrated
  automatically on first use.
- **The audit log is also per administration** (`.moneybird_audit_log_<administration_id>.jsonl`),
  and each entry records its `administration_id`. The duplicate-suppression check reads the
  tenant's own log (and the legacy shared log for back-compat), so tenants never share a write
  history.
- **OAuth (authorization-code flow) is supported** as a third credential source, tried after
  the header and the environment. One-time setup:
  1. Register an application at `https://moneybird.com/user/applications/new` with redirect URI
     `urn:ietf:wg:oauth:2.0:oob` (out-of-band: Moneybird displays the code in the browser, so no
     public callback endpoint is needed). For a hosted deployment, also register a stable HTTPS
     callback such as `https://<your-host>/oauth/callback`.
  2. Put the credentials in `.env`: `MONEYBIRD_OAUTH_CLIENT_ID` and
     `MONEYBIRD_OAUTH_CLIENT_SECRET`.
  3. Run `python scripts/oauth_login.py`, authorize in the browser, and paste the code. Tokens
     land in `moneybird_oauth_tokens.json` in the data dir (gitignored; contains secrets), the
     script verifies them by listing the reachable administrations, and expired access tokens
     are refreshed automatically from then on. The requested scopes are
     `sales_invoices documents estimates bank time_entries settings`.

  When `MONEYBIRD_ACCESS_TOKEN` is set it still wins over the OAuth store, so existing
  personal-token setups behave exactly as before. Not yet included: a server-side *per-user*
  token store keyed to inbound identity (a public multi-user product would put an OAuth consent
  step in front and map each end user to their own Moneybird tokens; today the multi-tenant path
  is the `X-Moneybird-Token` header).

## 3. Install and run

```powershell
python -m pip install -r requirements.txt
python .\moneybird_mcp_server.py
```

By default the server exposes a (legacy) SSE endpoint at:

```text
http://localhost:8000/sse
```

Set `MCP_TRANSPORT=http` to serve the current MCP streamable-HTTP transport at
`http://localhost:8000/mcp` instead — prefer this for new clients; `sse` remains
the default only so existing deployments keep working.

## Project layout

The server is split into a small package by concern; `moneybird_mcp_server.py`
is just the entrypoint you run.

```text
moneybird_mcp_server.py   # entrypoint: env-driven host/port/auth/transport
moneybird/
  config.py               # constants, MoneybirdError, .env loading, data_dir()
  credentials.py          # per-request tenant credentials (headers) + env fallback
  client.py               # Moneybird REST client (HTTP, retry/backoff)
  formatting.py           # pure helpers: titles, money, search-record shaping
  safety.py               # write guards: durable approvals (SQLite) + audit log
  sync.py                 # local search-index sync (cached on disk)
  invoicing.py            # bookkeeping logic: journals, invoices, merge/reclassify
  tools/                  # the 66 MCP tools, split by domain
    _registry.py          #   FastMCP instance + always-on server instructions
    _context.py           #   patchable indirection for client + audit-log access
    _writes.py            #   shared prepare/approve machinery (stage_write, run_approved_write)
    core.py               #   administrations, search/fetch, sync index, raw GET
    contacts.py           #   contact reads + guarded contact writes
    sales.py              #   sales reads + draft/send/pause/resume/credit writes
    sales_batches.py      #   batch create/update/schedule + meter-usage run
    purchases.py          #   purchase invoices, receipts, journals (reads)
    bank.py               #   financial mutations + link/unlink bookings
    payments.py           #   payment registration on invoices and receipts
    ledger.py             #   ledger accounts, general journals, reclassification
    reference.py          #   products, tax rates, projects, time entries, accounts
    reports.py            #   all Moneybird reports
  guidance.py             # the "skill" layer: playbook resource + scenario prompts
  playbooks/
    boekhoud_playbook.md  # deep bookkeeping reference (loaded on demand)
  auth.py                 # optional shared-secret auth middleware
docs/
  moneybird_api_coverage.md  # all 296 API operations + per-endpoint coverage status
  moneybird_api_paths.json   # slim OpenAPI snapshot backing the conformance test
```

Dependencies flow one way: `config → credentials → client → formatting → safety →
sync → invoicing → tools`. Nothing below `tools` imports from `tools`. `guidance.py`
imports nothing from the package and is registered by `tools/__init__.py`, so it
cannot create an import cycle.

### Write flow machinery

Every guarded write follows the same discipline via `tools/_writes.py`: a
`prepare_*` tool validates, builds a preview, and calls `stage_write(...)`; the
matching `*_from_approval` tool calls `run_approved_write(...)`, which pops the
stored approval, enforces the duplicate-suppression fingerprint, executes, and
audit-logs success or failure in one place. Adding a new write means writing a
prepare function and an executor — the safety plumbing comes for free. A few
multi-step batch flows (batch invoices, meter usage, reclassify, bulk delivery
method) keep hand-rolled executors because they record partial progress on failure.

### Durable approvals & server state

Approvals are stored in SQLite (`moneybird_approvals.sqlite3`), so a prepared write
survives a server restart and works across multiple worker processes. All server
state (approvals DB, per-administration audit logs, sync caches) lives in
`MONEYBIRD_MCP_DATA_DIR` (default: the working directory, for backward
compatibility); legacy state files in the working directory are still read and
migrated transparently.

## 4. Connect it to ChatGPT

According to OpenAI’s current MCP docs, ChatGPT developer mode can connect to a remote MCP server, and data-oriented servers should implement `search` and `fetch`.

Relevant OpenAI docs:

- `https://developers.openai.com/api/docs/mcp`
- `https://platform.openai.com/docs/guides/developer-mode`

To use this in ChatGPT:

1. Enable ChatGPT Developer Mode in ChatGPT settings.
2. Make the MCP server reachable over the internet.
3. Add the public `/sse` URL as your MCP server in ChatGPT Apps or Connectors.

For local testing, a tunnel is the quickest approach. Example with `cloudflared`:

```powershell
cloudflared tunnel --url http://localhost:8000
```

Then use the public URL ending in `/sse`.

## 5. What the tools do

- `search(query, limit=8)`: searches contacts, sales invoices, purchase invoices, receipts, general journals, and financial mutations.
- `fetch(id)`: fetches the full JSON for `contact:<id>`, `sales_invoice:<id>`, `purchase_invoice:<id>`, `receipt:<id>`, `general_journal_document:<id>`, `financial_mutation:<id>`, `ledger_account:<id>`, or `financial_account:<id>`.
- `list_contacts(limit=10, page=1)`: compact contact overview.
- `audit_invoice_delivery_settings(include_archived_contacts=False, include_inactive_recurring=False)`: controleert of contacten op verzendmethode `Email` staan, of er factuur-e-mailadressen ontbreken, en of periodieke facturen risico lopen door `auto_send`/verzendmethode/e-mailinstellingen.
- `list_sales_invoices(limit=10, page=1, state="all", reference="", contact_id="", period="")`: compact invoice overview with extra filtering.
- `audit_recent_sales_invoice_send_methods(limit=30, page_scan_limit=10)`: controleert recente verkoopfacturen en classificeert het oorspronkelijke verzend-event als handmatig, handmatig per e-mail, automatische e-mail, of e-factuur/SI.
- `list_purchase_invoices(limit=10, page=1, filter="", period="")`: compact inkoopfactuuroverzicht.
- `list_receipts(limit=10, page=1, filter="", period="")`: compact bonnen-/overige uitgavenoverzicht.
- `list_general_journal_documents(limit=10, page=1, filter="", period="")`: compact memoriaaloverzicht.
- `list_financial_mutations(limit=10, page=1, filter="", period="")`: compact bank- en kasmutatieoverzicht.
- `list_administrations()`: useful during setup if the token can access multiple administrations.
- `get_contact_by_customer_id(customer_id)`: fetches a contact by your own external identifier.
- `list_products(limit=25, page=1)`: reads product defaults, including `ledger_account_id` and `tax_rate_id`.
- `list_tax_rates()`: reads valid `tax_rate_id` values for invoice lines.
- `list_ledger_accounts()`: reads valid `ledger_account_id` values for invoice lines.
- `list_financial_accounts(limit=25, page=1)`: reads available bank, cash, and intermediary accounts.
- `list_projects(limit=25, page=1, state="")`: lists projects; optional `state` is `active`, `archived`, or `all`.
- `list_time_entries(limit=25, page=1, filter="", period="")`: lists logged hours; `filter` accepts Moneybird query syntax (e.g. `contact_id:123`, `project_id:456`, `state:open`), `period` accepts e.g. `202506` or `20250101..20250331`.
- `list_estimates(limit=10, page=1, filter="", period="")`: compact offerteoverzicht; `filter` accepts e.g. `state:open|late|accepted|rejected|billed`.
- `list_recurring_sales_invoices(limit=10, page=1, filter="")`: compact overzicht van periodieke facturen (frequentie, volgende factuurdatum, `auto_send`).
- `moneybird_request(path, query=None)`: read-only escape hatch that performs a single GET against any Moneybird endpoint this server does not wrap explicitly (e.g. `estimates`, `subscriptions`, `time_entries/123`, `documents/purchase_invoices`). `path` is relative to the administration; use `administrations` for the API root. It can only read — use the `prepare_*` / `*_from_approval` tools to change anything.
- `get_profit_loss(period)`: reads the Moneybird profit and loss report for the requested period.
- `get_balance_sheet(period)`: reads the Moneybird balance sheet report for the requested period.
- `get_general_ledger(period)`: reads the Moneybird general ledger report for the requested period.
- `get_financial_report(report_name, period, page=0)`: reads any Moneybird report — `profit_loss`, `balance_sheet`, `general_ledger`, `cash_flow`, `tax` (btw), `debtors` / `creditors` (openstaande posten), `debtors_aging` / `creditors_aging`, `revenue_by_contact`, `revenue_by_project`, `expenses_by_contact`, `expenses_by_project`, `journal_entries`, `subscriptions`, `assets`. Note: `cash_flow`, `tax`, `debtors`, and `creditors` accept at most one month of period (`this_month`, `202606`); the aging reports take a whole month as reference date.
- `sync_search_index(invoice_filter="state:all,period:this_year", document_filter="period:this_year", financial_mutation_filter="period:this_year", force_full=False)`: builds or refreshes a local cached search index from Moneybird synchronization endpoints across contacts, sales invoices, purchase invoices, receipts, general journals, and financial mutations.
- `search_contacts(query, limit=10)`: contact lookup by partial customer id, e-mail, phone, city, or company/person name.
- `get_invoice_defaults_for_contact(contact_id="", customer_id="")`: reads the latest invoice defaults for a contact so new invoices can inherit the right workflow, style, identity, tax, ledger, and send settings.
- `prepare_create_ledger_account(...)`: stages a ledger account create and returns an `approval_id`.
- `create_ledger_account_from_approval(approval_id)`: executes the staged ledger account create.
- `prepare_create_general_journal_document(...)`: stages a memoriaalboeking and returns an `approval_id`.
- `create_general_journal_document_from_approval(approval_id)`: executes the staged memoriaalboeking.
- `prepare_reclassify_document_lines(entries)`: stages purchase invoice / receipt line reclassifications, with optional balancing general journals for asset/liability moves.
- `reclassify_document_lines_from_approval(approval_id)`: executes the staged document reclassification.
- `prepare_create_contact(...)`: stages a contact write and returns an `approval_id`.
- `create_contact_from_approval(approval_id)`: executes the staged contact write.
- `prepare_create_sales_invoice_draft(...)`: stages a draft sales invoice write and returns an `approval_id`.
- `create_sales_invoice_draft_from_approval(approval_id)`: executes the staged draft invoice write.
- `prepare_batch_create_sales_invoices(entries, skip_if_duplicate=True, fail_on_duplicate=False)`: stages a multi-invoice batch with preview rows, duplicate warnings, optional scheduled sends, and an automatic merge-compatibility check for invoices planned on the same contact/date.
- `batch_create_sales_invoices_from_approval(approval_id)`: executes the staged batch create.
- `prepare_batch_update_sales_invoices(entries)`: stages updates to existing invoices by explicit invoice id or by customer lookup plus filters.
- `batch_update_sales_invoices_from_approval(approval_id)`: executes the staged batch update.
- `prepare_batch_schedule_sales_invoices(entries)`: stages future sending for multiple existing draft invoices, with merge checks and a single preview.
- `batch_schedule_sales_invoices_from_approval(approval_id)`: schedules the prepared batch and automatically verifies totals, state, date, and `sent_at`.
- `prepare_meter_usage_sales_invoices(...)`: turns meter readings into a complete invoice batch, calculates usage, skips configured/low-usage meters, reuses the latest matching tariff/tax/ledger, creates stable period references, and optionally schedules sending.
- `meter_usage_sales_invoices_from_approval(approval_id)`: executes the approved meter-usage batch and returns the automatic invoice verification table.
- `prepare_send_sales_invoice(...)`: stages sending or scheduling an invoice, with an automatic merge-compatibility check whenever the send is scheduled.
- `send_sales_invoice_from_approval(approval_id)`: executes the staged invoice send/schedule action.
- `prepare_pause_sales_invoice_workflow(sales_invoice_id)`: stages pausing a scheduled/automatic invoice workflow.
- `pause_sales_invoice_workflow_from_approval(approval_id)`: executes the pause.
- `prepare_resume_sales_invoice_workflow(sales_invoice_id)`: stages resuming a paused workflow.
- `resume_sales_invoice_workflow_from_approval(approval_id)`: executes the resume.
- `prepare_set_contacts_delivery_method_email(include_archived_contacts=False)`: stages a bulk update for contacts whose invoice delivery method is not `Email`.
- `set_contacts_delivery_method_email_from_approval(approval_id)`: executes the staged bulk delivery-method update and verifies remaining contact/recurring-invoice issues.
- `prepare_update_contact(...)`: stages a contact update, including optional field clearing.
- `update_contact_from_approval(approval_id)`: executes the staged contact update.
- `prepare_archive_contact(contact_id)`: stages archiving a contact.
- `archive_contact_from_approval(approval_id)`: executes the staged archive.
- `prepare_register_payment(document_type, document_id, payment_date, price, ...)`: stages a payment registration on a sales invoice, purchase invoice, or receipt, with an open-amount preview and overpayment/partial-payment warnings.
- `register_payment_from_approval(approval_id)`: executes the payment registration and verifies the document total is unchanged and the payment is visible.
- `prepare_link_bank_mutation_booking(financial_mutation_id, booking_type, booking_id, price="")`: stages linking a bank/cash mutation to an open invoice/document (`SalesInvoice`, `Document`) or directly to a ledger category (`LedgerAccount`) — the manual counterpart of Moneybird's bank reconciliation. Empty `price` links the full open amount.
- `link_bank_mutation_booking_from_approval(approval_id)`: executes the link and verifies the payment/category booking appears on the mutation.
- `prepare_unlink_bank_mutation_booking(financial_mutation_id, booking_type, booking_id)`: stages removing a wrongly matched `Payment` or `LedgerAccountBooking` from a mutation (errors early if the booking id is not on the mutation).
- `unlink_bank_mutation_booking_from_approval(approval_id)`: executes the unlink and verifies the booking is gone.
- `prepare_create_credit_invoice(sales_invoice_id)`: stages duplicating an invoice into a draft credit invoice (negated amounts, nothing sent).
- `create_credit_invoice_from_approval(approval_id)`: executes the credit duplication and verifies the credit total negates the original.

## 5b. Prompts and the playbook (the "skill" layer)

The tools are the hands; this layer is the craft, so someone else's AI client can process
overdue bookkeeping, categorize a year, or read the reports without re-deriving the rules.
It uses progressive disclosure rather than one giant always-on instruction:

- **Always-on, thin** — the hard rails live in the server `instructions` (no write without
  explicit approval, never invent data, verify totals, propose when unsure, you are not a
  tax advisor).
- **Scenarios (MCP prompts)** — invokable, parameterized playbooks that carry the rails
  inline and point at the reference:
  - `aan_de_slag()` — first-run onboarding: explains what the assistant can do, shows the
    approval mechanism, pulls a first read-only picture of the administration, and offers
    five concrete starter tasks.
  - `koppel_banktransacties(period, limit)` — walk through unprocessed bank mutations,
    propose a match per mutation (open invoice, document, or ledger category), and link
    each one after approval via the bank-mutation booking tools.
  - `verwerk_achterstand(period, document_kind)` — work through a backlog: inventory,
    categorize, and apply consistently, with approval per batch.
  - `categoriseer_heel_jaar(year)` — categorize a full year, quarter by quarter and
    internally consistent.
  - `leg_cijfers_uit(period)` — read the profit-and-loss and balance sheet and explain the
    numbers in plain language (read-only).
  - `diagnose_bankmutatie(zoekterm, period)` — work out why a bank mutation was not
    automatically linked to a category or document, inferring rule behavior from the mutation
    fields and `created_at`/`processed_at` timing (boekingsregels themselves are not in the API).
  - `factureer_meterverbruik(period_label, invoice_date, schedule_send_on)` — calculate,
    preview, approve, schedule, and verify a complete meter-usage invoice run.
- **Reference (MCP resource)** — `moneybird://playbook/bookkeeping` serves
  `moneybird/playbooks/boekhoud_playbook.md` on demand: golden rules, btw, private vs.
  business / drawings, categorization, a consistency checklist, and scenario recipes. Edit
  the markdown to tune behavior; no restart-time codegen is involved (it is read fresh).

## 6. Approval behavior

There are two layers of protection here:

1. The server marks real write tools as destructive with MCP tool annotations.
2. The server itself uses a two-step write flow:
   `prepare_*` only stages the action.
   `*_from_approval` performs the Moneybird write.

This is the important limitation: MCP tool annotations are only hints. They improve how ChatGPT or other MCP clients treat the tools, but they do not by themselves guarantee a human approval step.

If you are connecting this server through the OpenAI Responses API, the current OpenAI MCP docs say approvals are the actual enforcement point. Keep approvals enabled for destructive tools by using `require_approval: "always"` or only exempting clearly safe read tools.

Relevant OpenAI docs:

- `https://platform.openai.com/docs/guides/tools-remote-mcp`
- `https://developers.openai.com/api/docs/mcp`

## 7. Notes and limits

- This scaffold is intentionally conservative on writes.
- It now supports contact create/update/archive, ledger account creation, general journal creation, purchase-document reclassification, sales invoice draft creation, and explicit send/schedule as approval-gated actions.
- It now also supports previewed batch invoice creation, batch scheduling with verification,
  a first-class meter-usage invoice run, duplicate warnings, automatic merge checks for
  scheduled sends, workflow pause/resume, and batch invoice updates.
- It also supports the daily-bookkeeping writes: payment registration on sales/purchase
  invoices and receipts, linking/unlinking bank mutations to invoices, documents, or ledger
  categories (manual bank reconciliation), and duplicating an invoice to a draft credit
  invoice — all approval-gated with automatic post-write verification.
- When a new invoice is scheduled for a contact/date that already has exactly one scheduled invoice, the server automatically reuses that invoice's workflow/style/identity defaults before showing the approval preview.
- `search` uses a local synchronization cache when available and falls back to a live first-page scan when no cache exists yet.
- The sync cache now covers contacts, sales invoices, purchase invoices, receipts, general journal documents, and financial mutations.
- The HTTP client retries transient `429` and `5xx` responses with backoff, which makes multi-step bookkeeping runs much less fragile.
- The sync cache is stored locally and should not be committed.
- Successful write actions are appended to a per-administration JSONL audit log at `.moneybird_audit_log_<administration_id>.jsonl` (falling back to `.moneybird_audit_log.jsonl` when no administration is set).
- Failed multi-step writes now also append a failure entry with partial progress, which helps with recovery after interrupted bookkeeping runs.
- OpenAI’s current MCP docs explicitly warn that prompt injection and accidental writes are real risks. Do not disable approvals for destructive tools unless you truly trust the full prompt chain and the server.
- **Boekingsregels (bank/transaction rules) are not exposed by the Moneybird API**, so the server cannot read or change them. To explain why a bank mutation was not auto-processed, the `diagnose_bankmutatie` prompt and playbook recipe E infer rule behavior from the financial-mutation fields and `created_at`/`processed_at` timing, and point the user to Moneybird's own Boekingsregels settings.
- `list_financial_mutations` returns HTTP 400 ("too many ... use sync API") for a wide period; query per month (`period:"JJJJMM01..JJJJMMnn"`) or use the sync index.
- The `cash_flow`, `tax`, `debtors`, and `creditors` reports accept at most one month of period; the `*_aging` reports require a whole month as reference date (verified live: `{"error":"Period cannot exceed 1 month"}`).
- `docs/moneybird_api_coverage.md` holds the full catalogue of all 296 Moneybird API operations (from the official OpenAPI spec) with per-endpoint coverage status — consult it before wrapping new endpoints.
