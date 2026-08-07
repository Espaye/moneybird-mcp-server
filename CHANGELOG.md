# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and semantic
versioning while allowing pre-1.0 breaking changes.

## Unreleased

### Added

- **`moneybird-mcp auth login | status | logout | scopes`** — first-class local OAuth.
  A user registers an external Moneybird application once, supplies its client id and
  secret, and runs `auth login`; the command prints (and optionally opens) the
  authorization URL, takes the short out-of-band code Moneybird displays, exchanges it,
  verifies the result, selects the administration, and stores the connection. No
  Moneybird access or refresh token is ever copied by hand, and the MCP client
  configuration needs no credentials at all. `auth status` reports which identity is
  active and from where; `auth logout` deletes local credentials and says plainly that
  Moneybird publishes no revocation endpoint, so access is withdrawn at
  <https://moneybird.com/user/applications>. `python -m moneybird_mcp.oauth_login` and
  `python scripts/oauth_login.py` remain as aliases for `auth login`.
- **Administration selection at login.** A connection reaching exactly one
  administration selects it; several are listed and offered interactively, or chosen
  with `--administration ID`. Nothing is picked silently, because a guessed
  administration sends every later write to the wrong books. The choice is stored with
  the connection, so `MONEYBIRD_ADMINISTRATION_ID` is no longer needed after an OAuth
  login — an explicit environment value still overrides it, and `auth status` flags the
  override.
- **`moneybird_mcp/oauth_scopes.py`** — the requested scopes with a documented rationale
  per tool area, named profiles (`full`, `bookkeeping`, `invoicing`), and validation that
  rejects an unknown scope before the browser opens. Selectable with `--scopes` or
  `MONEYBIRD_OAUTH_SCOPES`. Moneybird scopes have no read-only variant and are *not* a
  write policy: that remains `MONEYBIRD_CAPABILITY_MODE` plus the
  prepare/approve/execute flow. Rows the mapping infers (products, projects, reports)
  are marked as inferred rather than presented as documented.
- **`moneybird_mcp/oauth_store.py`** — a typed `OAuthConnection` and a `TokenStore`
  interface with a local owner-only, atomically written JSON implementation. Every
  method takes the profile explicitly, so a hosted backend can swap in per-tenant
  storage without touching the API client. `OAuthConnection` redacts both tokens in its
  `repr`/`str`, so a traceback or a `%r` cannot leak one.
- `suggest_bank_mutation_matches`: for each unprocessed bank mutation, the open sales
  invoice, purchase invoice, or receipt it most likely settles. Moneybird's own
  transaction screen suggests matches and auto-links at full certainty, but nothing
  in the API exposes that, so this reproduces it deterministically — invoice reference
  found in the bank description, exact open amount, counterparty IBAN, contact name —
  and returns every candidate with the reasons that fired. Confidence is a tier
  (`exact`/`strong`/`possible`), never a score, and an equally-good runner-up is
  reported as `ambiguous` rather than broken by an arbitrary tie-break. It writes
  nothing: linking still goes through `prepare_link_bank_mutation_booking` and explicit
  approval. Replayed against real already-linked mutations it picked the correct invoice
  top-1 in every decidable case and flagged the one genuine tie (a repeating monthly
  invoice of identical amount, paid without a reference) instead of guessing.
- `get_bookkeeping_guide(topic)` and `list_bookkeeping_guide_topics`, making the Dutch
  bookkeeping playbook model-callable per topic. It was published only as an MCP
  resource, which is read by the *client*: Claude Desktop requires the user to attach it
  by hand and ChatGPT connectors do not read arbitrary resources at all, so the deepest
  domain knowledge in this server was unreachable in the two clients that matter most.
  The resource remains for clients that do use resources.
- `get_server_status` now reports the observed Moneybird rate-limit budget per bucket and
  the reference-cache state, so a task that slows down or fails can be attributed to the
  per-IP throttle rather than to the server.

### Removed

**Breaking.** The advertised tool catalogue goes from 85 tools / 84,399 bytes to
**57 tools / 69,436 bytes**, so the full list is comfortably shippable to every client
and no discovery layer is needed in front of it.

- **The 24 action-specific `*_from_approval` tools are no longer registered as MCP
  tools.** `execute_approved_action` already dispatched all of them, and it is the one
  tool carrying the destructive annotation an MCP client actually enforces its
  confirmation policy on. The Python functions remain — `tools/approvals.py` dispatches
  to them and scripts and tests call them directly — so only the MCP surface changed.
  Callers naming one of these tools must switch to
  `execute_approved_action(approval_id)`.
