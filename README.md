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
- `moneybird_request`
- `get_profit_loss`
- `get_balance_sheet`
- `get_general_ledger`
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
```

`MONEYBIRD_ADMINISTRATION_ID` can be left blank if your token only has access to one administration. If the token can see more than one, the server will ask you to choose one explicitly.

### Network exposure & authentication

- **`MCP_HOST` defaults to `127.0.0.1` (loopback only).** The cloudflared tunnel runs on the same host and connects to localhost, so this does **not** break tunnelling — it just stops the server from listening on every network interface. Only set `MCP_HOST=0.0.0.0` if you genuinely need to bind externally.
- **`MCP_AUTH_TOKEN`** is an optional shared secret. When set, every request to the SSE endpoint must present it as either `Authorization: Bearer <token>` or `X-MCP-Token: <token>`; anything else gets `401 Unauthorized`. When unset, the endpoint is unauthenticated (acceptable only on loopback).
- **Safety guard:** the server *refuses to start* if `MCP_HOST` is non-loopback while `MCP_AUTH_TOKEN` is unset — so you can't accidentally expose unauthenticated bookkeeping data to the network.

## 3. Install and run

```powershell
python -m pip install -r requirements.txt
python .\moneybird_mcp_server.py
```

By default the server exposes an SSE endpoint at:

```text
http://localhost:8000/sse
```

## Project layout

The server is split into a small package by concern; `moneybird_mcp_server.py`
is just the entrypoint you run.

```text
moneybird_mcp_server.py   # entrypoint: env-driven host/port/auth, runs the SSE app
moneybird/
  config.py               # constants, MoneybirdError, .env loading
  client.py               # Moneybird REST client (HTTP, retry/backoff)
  formatting.py           # pure helpers: titles, money, search-record shaping
  safety.py               # write guards: approval tokens (TTL) + audit log
  sync.py                 # local search-index sync (cached on disk)
  invoicing.py            # bookkeeping logic: journals, invoices, merge/reclassify
  tools.py                # the ~51 MCP tools exposed to ChatGPT
  guidance.py             # the "skill" layer: playbook resource + scenario prompts
  playbooks/
    boekhoud_playbook.md  # deep bookkeeping reference (loaded on demand)
  auth.py                 # optional shared-secret SSE auth middleware
```

Dependencies flow one way: `config → client → formatting → safety → sync →
invoicing → tools`. Nothing below `tools` imports from `tools`. `guidance.py`
imports nothing from the package and is registered imperatively at the end of
`tools.py`, so it cannot create an import cycle.

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
- `moneybird_request(path, query=None)`: read-only escape hatch that performs a single GET against any Moneybird endpoint this server does not wrap explicitly (e.g. `estimates`, `subscriptions`, `time_entries/123`, `documents/purchase_invoices`). `path` is relative to the administration; use `administrations` for the API root. It can only read — use the `prepare_*` / `*_from_approval` tools to change anything.
- `get_profit_loss(period)`: reads the Moneybird profit and loss report for the requested period.
- `get_balance_sheet(period)`: reads the Moneybird balance sheet report for the requested period.
- `get_general_ledger(period)`: reads the Moneybird general ledger report for the requested period.
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

## 5b. Prompts and the playbook (the "skill" layer)

The tools are the hands; this layer is the craft, so someone else's AI client can process
overdue bookkeeping, categorize a year, or read the reports without re-deriving the rules.
It uses progressive disclosure rather than one giant always-on instruction:

- **Always-on, thin** — the hard rails live in the server `instructions` (no write without
  explicit approval, never invent data, verify totals, propose when unsure, you are not a
  tax advisor).
- **Scenarios (MCP prompts)** — invokable, parameterized playbooks that carry the rails
  inline and point at the reference:
  - `verwerk_achterstand(period, document_kind)` — work through a backlog: inventory,
    categorize, and apply consistently, with approval per batch.
  - `categoriseer_heel_jaar(year)` — categorize a full year, quarter by quarter and
    internally consistent.
  - `leg_cijfers_uit(period)` — read the profit-and-loss and balance sheet and explain the
    numbers in plain language (read-only).
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
- It now also supports previewed batch invoice creation, duplicate warnings, automatic merge checks for scheduled sends, workflow pause/resume, and batch invoice updates.
- When a new invoice is scheduled for a contact/date that already has exactly one scheduled invoice, the server automatically reuses that invoice's workflow/style/identity defaults before showing the approval preview.
- `search` uses a local synchronization cache when available and falls back to a live first-page scan when no cache exists yet.
- The sync cache now covers contacts, sales invoices, purchase invoices, receipts, general journal documents, and financial mutations.
- The HTTP client retries transient `429` and `5xx` responses with backoff, which makes multi-step bookkeeping runs much less fragile.
- The sync cache is stored locally and should not be committed.
- Successful write actions are appended to a local JSONL audit log at `.moneybird_audit_log.jsonl`.
- Failed multi-step writes now also append a failure entry with partial progress, which helps with recovery after interrupted bookkeeping runs.
- OpenAI’s current MCP docs explicitly warn that prompt injection and accidental writes are real risks. Do not disable approvals for destructive tools unless you truly trust the full prompt chain and the server.
