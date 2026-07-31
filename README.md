# Moneybird MCP server

> **Beta (0.5.0):** the supported production scope is local stdio with a
> mechanically read-only default. Experimental writes require an explicit local
> opt-in and supervised approval; the localhost gateway is a demonstration, not
> a hosted product.

Chat with your [Moneybird](https://www.moneybird.nl) bookkeeping from Claude, ChatGPT, or any
other MCP client: read invoices, contacts, bank mutations, and reports. The supported default
is mechanically **read-only**. Experimental writes remain available for an explicitly
supervised local deployment, behind a durable prepare/execute flow and action-specific
verification.

- **Read everything that matters**: contacts, sales and purchase invoices, receipts, bank
  mutations, and every Moneybird report (P&L, balance sheet, btw, aging, ...), plus ranked
  full-text search over a local sync index.
- **Writes are opt-in**: execution is denied unless the operator explicitly sets
  `MONEYBIRD_CAPABILITY_MODE=write_enabled`. A `prepare_*` tool can stage a preview and
  `approval_id`; execution atomically claims that request, records a typed outcome, and applies
  action-specific checks. Approvals survive server restarts.
- **Dutch bookkeeping smarts built in**: btw rules and categorization playbook, bank-mutation
  diagnosis, purchase-invoice reconciliation against a supplier's usual booking, meter-usage
  invoicing, and PDF attachment reading to check the real invoice split.

Beta quick start for MCP clients: `pip install moneybird-mcp` and run the `moneybird-mcp` console
script (stdio). See **Install and run** below for the Claude Desktop one-file extension and
the authenticated HTTP/SSE options. The package and Desktop extension start read-only.

## Tool surface

The runnable server defaults to compact **Tool Search** discovery: it exposes seven core
Moneybird tools plus FastMCP's `search_tools` and `call_tool`. The model searches the catalog
for the few tools needed by the current task instead of receiving every schema on every
connection. Use `--tool-discovery full` (or `MCP_TOOL_DISCOVERY=full`) for an older MCP client
that cannot use Tool Search.

The full native catalog contains these tools:

- `get_server_status`
- `search`
- `fetch`
- `list_contacts`
- `audit_invoice_delivery_settings`
- `list_sales_invoices`
- `audit_recent_sales_invoice_send_methods`
- `list_purchase_invoices`
- `get_purchase_invoice_by_reference`
- `list_receipts`
- `list_general_journal_documents`
- `read_document_attachment`
- `review_purchase_invoices`
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
- `prepare_reclassify_bank_mutation_bookings`
- `reclassify_bank_mutation_bookings_from_approval`
- `prepare_link_bank_mutation_booking`
- `link_bank_mutation_booking_from_approval`
- `prepare_unlink_bank_mutation_booking`
- `unlink_bank_mutation_booking_from_approval`
- `prepare_create_credit_invoice`
- `create_credit_invoice_from_approval`
- `prepare_reconcile_purchase_invoice`
- `reconcile_purchase_invoice_from_approval`
- `prepare_bookkeeping_correction_batch`
- `bookkeeping_correction_batch_from_approval`
- `execute_approved_action`

`search` and `fetch` are the important data-source tools for ChatGPT deep research or
developer mode. The `prepare_*` tools create exact guarded previews.
`execute_approved_action(approval_id)` is the stable executor for every prepared action, so a
client does not need to discover a different execution schema after the user says yes.

## 1. Create a fresh Moneybird token

If you pasted a real token into chat, revoke it first and create a new one.

Moneybird uses a Bearer token for personal API access. You can find the docs here:

- `https://developer.moneybird.com/authentication`
- `https://developer.moneybird.com/integration/getting-started`

## 2. Configure the environment

The package never discovers `.env` files automatically. Supply configuration in
the MCP client's process environment (recommended), or copy `.env.example` to a
protected operator file and select it explicitly with an absolute path:

```powershell
moneybird-mcp --env-file C:\Users\you\.config\moneybird-mcp.env
```

The file format is:

```env
MONEYBIRD_ACCESS_TOKEN=mb_xxx
MONEYBIRD_ADMINISTRATION_ID=123456789
MCP_HOST=127.0.0.1
MCP_PORT=8000
# Safe local default.
MCP_TRANSPORT=stdio
MONEYBIRD_CREDENTIAL_MODE=local
MONEYBIRD_CAPABILITY_MODE=read_only
# Required for HTTP/SSE, including loopback.
MCP_AUTH_TOKEN=
# Set true only for a non-loopback listener behind a trusted TLS reverse proxy.
MCP_TRUSTED_TLS_PROXY=false
# Optional: where server state lives (approvals DB, audit logs, sync caches).
# Console script default: ~/.moneybird-mcp. Legacy clone entrypoint: working directory.
MONEYBIRD_MCP_DATA_DIR=
# Optional: "search" (default, compact on-demand Tool Search) or "full".
MCP_TOOL_DISCOVERY=search
# Optional: OAuth application credentials (register at
# https://moneybird.com/user/applications/new); used by scripts/oauth_login.py as an
# alternative to a personal MONEYBIRD_ACCESS_TOKEN.
MONEYBIRD_OAUTH_CLIENT_ID=
MONEYBIRD_OAUTH_CLIENT_SECRET=
```

`MONEYBIRD_ADMINISTRATION_ID` can be left blank if your token only has access to one administration. If the token can see more than one, the server will ask you to choose one explicitly.

### Network exposure & authentication

- **Every HTTP/SSE listener requires `MCP_AUTH_TOKEN`, including loopback.** Each request must
  present it as `Authorization: Bearer <token>` or `X-MCP-Token: <token>`; otherwise the server
  returns `401 Unauthorized`.
- **`MCP_HOST` defaults to `127.0.0.1`.** A non-loopback bind is refused unless
  `MCP_TRUSTED_TLS_PROXY=true`. Set that flag only when a trusted reverse proxy really terminates
  TLS before the plaintext application listener.
- The shared secret is a coarse single-server gate, not per-user OAuth, authorization, or tenant
  membership. Do not treat a public static-secret endpoint as a hosted multi-user product.

### Credential and deployment modes

Credential resolution is deliberately mode-specific:

| Mode | Where it runs | Moneybird identity | Limits |
|---|---|---|---|
| `local` | stdio only | request context, then environment, then local OAuth store | Default local mode |
| `network_single_user` | authenticated HTTP/SSE | environment, then local OAuth store | Rejects all request tenant headers |
| `hosted_request_only` | trusted gateway only | one nonblank gateway-injected request token/admin | Live reads only; no env/OAuth fallback, writes, durable sync/FTS, or PDF parsing |

`hosted_request_only` is containment for a future gateway, not a production-hosting claim. The
trusted gateway must authenticate the caller, strip client-supplied Moneybird headers, and inject
its own context. Exposing this mode directly would let an authenticated client choose its own
Moneybird header and is unsupported.

Local and single-user notes:

- Local sync JSON and FTS filenames are scoped by Moneybird administration id, but filenames are
  not authorization. `search` revalidates current administration membership before reading them.
  They are unencrypted local files with operator-managed retention.
- Approvals and audit exports are administration-scoped, not principal/session/grant-scoped.
  Different grants to the same administration are not isolated enough for hosting.
- **OAuth (authorization-code flow) is supported** for `local` and
  `network_single_user` when an environment token is absent. One-time setup:
  1. Register an application at `https://moneybird.com/user/applications/new` with redirect URI
     `urn:ietf:wg:oauth:2.0:oob` (out-of-band: Moneybird displays the code in the browser, so no
     public callback endpoint is needed). A future hosted service would instead need its own
     fixed HTTPS callback, identity boundary, and durable OAuth-state design.
  2. Supply `MONEYBIRD_OAUTH_CLIENT_ID` and `MONEYBIRD_OAUTH_CLIENT_SECRET`
     through the parent environment or a protected explicit environment file.
  3. Run `python scripts/oauth_login.py --env-file C:\absolute\operator.env`
     (omit the option when the parent environment already supplies the values),
     authorize in the browser, and paste the code. Tokens
     land in `moneybird_oauth_tokens.json` in the data dir (gitignored; contains secrets), the
     script verifies them by listing the reachable administrations, and expired access tokens
     are refreshed automatically from then on. The requested scopes are
     `sales_invoices documents estimates bank time_entries settings`.

  When `MONEYBIRD_ACCESS_TOKEN` is set it wins over the local OAuth store. Hosted request mode
  never consults either process-wide source. A production per-user identity/session/grant store
  is not implemented.

## 3. Install and run

### Option A — local install for MCP clients (Claude Desktop, Claude Code, Cursor, ...)

The package is published as `moneybird-mcp`; the console script speaks **stdio**,
which is what desktop MCP clients spawn. With [uv](https://docs.astral.sh/uv/)
installed, this client config is all you need:

```json
{
  "mcpServers": {
    "moneybird": {
      "command": "uvx",
      "args": ["moneybird-mcp"],
      "env": {
        "MONEYBIRD_ACCESS_TOKEN": "your-token-here",
        "MONEYBIRD_ADMINISTRATION_ID": "optional"
      }
    }
  }
}
```

Or install it explicitly: `pip install moneybird-mcp`, then use `moneybird-mcp`
as the command. On stdio, server state (approvals DB, audit log, search index)
defaults to `~/.moneybird-mcp` instead of the working directory.

### Option B — Claude Desktop extension (one file, no terminal)

`python scripts/build_mcpb.py` produces `dist/moneybird-mcp-<version>-<platform>.mcpb`.
Double-clicking it (or Claude Desktop → Settings → Extensions → Install) installs the
server with a settings form for the API token — no Python packaging knowledge needed
by the end user. The bundle vendors all dependencies, so it is specific to the
platform + Python minor version it was built on; the user's machine still needs a
system Python ≥ 3.11 on PATH. The bundle pins credential mode to `local`, and the
settings form defaults capability mode to `read_only`. Entering `write_enabled`
explicitly exposes the experimental supervised write surface; review each preview
carefully, because the switch and an approval ID are not independent proof of human
confirmation.

### Option C — run from a clone as an authenticated HTTP server

```powershell
python -m pip install -r requirements.txt
$env:MCP_AUTH_TOKEN = "<long-random-secret>"
$env:MONEYBIRD_CREDENTIAL_MODE = "network_single_user"
$env:MCP_TRANSPORT = "http"
python .\moneybird_mcp_server.py
```

For migration from older clone-based setups, use
`python .\moneybird_mcp_server.py --env-file C:\absolute\operator.env`.
Neither the legacy entrypoint nor package imports load a repository or
working-directory `.env`; this deliberate compatibility break prevents an
untrusted launch directory from changing capability, tenant, listener, or
credential policy. Values already supplied by the parent process always win.

This serves the current streamable-HTTP transport at:

```text
http://localhost:8000/mcp
```

Set `MCP_TRANSPORT=sse` only for a legacy client that still needs `/sse`. The same
HTTP mode is available via `moneybird-mcp --transport http` (add `--host`/`--port`
as needed). The runnable entrypoints use compact Tool Search by default; add
`--tool-discovery full` only for a client that needs every tool schema up front.
Both network transports refuse to start without `MCP_AUTH_TOKEN`; a non-loopback
bind additionally requires a real trusted TLS proxy and
`MCP_TRUSTED_TLS_PROXY=true`.

## Project layout

The server is split into a small package by concern; `moneybird_mcp_server.py`
is just the entrypoint you run.

```text
moneybird_mcp_server.py   # legacy entrypoint: HTTP/SSE with env-driven host/port/auth
pyproject.toml            # PyPI packaging: `moneybird-mcp` console script (stdio default)
mcpb/                     # Claude Desktop extension: manifest + bundle entry script
moneybird/
  server.py               # shared entrypoint: stdio | http | sse (build_config + main)
  config.py               # constants, MoneybirdError, explicit env-file parser, data_dir()
  credentials.py          # explicit local, network-single-user, hosted-request-only modes
  capabilities.py         # read-only default + explicit local/single-user write opt-in
  client.py               # Moneybird REST client (pooled HTTP, retry/backoff)
  http_transport.py       # process-wide keep-alive pool; no default tenant credentials
  task_context.py         # per-tool cache + batch loading for known record ids
  telemetry.py            # bounded privacy-safe API/tool performance counters
  performance_middleware.py # FastMCP timing middleware
  tool_discovery.py       # compact BM25 Tool Search profile
  formatting.py           # pure helpers: titles, money, search-record shaping
  safety.py               # write guards: durable approvals (SQLite) + audit log
  write_contracts.py      # versioned contracts for every approval-backed action
  sync.py                 # bounded-parallel, atomic local search-index sync
  invoicing.py            # bookkeeping logic: journals, invoices, merge/reclassify
  tools/                  # MCP tools, split by domain
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
    workflows.py          #   combined purchase + bank correction preview
    approvals.py          #   stable generic executor for guarded approvals
    ledger.py             #   ledger accounts, general journals, reclassification
    reference.py          #   products, tax rates, projects, time entries, accounts
    reports.py            #   all Moneybird reports
  guidance.py             # the "skill" layer: playbook resource + scenario prompts
  playbooks/
    boekhoud_playbook.md  # deep bookkeeping reference (loaded on demand)
  auth.py                 # required shared-secret auth middleware for HTTP/SSE
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
matching `*_from_approval` tool calls `run_approved_write(...)`, which first enforces the
deployment capability, atomically claims the stored approval, applies duplicate suppression
when that action has a nonempty fingerprint, executes, and records an explicit outcome.
Executors can mark partial, verification-failed, ambiguous, or failed work so it is never
recorded as successful duplicate evidence.
`write_contracts.py` is the fail-closed registry for every approval action. The
generic dispatcher refuses to load if an executor lacks a versioned declaration,
and shared comparison helpers verify every caller-controlled header/line field
rather than relying on record counts or totals alone.
Adding a new write means writing a prepare function and an executor — the safety
plumbing comes for free. A few
multi-step batch flows (batch invoices, meter usage, reclassify, bulk delivery
method) keep hand-rolled executors because they record partial progress on failure.
Clients may always call `execute_approved_action(approval_id)` after confirmation; it
reads the exact stored action and delegates to the existing action-specific executor, without
weakening single-use, expiry, tenant, fingerprint, or audit checks.

This flow is durable write-safety machinery, not an independent confirmation authority. The
same model-visible channel receives the `approval_id` and can call the executor. Use
`write_enabled` only in a supervised local or authenticated single-user deployment whose MCP
client supplies the human-confirmation boundary. Hosted request mode refuses every write even
if the process environment says `write_enabled`.

For a task that combines purchase-invoice reconciliation and bank-booking
reclassification, `prepare_bookkeeping_correction_batch` stages the existing guarded child
actions and returns one combined preview. Its executor preflights all children before the first
write. Moneybird has no cross-object transaction, so a later runtime/API failure is returned and
audited explicitly as partial progress rather than presented as atomic success. A child that
returns a verification-error status also makes the parent partial; returning without raising is
not sufficient for success.

### Performance architecture

- JSON API calls share a keep-alive `httpx` connection pool. Authorization stays on each
  request, so the pool can safely serve multiple tenants without storing a tenant token.
- `MoneybirdTaskContext` caches reference data only for one tool invocation and batch-loads
  known document/invoice/mutation ids in groups of at most 100. The bank-reclassification
  prepare+execute path for `N <= 100` uses approximately `5 + 2N` API calls instead of
  `2 + 6N`, including a separate final readback verification.
- The six versioned sync feeds run with at most three workers. A per-administration lock and
  atomic file replacement protect the JSON cache. `updated_at` records freshness;
  `content_updated_at` changes only when records change, so a no-change refresh does not rebuild
  SQLite FTS.
- Local bounded telemetry records normalized endpoints, durations, retries, status classes and
  tool call totals. It never records tokens, query parameters, request/response bodies, or raw
  numeric record ids. Metrics are grouped by a truncated token-derived pseudonymous scope, so
  `get_server_status` filters to the active credential. That label is not a tenant identity or
  authorization boundary.

On the live development administration (2026-07-29), a no-change sync improved from 4.21 s to
about 0.69–0.86 s (latest repeat: 0.71 s), repeated pooled GETs after connection setup took
about 0.05–0.08 s, and compact
discovery reduced the initial protocol tool schema from 77 tools / 68,260 compact JSON bytes to
9 tools / 6,933 bytes (about 90% smaller). These are reference measurements, not latency
guarantees.

### Durable approvals & server state

Approvals are stored in SQLite (`moneybird_approvals.sqlite3`), so a prepared write
survives a server restart and works across multiple worker processes. Durable local
state (approvals DB, per-administration audit logs, sync/FTS caches, local OAuth tokens) lives in
`MONEYBIRD_MCP_DATA_DIR`. The installed console script defaults it to
`~/.moneybird-mcp`; the legacy clone entrypoint keeps the working-directory default.
Legacy state files in the working directory are still read and migrated where
explicitly supported. Pending approvals expire after 15 minutes. Claimed,
partial, verification-failed, and ambiguous outcomes remain durable for operator reconciliation;
there is no automatic hosted reconciliation service.

For local incident recovery, `scripts/reconcile_execution.py` lists and inspects
unresolved rows and accepts only an evidence-bearing `proven_absent`,
`succeeded_verified`, or `manual_review` decision. It is deliberately an operator
CLI—not an MCP tool—and requires the approval ID to be repeated before resolution.

## 4. Connect it to ChatGPT

According to OpenAI’s current MCP docs, ChatGPT developer mode can connect to a remote MCP server, and data-oriented servers should implement `search` and `fetch`.

Relevant OpenAI docs:

- `https://developers.openai.com/api/docs/mcp`
- `https://platform.openai.com/docs/guides/developer-mode`

To use an authenticated self-hosted network endpoint in an MCP client:

1. Enable ChatGPT Developer Mode in ChatGPT settings.
2. Put the server behind trusted HTTPS and configure a long random `MCP_AUTH_TOKEN`.
3. Confirm that the client can send that bearer credential, then add the public `/mcp` URL.

For local testing, a tunnel can terminate TLS while the server remains on loopback:

```powershell
cloudflared tunnel --url http://localhost:8000
```

Then use the public URL ending in `/mcp` and configure the bearer secret in the client. A tunnel
does not turn the static-secret, single-user server into a production hosted service. Client
authentication capabilities change over time; verify them against the current client
documentation before exposing the endpoint.

## 5. What the tools do

- `get_server_status(recent_tools=20)`: returns local, privacy-safe API/tool latency, call-count, retry, and error aggregates; it makes no Moneybird API call.
- `search(query, limit=8)`: searches contacts, sales invoices, purchase invoices, receipts,
  general journals, and financial mutations. Local/single-user mode can use the sync/FTS cache;
  hosted request mode always uses a partial live scan.
- `fetch(id)`: fetches the full JSON for `contact:<id>`, `sales_invoice:<id>`, `purchase_invoice:<id>`, `receipt:<id>`, `general_journal_document:<id>`, `financial_mutation:<id>`, `ledger_account:<id>`, or `financial_account:<id>`.
- `list_contacts(limit=10, page=1)`: compact contact overview.
- `audit_invoice_delivery_settings(include_archived_contacts=False, include_inactive_recurring=False)`: controleert of contacten op verzendmethode `Email` staan, of er factuur-e-mailadressen ontbreken, en of periodieke facturen risico lopen door `auto_send`/verzendmethode/e-mailinstellingen.
- `list_sales_invoices(limit=10, page=1, state="all", reference="", contact_id="", period="")`: compact invoice overview with extra filtering.
- `audit_recent_sales_invoice_send_methods(limit=30, page_scan_limit=10)`: controleert recente verkoopfacturen en classificeert het oorspronkelijke verzend-event als handmatig, handmatig per e-mail, automatische e-mail, of e-factuur/SI.
- `list_purchase_invoices(limit=10, page=1, filter="", period="")`: compact inkoopfactuuroverzicht.
- `get_purchase_invoice_by_reference(reference)`: resolves an exact supplier invoice number directly through Moneybird's purchase-document filter and returns its current lines with ledger/tax names, attachments, payments, and version; use this instead of broad `search` when the user names an inkoopfactuur.
- `list_receipts(limit=10, page=1, filter="", period="")`: compact bonnen-/overige uitgavenoverzicht.
- `list_general_journal_documents(limit=10, page=1, filter="", period="")`: compact memoriaaloverzicht.
- `read_document_attachment(document_id, attachment_id="", kind="purchase_invoice")`: in local or authenticated single-user mode, downloads the PDF behind a purchase invoice, receipt, or general journal document into bounded memory, retains no file, and parses it in a disposable worker with a 10-second timeout and 256 MiB process-memory cap. It returns at most 40,000 characters from at most 100 pages (20 MiB download cap; requires `pip install 'moneybird-mcp[pdf]'`). Returned text is marked untrusted. Hosted request mode still refuses parsing until durable capacity, backpressure, abuse, and lifecycle controls exist.
- `review_purchase_invoices(period="", limit=100, contact_id="", kind="purchase_invoice")`: finds purchase invoices that need attention — still `new`, booked with fewer lines than the supplier usually gets, missing ledger accounts, a flipped incl/excl-btw flag, or a familiar line description mapped to a different ledger/tax destination. A contact-specific review uses the complete versioned document synchronization feed (paginated list fallback) so older supplier history is not lost after the first page or current-book-year default.
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
- `moneybird_request(path, query=None)`: read-only escape hatch that performs one JSON GET against a finite allowlist generated from the vendored Moneybird OpenAPI routes (e.g. `estimates`, `subscriptions`, `time_entries/123`, `documents/purchase_invoices`). `path` is relative to the administration; use `administrations` for the API root. Binary downloads, unknown paths, writes, traversal, and another administration's paths are refused.
- `get_profit_loss(period)`: reads the Moneybird profit and loss report for the requested period.
- `get_balance_sheet(period)`: reads the Moneybird balance sheet report for the requested period.
- `get_general_ledger(period)`: reads the Moneybird general ledger report for the requested period.
- `get_financial_report(report_name, period, page=0)`: reads any Moneybird report — `profit_loss`, `balance_sheet`, `general_ledger`, `cash_flow`, `tax` (btw), `debtors` / `creditors` (openstaande posten), `debtors_aging` / `creditors_aging`, `revenue_by_contact`, `revenue_by_project`, `expenses_by_contact`, `expenses_by_project`, `journal_entries`, `subscriptions`, `assets`. Note: `cash_flow`, `tax`, `debtors`, and `creditors` accept at most one month of period (`this_month`, `202606`); the aging reports take a whole month as reference date.
- `sync_search_index(invoice_filter="state:all,period:this_year", document_filter="period:this_year", financial_mutation_filter="period:this_year", force_full=False)`: in local/single-user mode, builds or refreshes a local cached search index from Moneybird synchronization endpoints across contacts, sales invoices, purchase invoices, receipts, general journals, and financial mutations. `search` queries it through a derived SQLite FTS5 index, with substring and live API fallbacks. Hosted request mode rejects this tool because its durable artifacts are not yet principal/grant-bound.
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
- `prepare_reclassify_bank_mutation_bookings(entries)`: stages up to 100 direct bank-booking moves between ledger accounts in one approval. Every entry identifies the mutation and exact `ledger_account_booking_id`; the preview stores the mutation version, source ledger, exact signed amount, destination, and payment reference.
- `reclassify_bank_mutation_bookings_from_approval(approval_id)`: preflights the complete batch before the first write, unlinks and re-links each exact amount, verifies the source disappeared and a new target booking appeared while mutation amount/state/open amount stayed unchanged, and attempts to restore the original source booking when a target link fails. Moneybird offers no cross-mutation transaction, so the response reports partial progress explicitly if recovery is needed.
- `prepare_link_bank_mutation_booking(financial_mutation_id, booking_type, booking_id, price="")`: stages linking a bank/cash mutation to an open invoice/document (`SalesInvoice`, `Document`) or directly to a ledger category (`LedgerAccount`) — the manual counterpart of Moneybird's bank reconciliation. Empty `price` links the full open amount.
- `link_bank_mutation_booking_from_approval(approval_id)`: executes the link and verifies the new booking's signed price, the resulting open amount, and the processed state when the mutation is fully closed.
- `prepare_unlink_bank_mutation_booking(financial_mutation_id, booking_type, booking_id)`: stages removing a wrongly matched `Payment` or `LedgerAccountBooking` from a mutation (errors early if the booking id is not on the mutation).
- `unlink_bank_mutation_booking_from_approval(approval_id)`: executes the unlink and verifies the booking is gone.
- `prepare_create_credit_invoice(sales_invoice_id)`: stages duplicating an invoice into a draft credit invoice (negated amounts, nothing sent).
- `create_credit_invoice_from_approval(approval_id)`: executes the credit duplication and verifies the credit total negates the original.
- `prepare_reconcile_purchase_invoice(document_id, reference_document_id="", kind="purchase_invoice", target_total="", relabel_period=True, desired_lines=None, prices_are_incl_tax=None, source_note="")`: stages a purchase-invoice repair. With `desired_lines`, it validates exact PDF-derived descriptions, prices, ledger ids, and tax-rate ids and refuses any split that changes the current total; without them it reproduces and proportionally scales a reference invoice. Both modes store the document version in the approval.
- `reconcile_purchase_invoice_from_approval(approval_id)`: aborts if the document changed after the preview, otherwise executes the staged reconcile and re-fetches the invoice to verify the total, exact line set, tax-price mode, and resulting version.
- `prepare_bookkeeping_correction_batch(bank_reclassifications=None, purchase_reconciliations=None)`: groups related purchase-invoice and bank-booking corrections under one exact preview and approval; mixed plans are globally preflighted before the first write.
- `bookkeeping_correction_batch_from_approval(approval_id)`: executes that combined plan and reports/audits verified completion or explicit partial failure.
- `execute_approved_action(approval_id)`: stable executor for any pending guarded approval; delegates only to the action stored in that approval.

## 5b. Prompts and the playbook (the "skill" layer)

The tools are the hands; this layer is the craft, so someone else's AI client can process
overdue bookkeeping, categorize a year, or read the reports without re-deriving the rules.
It uses progressive disclosure rather than one giant always-on instruction:

- **Always-on, thin** — the behavioral rules live in the server `instructions` (ask before
  executing, never invent data, apply the relevant verifier, propose when unsure, you are not a
  tax advisor). Instructions guide the model; the mechanical boundary is the capability policy.
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

There are three distinct layers:

1. The process defaults to `MONEYBIRD_CAPABILITY_MODE=read_only`; all MCP write executors deny
   mutation. `hosted_request_only` refuses writes unconditionally.
2. The server marks real write tools as destructive with MCP tool annotations.
3. An opted-in local/single-user server uses a durable two-step write flow:
   `prepare_*` only stages the action.
   `execute_approved_action` (or the matching `*_from_approval`) performs the Moneybird write.

Capability denial is application enforcement. Tool annotations and the prepare/execute sequence
do **not** independently prove a human approved: the same model-visible channel receives the
`approval_id` and can call the executor. A trusted MCP client UI may add a real confirmation
boundary, but this repository does not mint or verify that receipt.

If an MCP client offers tool confirmation, keep it enabled for every destructive tool in
addition to the server's default read-only policy. Treat client approval behavior as a separate,
version-specific control and verify it against that client's current documentation.

Relevant OpenAI docs:

- `https://platform.openai.com/docs/guides/tools-remote-mcp`
- `https://developers.openai.com/api/docs/mcp`

## 7. Notes and limits

- The supported default is read-only. Experimental writes require the explicit
  `MONEYBIRD_CAPABILITY_MODE=write_enabled` process opt-in and remain limited to supervised local
  or authenticated single-user operation.
- It now supports contact create/update/archive, ledger account creation, general journal creation, purchase-document reclassification, sales invoice draft creation, and explicit send/schedule as approval-gated actions.
- It now also supports previewed batch invoice creation, batch scheduling with action-specific checks,
  a first-class meter-usage invoice run, duplicate warnings, automatic merge checks for
  scheduled sends, workflow pause/resume, and batch invoice updates.
- It also supports the daily-bookkeeping writes: payment registration on sales/purchase
  invoices and receipts, linking/unlinking bank mutations to invoices, documents, or ledger
  categories (manual bank reconciliation), and duplicating an invoice to a draft credit
  invoice. These flows use their implemented action-specific postcondition checks and closed
  outcomes; they do not provide an independent guarantee of bookkeeping correctness.
- When a new invoice is scheduled for a contact/date that already has exactly one scheduled invoice, the server automatically reuses that invoice's workflow/style/identity defaults before showing the approval preview.
- `search` uses a local synchronization cache when available and falls back to a live first-page scan when no cache exists yet.
- Local/single-user sync caches cover contacts, sales invoices, purchase invoices, receipts,
  general journal documents, and financial mutations. Hosted request mode is live-read-only:
  it neither reads nor builds durable JSON/FTS caches.
- The HTTP client retries transient `429` and `5xx` responses with backoff, which makes multi-step bookkeeping runs much less fragile.
- The HTTP client reuses keep-alive connections, task-local loaders batch known ids, and
  versioned sync feeds run with bounded parallelism. Compact Tool Search is the default to avoid
  sending the full catalog to the model up front.
- The sync cache is stored locally and should not be committed.
- Successful write actions are appended to a per-administration JSONL audit log at `.moneybird_audit_log_<administration_id>.jsonl` (falling back to `.moneybird_audit_log.jsonl` when no administration is set).
- Failed multi-step writes now also append a failure entry with partial progress, which helps with recovery after interrupted bookkeeping runs.
- Moneybird fields and PDF text are untrusted model input. Prompt injection can influence model
  behavior; keep the mechanical capability policy read-only unless an explicitly supervised
  local/single-user workflow accepts that residual risk.
- **Boekingsregels (bank/transaction rules) are not exposed by the Moneybird API**, so the server cannot read or change them. To explain why a bank mutation was not auto-processed, the `diagnose_bankmutatie` prompt and playbook recipe E infer rule behavior from the financial-mutation fields and `created_at`/`processed_at` timing, and point the user to Moneybird's own Boekingsregels settings.
- `list_financial_mutations` returns HTTP 400 ("too many ... use sync API") for a wide period; query per month (`period:"JJJJMM01..JJJJMMnn"`) or use the sync index.
- The `cash_flow`, `tax`, `debtors`, and `creditors` reports accept at most one month of period; the `*_aging` reports require a whole month as reference date (verified live: `{"error":"Period cannot exceed 1 month"}`).
- `docs/moneybird_api_coverage.md` holds the full catalogue of all 296 Moneybird API operations (from the official OpenAPI spec) with per-endpoint coverage status — consult it before wrapping new endpoints.

## 8. Licensing

This project is **source-available, not OSI-approved open source**. It is distributed
under the **MIT License with the "Commons Clause" License Condition v1.0**. The complete
licence text is in [`LICENSE`](LICENSE); the wording below is a plain-language summary and
the `LICENSE` file governs.

The current repository version and all future versions are released under these terms.

**You may**, free of charge:

- inspect and download the source;
- modify it;
- use it personally;
- use it internally within your own organisation.

**You may not**, without a separate commercial licence from the Licensor (`Espaye`):

- sell the Software;
- offer it as a paid or commercial hosted service;
- provide a managed service whose value derives, entirely or substantially, from the
  functionality of the Software;
- repackage or redistribute it commercially as a competing product.

In the Commons Clause's own terms, the licence does not grant the right to **Sell** the
Software, where "Sell" means using the granted rights to provide a product or service to
third parties, for a fee or other consideration, whose value derives entirely or
substantially from the Software's functionality. Commercial sale and substantially
equivalent hosted services therefore require a separate commercial licence.

For commercial licensing enquiries, open an issue at
[github.com/Espaye/moneybird-mcp-server](https://github.com/Espaye/moneybird-mcp-server/issues)
or contact the repository owner.

Package metadata declares the SPDX expression `LicenseRef-MIT-Commons-Clause-1.0`. Because
the combination has no standard SPDX identifier, PEP 639's custom `LicenseRef-` form is used
deliberately; it is not a placeholder and must not be replaced with plain `MIT`.

Third-party dependencies keep their own licences, which are unaffected by this condition.