- `get_profit_loss`, `get_balance_sheet`, and `get_general_ledger` — strict subsets of
  `get_financial_report(report_name, period)`.
- `list_receipts` and `list_general_journal_documents` — folded into
  `list_purchase_documents(kind=...)`, which replaces `list_purchase_invoices` and takes
  `purchase_invoice`, `receipt`, or `general_journal_document`.
- `search_contacts` and `get_contact_by_customer_id` — folded into `list_contacts`,
  which now takes an optional `query` (partial name, e-mail, phone, city, customer id)
  or an exact `customer_id`, and still pages through everything when given neither.

### Changed

- **Token-endpoint failures now say what went wrong.** `HTTP 400` alone is not
  actionable, so the RFC 6749 `error` / `error_description` fields are extracted and
  paired with specific guidance: `invalid_client` points at the application credentials,
  `invalid_grant` explains that codes are single-use and expire. Only those two fields
  are ever quoted — the token endpoint is the one endpoint whose responses contain
  credentials, so the body is never rendered wholesale. Timeouts and network failures are
  reported distinctly, and neither grant is retried automatically: repeating a consumed
  authorization code turns a blip into a dead grant, and a refresh may rotate the refresh
  token.
- OAuth `--env-file` handling, the state-directory default and the "no `.env` is ever
  discovered" rule are unchanged and now covered by a subprocess regression for the auth
  commands specifically, since those read the OAuth application credentials.
- **The runnable server now defaults to `--tool-discovery full` instead of `search`.**
  Compact discovery shrinks the advertised catalogue to ~7 KB, but that catalogue lives
  in the client's *cached* prompt prefix, so the saving is nearly free anyway — while
  every task pays an extra `search_tools`/`call_tool` round trip, and each `search_tools`
  answer is itself 6–12 KB of uncached output. With the catalogue now at 57 tools the
  full list is small enough that the trade never pays off. BM25 ranking is also
  English-biased: `meterstanden factureren` returned no tools at all, and `betaling boeken
  op factuur` omitted `prepare_register_payment` entirely. Compact mode remains available
  via `--tool-discovery search` / `MCP_TOOL_DISCOVERY=search` for clients that cannot take
  the full list.
- Ledger accounts, tax rates, and the administration-membership revalidation are cached
  in-process for a short TTL, keyed on a salted digest of the access token plus the
  administration id, and disabled entirely in `hosted_request_only` mode. Measured on a
  live administration: a repeat `list_ledger_accounts` went from ~390–630 ms (43 KB on the
  wire) to ~0 ms, and a repeat `search` from ~107 ms to ~5 ms, because the membership
  round trip — not the local index, which costs ~6 ms — was dominating it. Tune with
  `MONEYBIRD_REFERENCE_CACHE_SECONDS` / `MONEYBIRD_MEMBERSHIP_CACHE_SECONDS`; `0` disables.
- `search` hits now carry `date`, `amount`, `state`, and `contact_id`, so choosing between
  results usually no longer needs a `fetch` per candidate (a single sales invoice returns
  ~9.7 KB of raw record). The sync index and its FTS cache gained matching schema versions
  and rebuild themselves when the record shape changes.
- `review_purchase_invoices` and reference-based reconciliation resolve a supplier's
  history from the local sync index, which now stores each document's `contact_id`. The
  previous path fetched every purchase document in the administration in batches of 100
  and filtered client-side; its early exit compared against the *match* limit, so on any
  normal administration it never fired. The scan remains as a fallback, now reading
  newest-first and stopping honestly when the rate budget runs out.
- The live-fallback `search` runs its six source scans concurrently (bounded at three
  workers, each still failing independently) instead of serially.
- A `429` whose window outlasts the retry cap now fails immediately with the bucket, the
  documented limit, and when it frees up, instead of silently burning the remaining
  retries against a five-minute window and spending more of an exhausted budget.

### Fixed

- **A token refresh no longer discards the refresh token or the granted scopes.**
  Moneybird may answer a refresh-token grant with only a new access token; the whole
  stored record was previously replaced by that response, so the refresh token vanished
  and the next expiry would have forced a re-login. An absent field now means
  "unchanged", never "cleared". A *failed* refresh raises and leaves the stored
  credentials exactly as they were, so an unreachable Moneybird cannot cost a user their
  grant, and the message names re-authentication as the remedy only when the grant is
  actually the problem.
