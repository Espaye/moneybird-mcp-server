# CLAUDE.md — operating guide for this repo

Guidance for an AI agent working in this repository. The goal: be able to read from
and (carefully) write to the live Moneybird administration **without rediscovering the
setup each time**. Read this before doing live Moneybird work.

## What this is

A Moneybird MCP server (FastMCP). The MCP tools in `moneybird/tools/` are the surface
a chat model normally calls. When you work *in this repo* those tools are usually **not**
wired up as live MCP tools, so to actually touch the administration you run small Python
scripts that import the package directly (see below).

## Credentials — already handled, don't hand-roll it

- Local/single-user credentials live in `.env` at the repo root:
  `MONEYBIRD_ACCESS_TOKEN` and `MONEYBIRD_ADMINISTRATION_ID`.
- `moneybird/config.py` calls `load_local_env()` **on import**, which loads that `.env`
  into the environment. As of the cwd-independent fix it looks in the current directory
  *and* next to the package, so **just `import moneybird...` and credentials are present**
  — regardless of where you run Python from. Do not parse `.env` yourself.
- Real env vars always win (`setdefault`). Credential resolution then follows the explicit
  deployment mode:
  - `local` (stdio only): request context → env → local OAuth profile;
  - `network_single_user`: authenticated network edge → env → local OAuth profile; request
    Moneybird headers are rejected;
  - `hosted_request_only`: one nonblank trusted request context; no env/OAuth fallback.
- **OAuth**: `moneybird/oauth.py` implements the authorization-code flow (app credentials in
  `.env` as `MONEYBIRD_OAUTH_CLIENT_ID`/`MONEYBIRD_OAUTH_CLIENT_SECRET`; interactive login via
  `python scripts/oauth_login.py`, out-of-band redirect). Tokens persist in
  `moneybird_oauth_tokens.json` in the data dir and are used automatically in local or
  network-single-user mode when `MONEYBIRD_ACCESS_TOKEN` is absent. Hosted request mode never
  reads that store.

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

1. **The supported default is mechanically read-only.** MCP write execution requires
   `MONEYBIRD_CAPABILITY_MODE=write_enabled`, and hosted request mode refuses all writes
   regardless of that environment value.
2. **Never write without explicit user confirmation.** For a deliberately enabled local or
   authenticated single-user run: read → show an exact preview → wait for a clear "yes" → only
   then execute. The approval ID is model-visible and does not independently prove that "yes";
   this is an operator rule unless the MCP client supplies a trusted confirmation UI.
3. Never invent data (amounts, references, dates, ledger/tax ids). Ask or leave blank.
4. Apply the action-specific verifier. For a reclassification or incl/excl conversion, verify
   the document total is unchanged to the cent; other writes have different invariants.
5. Writes go through `update_document` / `update_sales_invoice` with
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
  fetch a period and filter client-side on the contact. When the user gives an exact supplier
  invoice number, use `get_purchase_invoice_by_reference` (or
  `MoneybirdClient.get_document_by_reference`) instead of broad `search`; it uses Moneybird's
  server-side `reference:` filter and then requires an exact match.
- **Moneybird's boekingsregels vullen ook inkoopfacturen automatisch in — inconsistent.**
  Dezelfde regels die bankmutaties boeken, vullen inkomende inkoopfacturen in, maar niet
  betrouwbaar: dezelfde leverancier komt de ene maand met de vaste meerregelige splitsing binnen
  en de volgende als één verzamelregel, nog in status `new`, soms met `prices_are_incl_tax`
  omgedraaid. De regel zelf zie/repareer je niet (niet in de API), alleen het resultaat. Gebruik
  `review_purchase_invoices` om afwijkers te vinden (nog `new`, minder regels dan gebruikelijk,
  ontbrekende grootboeken, een afwijkende btw-vlag, of een bekende omschrijving die ineens op een
  ander grootboek/btw-tarief staat) en `prepare_reconcile_purchase_invoice`
  → `reconcile_purchase_invoice_from_approval` om de vaste boeking van een goede referentiefactuur
  te reproduceren. Regelprijzen worden naar het doeltotaal geschaald zodat het documenttotaal tot
  op de cent gelijk blijft; wijken de totalen af, dan is de regel-voor-regel-splitsing een (in de
  preview gemarkeerde) aanname. De echte splitsing lees je van de factuur-PDF met
  `read_document_attachment` en geef je als exacte `desired_lines` mee; die modus valideert ids en
  weigert een verdeling die het totaal wijzigt. Contactgerichte reviews gebruiken de volledige
  versioned sync-feed (met paginering als fallback), zodat oudere boekjaren niet door Moneybirds
  impliciete huidige-boekjaarfilter verdwijnen. Elke reconcile
  slaat de documentversie op en breekt vóór de PATCH af als de factuur sinds de preview is gewijzigd.
  (Ontwerp: `docs/reading_pdf_attachments.md`; logica: `moneybird/purchase_reconcile.py`.)
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
  en `prepare_create_credit_invoice`. Voor een reeks bestaande directe bankboekingen die naar
  een ander grootboek moeten, gebruik je `prepare_reclassify_bank_mutation_bookings` →
  `reclassify_bank_mutation_bookings_from_approval`: volledige preflight op mutatieversie en
  bronboeking, exact bedrag, nacontrole van state/open bedrag, plus herstelpoging bij mislukte
  relink. Moneybird biedt geen transactie over meerdere mutaties, dus partial failures worden
  expliciet als zodanig geaudit. Gebruik deze flows, geen handmatige constructies.
  Let op de afwijkende API-conventie (live bevestigd op 2026-07-29): de
  `link_booking`-request verwacht `price_base` als **positieve grootte**, terwijl Moneybird op de
  teruggegeven betaling/grootboekboeking een ondertekend `price` gebruikt. De client vertaalt
  daarom het ondertekende toolbedrag aan de HTTP-grens. De uitvoerder controleert daarna ook het
  teruggegeven teken, `amount_open` en de verwerkte status; alleen "er verscheen een koppeling" is
  niet voldoende bewijs van succes.