- `suggest_bank_mutation_matches` reads Moneybird's rate-limit headers correctly.
  They do not follow the IETF RateLimit draft: `RateLimit-Remaining` is *seconds until
  reset* (it tracks `RateLimit-Reset` minus now, and exceeds `RateLimit-Limit`, which a
  request count cannot), `RateLimit-Reset` is an absolute Unix epoch rather than a delay,
  and the actual request count is in the undocumented `RateLimit-RequestsRemaining`.
  A `remaining` value above `limit` is now discarded rather than believed.
- Unpaid documents are selected with the correct per-type state vocabulary.
  `state:open` is accepted on a purchase invoice and returns zero rows, because an unpaid
  purchase invoice is `late` or `new`, never `open` — a silent empty result. Moneybird
  accepts pipe-separated alternatives, so the whole unpaid set is now one request
  (`state:open|late|reminded|pending_payment` for sales invoices,
  `state:open|late|new|pending_payment` for documents).

## 0.6.1 — 2026-08-05

### Added

- A small typed, versioned product-workflow catalogue exposed through
  `list_supported_workflows`. The checked-in catalogue is generated from the same
  registry and lists only workflows integrated and tested end to end.
- A product/pricing vertical slice: read-only product inventory auditing, exact-decimal
  percentage/fixed/explicit price analysis, and guarded product-only bulk price updates
  with exclusions, rounding, derived semantic duplicate suppression, concrete
  administration/source-version preflight, honest known-partial outcomes, and independent
  read-after-write verification. Product updates explicitly do not claim to change
  invoices, recurring invoices, subscription templates, or subscriptions.

- `get_server_status` now reports the package version and credential state. In
  local or single-user mode it remains callable without credentials and returns
  the setup problem as data instead of failing like a Moneybird API tool.
- `moneybird_mcp/oauth_login.py`, so the interactive OAuth login runs from an installed
  package as `python -m moneybird_mcp.oauth_login`. `scripts/oauth_login.py` is now a thin
  wrapper for source checkouts; the wheel ships only the `moneybird_mcp` package, so error
  messages could not usefully point at a path under `scripts/`.
- A startup warning when a local or single-user server starts without configured
  Moneybird credentials. An MCP client reports any server that starts as connected, so
  a forgotten token previously first surfaced as a failed answer to a real question.
  Startup is not aborted: credentials can still arrive through an OAuth login later.
  The check reads local configuration only — it never contacts Moneybird and never
  refreshes or rewrites the OAuth token store, so a slow upstream cannot delay the
  server's first connection.
- The same condition is also prepended to the server instructions, which every MCP
  client hands the model at connect time, so the user is told in the conversation on
  their first question instead of receiving a tool error. The server log is the wrong
  channel on its own: an MCP client shows any server that starts as connected, and
  nobody opens the log. The notice states that it is written at startup and cannot see
  a later fix, so a user who configures credentials mid-session is not told to keep
  waiting.

- Dutch translations of the onboarding documentation: `README.nl.md`,
  `docs/getting-started.nl.md`, and `docs/data-lifecycle.nl.md`. The English and Dutch
  pages cross-link through a language selector, and `README.nl.md` ships in the source
  distribution. Technical reference, security, and maintainer documentation stay in
  English. `tests/test_licensing.py` now also pins the Dutch licence statement, so a
  translation cannot soften the source-available terms.

### Changed

- **Breaking:** renamed the installed Python import package from `moneybird` to
  `moneybird_mcp`, matching the distribution name and avoiding file ownership
  collisions with the unrelated `moneybird` project on PyPI. The console command
  remains `moneybird-mcp`; module invocations now use, for example,
  `python -m moneybird_mcp.oauth_login`. The release version is 0.6.1 so source
  checkouts and installed 0.5.0 builds no longer report the same identity.
- Reworked the public documentation around a short onboarding-first `README.md`, with
  dedicated getting-started, deployment-and-safety, tool-reference, and local-data-lifecycle
  guides under `docs/`. Simplified `SUPPORT.md`. The README no longer doubles as the full
  manual; it links to the new pages instead.
- Added PyPI, documentation, changelog, and security-policy links to the `pyproject.toml`
  project URLs, so they appear in the package metadata sidebar on PyPI.
- **Relicensed from MIT to MIT with the "Commons Clause" License Condition v1.0.**
  The project is now **source-available, not OSI-approved open source**. Inspecting,
  downloading, modifying, personal use, and internal use within your own organisation
  remain permitted free of charge. Selling the Software — including offering it as a paid
  or commercial hosted service, providing a managed service whose value derives entirely or
  substantially from its functionality, or repackaging it commercially as a competing
  product — now requires a separate commercial licence from the Licensor (`Espaye`).
  Enquiries go to the repository owner via a GitHub issue.
- Package metadata now declares the PEP 639 custom expression
  `LicenseRef-MIT-Commons-Clause-1.0` in `pyproject.toml` and `mcpb/manifest.json` instead
  of plain `MIT`. The combination has no standard SPDX identifier; the `LicenseRef-` form is
  deliberate and must not be replaced with a well-known id to satisfy tooling.
  `tests/test_licensing.py` pins the licence text and both metadata declarations so the
  project cannot silently revert to plain MIT.
- Added a licensing section to `README.md` and a contribution-licensing statement to
  `CONTRIBUTING.md`.

This change applies to this and all future versions. It does not retroactively withdraw the
MIT terms from copies already obtained under earlier releases.

Version `0.4.0` and earlier were published under plain MIT. On 2026-07-31 those artifacts were
withdrawn from normal public distribution: PyPI releases `0.1.0` through `0.4.0` were deleted,
as were the GitHub releases for `v0.3.0` and `v0.4.0` and their attached assets. The `v0.3.0`
and `v0.4.0` tags and the project's Git history were deliberately left intact. That withdrawal
is a distribution decision, not a revocation of rights already granted; copies may persist in
third-party mirrors and caches outside this project's control. Third-party dependencies keep
their own licences.

### Fixed

- Batch sales-invoice updates now reject unknown or lookup-only fields instead of
  silently dropping them, and both prepare and execute refuse an empty patch. Credit
  invoices are verified against Moneybird's actual duplication contract — negated
  total plus draft state — without predicting provider-owned header/line layout.
- Ambiguous post-write failures now immediately warn that the mutation may already
  have been applied and must be reconciled before retrying. Direct bank bookings to
  profit-and-loss ledger accounts warn that they create no VAT posting.
- VAT analysis detects settlement journals by exact period and participating VAT
  accounts, reconstructs the pre-settlement movements, and no longer diagnoses an
  already-cleared period as anomalous. The same period-based rule blocks a second
  settlement under a different reference during both prepare and execution.
- Profit/loss and balance-sheet rows now include ledger names, numbers, and account
  types, and the common report tools have useful period defaults. Archive and pause
  previews disclose their operational consequences, while common Dutch bookkeeping
  phrases participate in compact tool search.
- **Purchase-invoice reconciliation now preserves totals in both VAT price modes.**
  Reference lines stay excl. tax when `prices_are_incl_tax` is false; scaling and
  cent rebalancing are checked against the calculated incl.-tax total before an
  approval is staged. This also fixes the combined bookkeeping-correction flow.
- Ledger-account creation now requires the Moneybird-mandated RGS 3.5 code in
  its tool schema and verifies it after creation. Ledger listings expose existing
  taxonomy codes, and VAT-rounding guidance suggests only semantically adjacent
  difference accounts instead of arbitrary expense categories.
- Direct MCP calls now receive the same compact argument-validation errors as
  compact `call_tool`. Status and prepare responses expose capability mode,
  read-only execution denials are audited, reclassification previews prefer the
  true excl.-tax line total, unlink previews/verification are complete, imported
  financial accounts resolve their identifier, and MCP serverInfo reports the
  package version. The beta version is now 0.6.1.
- Bank-mutation links with an omitted `price` now stage the mutation's current
  `amount_open` as an explicit signed price, so Moneybird never receives a nil
  amount. A verified no-op is reported as `failed`, not partial completion.
  Compact mutation lists now count invoice/document payments as bookings as
  well as direct ledger bookings, expose `amount_open` and `settlement_state`,
  and fall back to the account identifier/IBAN when an imported bank account has
  no display name.
- VAT analysis now validates its explicit whole-month range before any API call
  and no longer resolves or advertises the rounding account it does not use.
  Settlement preparation resolves `Afrondingsverschillen` only when the declared
  amount actually creates a non-zero rounding line; a missing required account
  names `rounding_ledger_account_id`, points to
  `prepare_create_ledger_account`, and shows at most three plausible candidates.
- Sales-invoice send approvals now bind and summarize the resolved delivery
  method, invoice number, total, and email recipient. Purchase-review reasons
  render ledger numbers/names instead of opaque ids and require at least two
  prior supplier invoices before calling a pattern "usual". Batch invoice
  preparation errors identify the failing zero-based entry and its reference.