- **Groepeer samenhangende correcties in één taakpreview.** Gebruik
  `prepare_bookkeeping_correction_batch` wanneer een opdracht zowel inkoopfactuurcorrecties als
  directe bankherclassificaties bevat. De workflow maakt één exact `approval_id`, preflight alle
  child-acties vóór de eerste write en wordt na het akkoord uitgevoerd met
  `execute_approved_action`. Moneybird heeft geen transactie over verschillende objecten; een
  runtimefout kan dus nog steeds een expliciet geaudit `completed_with_errors`-resultaat geven.
  Een child die zelf `completed_with_errors` of een verificatiefout retourneert, maakt de parent
  eveneens `completed_with_errors`; alleen volledig geverifieerde children tellen als voltooid.
  Voor elk los `prepare_*`-resultaat mag eveneens `execute_approved_action(approval_id)` worden
  gebruikt; die kiest uitsluitend de executor van de opgeslagen, nog geldige approval.

## Performance architecture (verified 2026-07-29)

- De runnable server gebruikt standaard compacte tool discovery (`search`): zeven
  kern-tools plus FastMCP's `search_tools`/`call_tool` worden vooraf aangeboden. Dit verlaagt de
  protocolcatalogus van 77 tools / 68.260 compacte JSON-bytes naar 9 tools / 6.933 bytes
  (circa 90% kleiner). Start tijdelijk met
  `--tool-discovery full` of `MCP_TOOL_DISCOVERY=full` voor oude clients die geen Tool Search
  ondersteunen. Directe package-importen blijven standaard `full` voor compatibiliteit.
  De legacy entrypoint importeert tools pas na CLI/`.env`-verwerking, zodat ook
  `python moneybird_mcp_server.py --tool-discovery full` werkelijk naar `full` schakelt.
- `moneybird/http_transport.py` beheert één luie `httpx`-connection pool. Authenticatie blijft
  per request/tenant en staat nooit op de gedeelde client. In een live meting kostte de eerste
  identieke GET circa 0,29 s en hergebruikte GETs circa 0,05–0,08 s.
- `MoneybirdTaskContext` cachet referentiedata binnen één tool-invocation en haalt bekende
  document-, factuur- en mutatie-id's in groepen van maximaal 100 via de sync-endpoints. Cache
  nooit tussen taken/tenants. De bankbatch gebruikt hierdoor voor `N <= 100` ongeveer `5 + 2N`
  API-calls voor prepare+execute in plaats van `2 + 6N`, inclusief een aparte eindverificatie.
- Duurzame JSON/FTS-sync is alleen beschikbaar in local/network-single-user mode. Hosted request
  mode gebruikt uitsluitend de gedeeltelijke live zoekfallback en weigert `sync_search_index`.
  `read_document_attachment` is daar eveneens uitgeschakeld totdat PDF-werk in een apart
  begrensd proces draait.
- De zes sync-feeds lopen begrensd parallel (maximaal drie workers), met een lock per
  administratie en atomaire JSON-save. `updated_at` is de freshness-tijd; alleen
  `content_updated_at` triggert een FTS-rebuild. Een live no-change sync daalde van 4,21 s naar
  0,69–0,86 s (laatste herhaling: 0,71 s).
- `ToolTelemetryMiddleware` en de HTTP-client verzamelen begrensde, in-memory
  latency/call-count/retry-statistieken. `get_server_status` leest die lokaal. Telemetrie bevat
  geen tokens, queryparameters, bodies of responses; numerieke record-id's worden uit
  endpointnamen verwijderd. Metrics worden gegroepeerd op een afgekorte, token-afgeleide
  pseudonieme scope. Dat label filtert procesmetrics maar is geen tenantidentiteit of
  autorisatiegrens.
- Duplicate-suppression blijft geldig na een latere `failed`/`partial_failure`-auditregel.
  Alleen een expliciete, nieuwere `invalidated`-regel heft een bewezen succes voor exact
  dezelfde fingerprint op; een later succes sluit die fingerprint opnieuw.

## API coverage reference

`docs/moneybird_api_coverage.md` is the generated catalogue of **all 296 operations** in the
official Moneybird OpenAPI spec, annotated with what this server covers (dedicated tool,
`moneybird_request` read, or not exposed). Check it before wrapping a new endpoint or before
answering "does the API support X?". Its header explains how to regenerate it from the spec
asset bundled on developer.moneybird.com.

## Where things live

- `moneybird/tools/` — MCP tool definitions, split by domain (`sales.py`, `bank.py`,
  `payments.py`, `contacts.py`, `ledger.py`, `purchases.py`, `reference.py`,
  `reports.py`, `core.py`, `sales_batches.py`, `workflows.py`, `approvals.py`).
  `_registry.py` holds the FastMCP
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
  moneybird_mcp_server.py` keeps the legacy SSE default for existing deployments. Every
  network transport requires `MCP_AUTH_TOKEN`; a non-loopback bind also requires a real trusted
  TLS proxy plus `MCP_TRUSTED_TLS_PROXY=true`.
  `mcpb/` + `scripts/build_mcpb.py` build the Claude Desktop extension bundle
  (`dist/*.mcpb`; platform-specific because dependencies are vendored into it).
- `moneybird/client.py` — HTTP client + endpoint methods. Every endpoint it calls is
  checked against `docs/moneybird_api_paths.json` by
  `tests/test_client_spec_conformance.py`, so a typo'd path fails the suite.
- `moneybird/http_transport.py` — shared keep-alive connection pool; request credentials stay
  tenant-scoped in `client.py`. `moneybird/task_context.py` provides invocation-scoped batch
  loading, and `moneybird/telemetry.py` + `performance_middleware.py` provide privacy-safe
  local performance counters.
- `moneybird/tool_discovery.py` — compact FastMCP BM25 Tool Search configuration and the
  always-visible core tool set.
- `moneybird/purchase_reconcile.py` — write-payload builders for reference-based and exact
  PDF-derived purchase reconciliation. It scales or validates line prices, maps them onto
  existing lines, and records pre/post-write expectations.
- `moneybird/write_contracts.py` — required versioned WriteSpec registry and shared
  controlled-field/financial-line comparison helpers. `tools/approvals.py` asserts that
  its action keys exactly match the registry, so a new executor cannot silently omit its
  precondition, verifier, occurrence identity, or reconciliation rule.
- `moneybird/purchase_review.py` — read-only supplier-history retrieval and advisory anomaly
  detection behind `review_purchase_invoices`. Description-similarity checks are optional;
  deterministic state and supplier-pattern checks remain available independently. Tests live in
  `tests/test_purchase_reconcile.py` and `tests/test_purchase_review.py`. PDF-reading design note:
  `docs/reading_pdf_attachments.md`.
- `moneybird/config.py` — constants, `MoneybirdError`, `.env` loading, and `data_dir()`
  (where approvals DB / audit logs / sync caches live; override with
  `MONEYBIRD_MCP_DATA_DIR`). Approvals are persisted in SQLite and survive restarts.
- `moneybird/search_fts.py` — SQLite FTS5 layer derived from the JSON sync index (the
  durable store stays JSON; the FTS file is a rebuildable cache keyed on
  `content_updated_at`, not a no-change freshness refresh).
  `search` tries FTS (AND then OR prefix match, bm25-ranked), then substring, then live.
- `moneybird/playbooks/boekhoud_playbook.md` — btw rules, categorization, consistency
  checklist, bank-mutation diagnosis. Read it before a bookkeeping task.
- `scripts/` — runnable read-only/reclassify scripts (good examples of the patterns above).
- `docs/releasing.md` — the release checklist. Releases are **version-driven**: a
  commit on `main` that bumps `version` in pyproject **and** mcpb/manifest.json is
  the release trigger. Never bump the version as a drive-by edit.
- `.github/workflows/` — `ci.yml` runs the suite on main + PRs (Ubuntu 3.11–3.14 plus
  Windows 3.11), a lowest-direct-dependency lane, reproducibility/SBOM checks, and
  distribution inspection. `security.yml` runs CodeQL and a full-history Gitleaks scan.
  `release.yml` is a main-only,
  stage-independent state machine: it checks PyPI, tag and release assets; gates the exact
  source SHA through the full test/dependency/artifact matrix; creates and re-verifies the tag
  before Trusted Publishing, re-verifies it after any environment-approval delay, and compares
  the tested candidate's filenames/hashes with the exact downloaded PyPI wheel/sdist.
  Trusted Publishing emits attestations; the workflow cryptographically verifies their
  repository identity, generates a reproducible CycloneDX SBOM from the exact published
  wheel, then creates or repairs the GitHub release and verifies the final tag, exact
  package/SBOM names, and digests. Historical recovery uses helpers from the guarded workflow
  commit; when that provenance cannot be reproduced/reviewed, recovery is manual rather than
  a rebuild. Partial PyPI publication fails closed. These workflow checks
  are defense in depth: the live repository still needs a `pypi` environment restricted to
  `main` with an independent required reviewer, plus a protected `v*` tag ruleset, before the
  release boundary is production-ready.
  Both workflows gate on `scripts/check_dist_hygiene.py`, which asserts the wheel
  ships only the `moneybird` package and that no `.env`/tokens/approvals DB/sync
  cache/audit log is packaged. CI never has credentials — keep the suite fully
  mocked (verified: the whole suite passes in a checkout without `.env`).
- `scripts/reconcile_execution.py` — local operator-only unresolved-execution inspection
  and evidence-bearing resolution. Never expose it as an MCP tool or automatically turn
  a lease/crash into a retry.
- `docs/hosted_gateway_design.md` — localhost gateway demo plus explicit production no-go.
  The server has a contained live-read-only `hosted_request_only` mode, but production identity,
  grant storage, authorization, durable artifact ownership and trusted write confirmation are
  not built.
- `gateway/` — the M1 localhost demo of that design (`python -m gateway`, loopback-only,
  source checkout only; not in the wheel, sdist, or `.mcpb`): OAuth onboarding pages +
  tenant-injecting dispatch to the
  in-process MCP app. Tests in `tests/test_gateway_demo.py`.
- `README.md` — setup, deployment, ChatGPT connection, tool descriptions.

## Tests

`python -m pytest -q` from the repo root. All tests should pass; when adding an MCP prompt,
also update `test_register_guidance_registers_prompts_and_resource` (it pins the exact
prompt-name set).

## GitHub publishing credentials

- Voor een opdracht die uitsluitend om **commit en push** vraagt, is Git zelf de bron van
  waarheid: controleer remote/branch en voer `git push` uit. Blokkeer zo'n opdracht niet op
  alleen een mislukte `gh auth status`.
- `gh auth status` valideert de credential via het netwerk. In een gesandboxte omgeving kan
  geblokkeerde netwerktoegang daardoor misleidend als een ongeldige token worden gerapporteerd.
  Behandel dat niet als bewijs van verlopen authenticatie: herhaal de controle met toegestane
  netwerktoegang en verifieer zo nodig read-only met `gh api user --jq .login`.
- Vereis een geldige `gh`-sessie alleen wanneer de gebruiker ook een pull request, issue,
  release of andere GitHub-API-actie vraagt. Test in dat geval `gh auth status` en laat zo
  nodig `gh auth login -h github.com` uitvoeren.
- **`origin` is sinds 2026-07-29 een SSH-remote, en dat is opzettelijk.** Pushen hangt
  daardoor niet meer van het OAuth-token af — relevant nu een push naar `main` een
  PyPI-release kan zijn. Zet het niet terug naar HTTPS zonder de volgende val te kennen:
  GitHub weigert elke HTTPS-push die `.github/workflows/**` aanraakt zolang het token de
  `workflow`-scope mist (`refusing to allow an OAuth App to create or update workflow ...
  without 'workflow' scope` — live geraakt op 2026-07-29). Een SSH-sleutel kent die
  scope-beperking niet; het alternatief is eenmalig
  `gh auth refresh -h github.com -s workflow` (interactief, dus door de gebruiker).
- Voor github.com is **gh zelf de git credential helper**
  (`credential.https://github.com.helper` in `~/.gitconfig`), niet Git Credential Manager.
  Een HTTPS-push gebruikt dus het gh-token, en `gh auth refresh` heeft er daadwerkelijk
  effect op. Diagnoseer de twee paden los van elkaar: `git ls-remote origin` (het pad dat
  git nu echt gebruikt) versus `ssh -T git@github.com` (geeft altijd exit 1, ook bij succes).