- Compact `call_tool` validation failures now omit Pydantic internals, input
  dumps, and documentation URLs. Potential-invoice-duplicate warnings now
  include currency and excl./incl.-VAT totals.
- **Compact discovery can no longer hide a write behind unannotated `call_tool`.**
  The proxy now advertises itself as read-only, accepts only targets explicitly
  annotated read-only, and omits action-specific write executors from search
  results. Direct calls to those hidden executors are also rejected instead of
  falling through FastMCP's underlying catalog. Approved mutations must use the
  directly exposed, destructively annotated `execute_approved_action` tool,
  allowing the MCP client to enforce its destructive-tool confirmation policy.
- **Draft-invoice approvals now show the money being approved.** Single and batch
  invoice previews resolve the effective VAT setting/rate and return explicit
  quantity, unit price, per-line excl./VAT/incl. subtotals, per-invoice totals,
  and currency-grouped batch totals. The incl.-VAT total is included in the
  approval summary. A product's tax and ledger defaults are resolved when there
  is no previous invoice; otherwise callers must provide a tax rate when no safe
  preview default exists. Payment previews already showed both the payment and
  open document amount and remain unchanged.
- Added Windows upgrade recovery guidance: quit the MCP client before a `pip`
  upgrade, rerun after `WinError 32` to repair a partial uninstall, and prefer
  `uvx` to avoid replacing an in-use console script. Claude Code setup now also
  explains local/project/user scopes and the missing-tools symptom outside the
  scope where a server was registered.
- **Moneybird's own explanation of a rejected write is no longer thrown away.** A failed
  create reported only `Moneybird returned HTTP 422 for operation /:id/contacts.json`,
  while the response body naming the field and the reason
  (`send_invoices_to_email: includes a domain which cannot receive emails`) was read into
  a local variable and discarded. Neither a user nor an agent could correct the input from
  that message. Errors now carry the reported reason, bounded so an unexpected body cannot
  flood a tool result or the audit log, and are raised as `MoneybirdHTTPError` (a
  `MoneybirdError` subclass, so existing handlers are unaffected).
- **A rejected write is closed as failed instead of left unresolved.** Every error after
  the dispatch boundary was recorded as `ambiguous`, so a typo in an email address
  permanently wrote an unresolved entry into the bookkeeping audit trail and burned the
  approval. `ambiguous` is now reserved for what it is for — timeouts, 5xx, network
  failures, where the write genuinely may have landed. A status Moneybird answered with a
  refusal proves the refused request changed nothing. The proof is required, not assumed:
  the HTTP client counts accepted mutations for the write being executed, so a refusal
  that follows an already-accepted write still stays unresolved, and 409 Conflict is
  deliberately excluded because it can mean the record already exists. Moneybird's batch
  readers are POSTs to `.../synchronization.json`, so a mutation is identified by
  `retry_safe` rather than by HTTP method — counting a bulk read as a write would have
  dragged the next rejection straight back to unresolved.
- **Tool search returns the tool a plain request describes.** `search_tools` ranked
  `prepare_create_credit_invoice` first for "create a new contact", and did not return
  `prepare_create_contact` at all for "add contact". BM25 weights rare words heavily:
  "new" is rare across the catalogue while "contact" appears in a dozen tool names.
  Descriptions now carry the words users actually type. Across a set of plain-language
  requests, top-1 accuracy went from 2/10 to 9/10. This costs nothing in protocol bytes —
  the affected tools are hidden in compact discovery mode, so the catalogue is
  byte-identical.
- A deliberate refusal no longer renders as a crash. Every `MoneybirdError` raised by a
  tool — missing credentials, a rejected period, a failed precondition — was an exception
  type FastMCP did not recognise, so it was logged with `logger.exception` and rendered as
  a boxed multi-frame traceback with source lines in the MCP client log. The tool surface
  now translates these into FastMCP's `ToolError`, which it logs without a traceback, and
  writes the reason itself as a single line. Direct Python callers still see
  `MoneybirdError` unchanged.
- The missing-credentials message now matches the credential mode that is actually
  running. In local mode it no longer suggests `X-Moneybird-Token` (read only in hosted
  request mode) or `scripts/oauth_login.py` (absent from the wheel).

## 0.4.0 — 2026-07-31

### Security

- Removed import-time and working-directory `.env` discovery. Configuration files
  are now loaded only through an explicit `--env-file PATH`, and cannot override
  values supplied by the parent process.
- Defaulted the server and Desktop bundle to `read_only`; experimental writes now require
  an explicit local or authenticated single-user capability opt-in.
- Forced hosted request mode to live reads only: all writes, durable search sync/cache
  access, and attachment download/parsing are refused without credential fallback.
- Required bearer authentication for every network transport and an explicit trusted
  TLS-proxy acknowledgement for non-loopback binds.
- Bounded PDF downloads and extraction, removed current attachment-file retention, and
  hardened signed-storage redirect handling with validated DNS-to-TCP address pinning.
- Moved local PDF parsing into a disposable spawned process with a hard wall-clock
  timeout and Unix/Windows process-memory containment.
- Added capability-aware CodeQL, pinned Bandit scanning for private repositories, and
  scheduled full-history Gitleaks scanning; release artifacts now require reproducible
  builds and a CycloneDX SBOM, while newly published PyPI artifacts require verified
  publish provenance.
- Made numeric write inputs reject trailing junk, ambiguous separators, non-finite
  values, non-positive payments, and zero-value explicit bank links.
- Applied best-effort owner-only modes to explicitly configured data directories and
  current approval, audit, OAuth, sync, and FTS state files.
- Hardened the loopback gateway demo's identifiers and single-process JSON writes while
  documenting its plaintext, URL-key, and production no-go limitations.
- Added vulnerability reporting, supported-deployment guidance, and reconciled threat/data
  boundaries.
- Added direct transport tests for numeric-address TCP pinning with original-hostname
  TLS verification, closed mixed/non-public DNS answers, and fixed Python 3.14
  compatibility in the pinned HTTPS handler.

### Changed

- Added a scoped Ruff correctness/import gate and a 70% CI coverage regression floor.
- Replaced push-triggered publication with a default-branch-only manual release
  dispatch that requires the exact version and full commit SHA, refuses any
  existing PyPI version/tag/release, and never overwrites release assets.
- Added durable atomic write claims, typed outcomes, and action-specific postcondition
  checks where defined; partial or ambiguous execution is no longer presented as verified
  success, without claiming independent bookkeeping correctness.
- Added fallback semantic fingerprints to every staged action, captured live pre-state
  for repeatable contact/invoice workflow changes, and kept every executor exception
  unresolved when a dispatch may have occurred. Contact and several create/workflow
  actions now use independent post-write reads.
- Added a versioned WriteSpec registry for all approval executors, exact
  caller-controlled header/line comparison, complete batch preflights, and
  occurrence-aware payment, credit, link, and unlink verification.
- Persisted claim owner, attempt, execution phase, dispatch timing, and reconciliation
  evidence. Added a local-only operator reconciliation CLI; unresolved dispatched
  work remains duplicate-blocking until explicitly resolved.
- Bound repeatable bank link/unlink identities to the mutation occurrence so a
  verified unlink can be followed by a later legitimate relink/unlink cycle.
- Bound bank-link approvals to the exact target occurrence, require the
  independent post-read to match both booking id and type, expose only the three
  link types whose target can be proven, and preserve invoice-currency amounts
  instead of translating them as ledger base amounts.
- Made hosted search use live Moneybird reads with membership revalidation instead of local
  JSON/FTS state.
- Hardened release automation with a `main` ref guard, source/tag verification, dependency
  audit, exact artifact matrix tests, late tag re-verification, tested-candidate/PyPI
  digest comparison, and final GitHub tag/asset repair and verification from published
  artifacts. Partial or yanked PyPI state now fails closed; legacy repair uses helpers
  from the guarded workflow commit, whose review trust still depends on repository
  controls.
- Added a lowest-supported-dependency test lane, pinned the isolated build backend,
  required two-build hash equality, emitted a reproducible CycloneDX SBOM, and
  required cryptographic verification of Trusted Publishing attestations for new
  publications.
- Made the reproducibility check emit the exact compared artifacts, so CI and the
  release publisher cannot accidentally use a separately built, non-deterministic
  wheel or source distribution.
- Documented that production release readiness still requires a `main`-restricted,
  independently reviewed `pypi` environment and a protected `v*` tag ruleset.
- Declared Pydantic as a direct runtime dependency and aligned release/package metadata.

## 0.3.0

- Published the current compact-discovery Moneybird MCP beta with 77 tools, OAuth helpers,
  durable approvals, audit logging, incremental sync, attachment extraction, and CI/release
  automation.

Earlier changes predate this maintained changelog; consult Git history for details.
