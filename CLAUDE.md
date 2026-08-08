# CLAUDE.md — operating guide for this repo

Guidance for an AI agent working in this repository. The goal: be able to read from
and (carefully) write to the live Moneybird administration **without rediscovering the
setup each time**. Read this before doing live Moneybird work.

## What this is

A Moneybird MCP server (FastMCP). The MCP tools in `moneybird_mcp/tools/` are the surface
a chat model normally calls. When you work *in this repo* those tools are usually **not**
wired up as live MCP tools, so to actually touch the administration you run small Python
scripts that import the package directly (see below).

## Credentials — already handled, don't hand-roll it

- Local/single-user credentials are supplied explicitly as
  `MONEYBIRD_ACCESS_TOKEN` and `MONEYBIRD_ADMINISTRATION_ID`.
- Package imports never discover or load `.env`. For a runnable server, export values
  in the parent process or pass an absolute `--env-file PATH`. For a one-off script,
  call `moneybird_mcp.config.load_env_file(PATH)` before importing modules that consume
  configuration. Parent-process values always win.
- Real env vars always win (`setdefault`). Credential resolution then follows the explicit
  deployment mode:
  - `local` (stdio only): request context → env → local OAuth profile;
  - `network_single_user`: authenticated network edge → env → local OAuth profile; request
    Moneybird headers are rejected;
  - `hosted_request_only`: one nonblank trusted request context; no env/OAuth fallback.
- **OAuth**: split across four modules so a hosted callback flow can replace only the top
  layer. `oauth_scopes.py` (scope catalogue + rationale + profiles), `oauth_store.py`
  (`OAuthConnection` + the `TokenStore` interface + the local `FileTokenStore`),
  `oauth.py` (URL construction, both grants, refresh-on-read session), `auth_cli.py`
  (presentation only). App credentials come from `MONEYBIRD_OAUTH_CLIENT_ID` /
  `MONEYBIRD_OAUTH_CLIENT_SECRET` in the parent environment or an explicit `--env-file`.
  The user-facing command is `moneybird-mcp auth login | status | logout | scopes`;
  `moneybird_mcp/oauth_login.py` and `scripts/oauth_login.py` are aliases kept so existing
  documentation and shell history still work — error messages must never point at a path
  the wheel lacks. Tokens persist in `moneybird_oauth_tokens.json` in the data dir and are
  used automatically in local or network-single-user mode when `MONEYBIRD_ACCESS_TOKEN` is
  absent. Hosted request mode never reads that store. Full user-facing detail:
  `docs/oauth.md`.
  Six things here are load-bearing and easy to undo by accident:
  1. **A refresh answer's absent field means "unchanged", not "cleared".** Moneybird may
     return only a new access token; replacing the record wholesale drops the refresh
     token and the granted scopes, and the next expiry becomes a forced re-login
     (`OAuthConnection.merged_with_refresh`). A *failed* refresh must raise and leave the
     store untouched — a network blip is not a reason to discard a grant.
  2. **The token endpoint is the one endpoint whose responses contain credentials**, so
     its error bodies are never rendered wholesale. Only RFC 6749 `error` and
     `error_description` are extracted, mapped to specific guidance
     (`invalid_client` → the app credentials, `invalid_grant` → single-use/expired code).
     Neither grant is retried: an authorization code is single-use and a refresh may
     rotate the refresh token, so the usual retry convention does not apply.
  3. **The administration is never selected silently.** One reachable administration is
     taken, several are offered, and skipping saves no administration — a guessed one
     sends every later write to the wrong books. Skipping is not an error: the OAuth
     connection itself is already stored and usable, just without an administration,
     which a later login or `MONEYBIRD_ADMINISTRATION_ID` can supply. The choice is
     stored on the connection; `MONEYBIRD_ADMINISTRATION_ID` still overrides it, and
     `auth status` says which wins.
  6. **`auth login` persists the grant before it verifies it.** The exchange spends the
     authorization code, so a failed `/administrations` check must leave the tokens
     stored and say so — discarding them would cost the user another authorization for
     no safety gain. Do not "fix" the order.
  4. **Moneybird documents no revocation endpoint** (`oauth.REVOCATION_SUPPORTED`), so
     `auth logout` deletes local credentials only and must keep saying so.
  5. **Local OAuth is for development and self-hosters with their own registered
     application — never present it as the default public setup.** A Client Secret
     authenticates the *application*, so it cannot ship inside a source-available
     package; the personal API token stays the simple public path. Do not add a bundled
     application credential, and do not reword the docs to imply a PyPI user receives
     one. The hosted service is where OAuth becomes the normal path: backend holds the
     secret, user presses Connect Moneybird over an HTTPS callback, tokens stored
     server-side.
- **Scopes come from Moneybird's per-endpoint reference, not the Authentication page.**
  `docs/moneybird_api_scopes.json` is the checked-in snapshot (regenerate with
  `scripts/render_api_scopes.py`, which needs PyYAML and a downloaded `openapi.yml`).
  Parse the `Required scope(s)` *description text*, never the `security` array: the array
  is the same flat list whether scopes are needed together (`documents` **and**
  `sales_invoices` for profit_loss) or any one suffices (`Any of:` for contacts, ledger
  account reads, tax rate reads). Three groupings are counter-intuitive and were wrong
  here before 2026-08-08: **no report requires `settings`** (balance_sheet/cash_flow/
  general_ledger → `bank`; profit_loss/tax/journal_entries → `documents` + `sales_invoices`;
  debtors/revenue/subscriptions → `sales_invoices`; creditors/expenses/assets →
  `documents`), financial **accounts** are `settings` while financial **mutations** are
  `bank`, and `/administrations` needs no scope at all (documented, and **live
  geverifieerd 2026-08-08** met een echte OAuth-grant — daarom kan `auth login` een zojuist
  opgeslagen verbinding verifiëren en de bereikbare administraties aanbieden).
  `tests/test_oauth_scopes.py` joins every claim against the
  snapshot and proves the six requested scopes are minimal, so this cannot silently rot.
- **De volledige lokale OAuth-flow is op 2026-08-08 op Windows live tegen Moneybird
  doorlopen** met een echte geregistreerde applicatie: autorisatiepagina, OOB-code-uitwisseling,
  `/administrations`, administratiekeuze, `auth status` zonder secrets, echte leesacties via de
  opgeslagen verbinding zónder `MONEYBIRD_ACCESS_TOKEN`, en `auth logout`. Behandel die stappen
  dus als werkend; zoek een storing eerst in de omgeving (data dir, client id/secret) voordat je
  de flow zelf herschrijft.

## Running a one-off live query or fix

Put throwaway scripts in the scratchpad, not in the repo. Template:

```python
import sys
sys.path.insert(0, r"C:\path\to\moneybird-mcp-server")
from moneybird_mcp.config import load_env_file
load_env_file(r"c:\absolute\operator.env")  # explicit; parent env still wins
from moneybird_mcp.client import get_client

client = get_client()
# read-only first:
ledgers = {str(l["id"]): l["name"] for l in client.list_ledger_accounts()}
taxes   = {str(t["id"]): t["name"]  for t in client.list_tax_rates()}
```

`moneybird_mcp/client.py::MoneybirdClient` has typed methods for the common endpoints
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
6. **`ambiguous` costs a human a reconciliation, so spend it only on real unknowns.**
   `safety.classify_failed_write` decides: a status Moneybird answered with a refusal
   (`DEFINITIVE_REJECTION_HTTP_STATUS_CODES`, 422 being the usual write rejection) proves
   the refused request applied nothing, and closes as `failed`. Timeouts, 5xx and network
   failures stay `ambiguous`, and so does a refusal that arrives *after* an accepted
   mutation — the HTTP client counts those per execution (`safety.record_applied_write`,
   reset in `pop_approval`), so the proof is evidence and not an assumption. 409 is
   deliberately not definitive: it can mean the record already exists. Never widen this by
   guessing from the message text; add the status to the set with a reason.
   **What counts as a mutation is `retry_safe`, not the HTTP method.** Moneybird's batch
   readers (`fetch_*_by_ids`) are POSTs to `.../synchronization.json`, and a method-only
   test would score a bulk *read* as an applied write — which silently drags the next
   rejection back to `ambiguous`. A request marked `retry_safe` is by construction a read,
   because nothing that could change data may be retried automatically; keep that
   invariant when adding endpoints.
   The user-facing error for an ambiguous execution must immediately say that the write
   may already have been applied and must be verified before retrying. The audit state
   alone is not enough: a bare connection error otherwise looks like a safe pre-write
   failure at the exact moment the operator must decide what to do.

## Domain gotchas learned the hard way

- **Moneybird explains its refusals; pass that on.** A rejected write answers with the
  field and the reason (`{"error": {"send_invoices_to_email": ["includes a domain which
  cannot receive emails"]}}`), which is the only actionable part — `HTTP 422` alone is not
  something a user or an agent can correct. `client._request` raises `MoneybirdHTTPError`
  carrying `status_code` and the parsed `reported` body, rendered into the message by
  `formatting.format_reported_error` and capped at `MAX_ERROR_DETAIL_CHARS`. Keep that cap
  when touching it: the same text lands in the durable audit log.
- **`search_tools` ranks on description text, so descriptions must use the words users
  type.** BM25 (`fastmcp` `BM25SearchTransform`) indexes name + description + parameter
  descriptions, with no tags or boost knobs, and weights *rare* words heavily. That made
  "create a new contact" return `prepare_create_credit_invoice` first — "new" is rare in
  the catalogue, "contact" appears in a dozen tool names — while `prepare_create_contact`
  said only "Use this before creating a Moneybird contact". Lead a tool description with
  the plain phrasings ("add a customer, client, supplier, or vendor"), and note that BM25
  normalises for length, so a terse description can still outrank a longer, better one. Pinned by `tests/test_tool_discovery.py::ToolSearchRankingTests`, which
  ranks in a **subprocess** with `MONEYBIRD_TOOL_DISCOVERY=search` and an explicit
  `cwd`/`PYTHONPATH` on the repo root — the discovery mode is fixed per process at import,
  and a `pip install`ed copy of this package in site-packages otherwise shadows the working
  tree from any other cwd and silently ranks the released descriptions instead.
  Enriching a *hidden* tool costs zero protocol bytes; only the seven always-visible
  tools in `tool_discovery.ALWAYS_VISIBLE_TOOLS` affect the compact catalogue size.

- **Meterverbruikfacturen hebben een first-class flow.** Gebruik
  `prepare_meter_usage_sales_invoices` met begin/eindstanden of `usage_kwh`; de tool rekent
  verbruik na, kan lage/uitgesloten meters overslaan, hergebruikt de laatste passende
  meterregel voor tarief/btw/grootboek en kan alles in één batch inplannen. Na akkoord:
  `execute_approved_action`; de uitvoering verifieert status, datum en
  totaal automatisch. Voor bestaande concepten gebruik je
  `prepare_batch_schedule_sales_invoices` → `execute_approved_action`.

- **Suppliers invoice *us*.** A utility or telecom vendor has **0 sales
  invoices**; its documents are **purchase invoices** (or receipts). Look under
  `list_documents("purchase_invoice", ...)` / `("receipt", ...)`, then filter by
  `contact.id`. Sales-invoice filters take `contact_id`; the document endpoints don't, so
  fetch a period and filter client-side on the contact. When the user gives an exact supplier
  invoice number, use `get_purchase_invoice_by_reference` (or
  `MoneybirdClient.get_document_by_reference`) instead of broad `search`; it uses Moneybird's
  server-side `reference:` filter and then requires an exact match. Since 2026-08-02 a
  contact-filtered `list_sales_invoices` that comes back empty says this in a `note` on the
  result, because a bare `count: 0` otherwise reads as "this contact has no invoices".
- **Moneybird's boekingsregels vullen ook inkoopfacturen automatisch in — inconsistent.**
  Dezelfde regels die bankmutaties boeken, vullen inkomende inkoopfacturen in, maar niet
  betrouwbaar: dezelfde leverancier komt de ene maand met de vaste meerregelige splitsing binnen
  en de volgende als één verzamelregel, nog in status `new`, soms met `prices_are_incl_tax`
  omgedraaid. De regel zelf zie/repareer je niet (niet in de API), alleen het resultaat. Gebruik
  `review_purchase_invoices` om afwijkers te vinden (nog `new`, minder regels dan gebruikelijk,
  ontbrekende grootboeken, een afwijkende btw-vlag, of een bekende omschrijving die ineens op een
  ander grootboek/btw-tarief staat) en `prepare_reconcile_purchase_invoice`
  → `execute_approved_action` om de vaste boeking van een goede referentiefactuur
  te reproduceren. Regelprijzen worden naar het doeltotaal geschaald zodat het documenttotaal tot
  op de cent gelijk blijft; wijken de totalen af, dan is de regel-voor-regel-splitsing een (in de
  preview gemarkeerde) aanname. De echte splitsing lees je van de factuur-PDF met
  `read_document_attachment` en geef je als exacte `desired_lines` mee; die modus valideert ids en
  weigert een verdeling die het totaal wijzigt. Contactgerichte reviews gebruiken de volledige
  versioned sync-feed (met paginering als fallback), zodat oudere boekjaren niet door Moneybirds
  impliciete huidige-boekjaarfilter verdwijnen. Een patroon heet pas "gebruikelijk" na minstens
  twee eerdere facturen van die leverancier; reviewredenen tonen grootboeknummer en -naam in
  plaats van alleen het interne id. Elke reconcile
  slaat de documentversie op en breekt vóór de PATCH af als de factuur sinds de preview is gewijzigd.
  (Ontwerp: `docs/reading_pdf_attachments.md`; logica: `moneybird_mcp/purchase_reconcile.py`.)
- **Een btw-betaling boeken is de helft; de aangifteperiode schoonboeken is de andere helft.**
  Moneybird verplaatst bij het indienen niets: *Te betalen btw* en *Te vorderen btw* lopen door
  tot een memoriaal ze afwikkelt. Gebruik `analyze_vat_settlement` →
  `prepare_vat_settlement_journal`. Twee dingen die stelselmatig misgaan als je ze niet weet:
  (1) **verlegde btw** staat als verschuldigd én als aftrekbaar geboekt, dus de bruto
  grootboekmutaties zijn aan beide kanten hoger dan het btw-rapport terwijl het netto bedrag
  gelijk blijft — schoonboeken gaat op **bruto**, en een gelijk verschil aan beide kanten is geen
  afwijking maar iets om uit te leggen; (2) de aangifte wordt in **hele euro's** ingevuld en mag
  **in je voordeel** worden afgerond (verschuldigd omlaag, voorbelasting omhoog), dus het betaalde
  bedrag ligt legitiem onder het exacte saldo. Leid dat bedrag nooit af en hanteer geen
  vaste tolerantiegrens — vraag de aangifte; de tool toetst het bedrag op hele euro's en tegen
  een uit het aantal rubrieken *afgeleide* grens. Een niet-nulrestant gaat naar
  `Afrondingsverschillen`; de read-only analyse zoekt die rekening niet op en de prepare-flow
  vereist hem alleen wanneer werkelijk een afrondingsregel nodig is. Let op:
  het memoriaal balanceert per definitie, dus een verkeerd bedrag verdwijnt zonder die controles
  geruisloos in de afrondingsregel en komt daarna als geverifieerd terug. Logica:
  `moneybird_mcp/vat_settlement.py`; playbook §3 + §3b; regressietests (met verlegde btw, refunds en
  afrondingsvarianten) in `tests/test_vat_settlement.py`. Houd de bedragen daar synthetisch —
  deze repo is publiek. Een afgewikkelde periode wordt niet aan vrije referentietekst
  herkend, maar aan een memoriaal **binnen de exacte periode** dat beide btw-rekeningen
  raakt, of één btw-rekening plus de rekening voor de Belastingdienst. De analyse telt
  die afwikkelregels terug om de bruto positie vóór afwikkeling te reconstrueren;
  prepare én execute gebruiken dezelfde periodegebaseerde detectie, zodat een andere
  referentie nooit een dubbele afwikkeling kan openen.
- **Nieuwe grootboekrekeningen vereisen een RGS 3.5-code.**
  `prepare_create_ledger_account` heeft daarom geen lege/default `rgs_code`; de code is verplicht
  en wordt na creatie via `taxonomy_item.code` geverifieerd. `list_ledger_accounts` toont de
  bestaande `rgs_code`, naam en taxonomieversie als voorbeelden uit de actieve administratie.
- **Btw-aangiftes zelf zitten niet in de API** (net als de boekingsregels): `tax_returns`,
  `vat_returns`, `vat_documents`, `vat_declarations` → 404. `VatDocument` is wel een geldig
  `booking_type`, maar de id is niet op te halen; bouw daar geen flow op. `period_locked_until`
  op de administratie is wél leesbaar en zegt of een verstreken periode nog boekbaar is.
  `moneybird_request` weigert al die routes sinds 2026-08-02 met een uitleg die naar
  `analyze_vat_settlement` verwijst, zodat niemand nog de spellingen langsloopt.
- **Document line prices follow each document's `prices_are_incl_tax` flag.** With the
  flag true, the invoice total incl is the sum of line `price` values; with it false,
  Moneybird adds each line's tax rate to those raw prices. Reference-based purchase
  reconciliation therefore scales the reference line's own raw price and independently
  calculates the incl.-tax result before staging an approval. Never put an incl.-tax target
  total directly into an excl.-tax line price. `total_price_excl_tax_with_discount` is the
  preferred true excl.-tax amount for line previews.
- **The `amount` field is messy in older data**: values like `"1 x"` or `""` occur and
  Moneybird treats them as `1`. Normalize to `"1"` when asked to make lines consistent;
  it doesn't change any total. Reading is handled for you: `formatting.document_line_quantity`
  reads a blank or `"1 x"` as 1 and a `"3 stuks"` as 3, and *refuses* an ambiguous value like
  `"1,5"` instead of guessing — the quantity scales a line total, so a silent wrong reading
  becomes a wrong amount. (Before 2026-08-02 this raised a raw `decimal.InvalidOperation`
  from the reclassify preview.)
- **Bankmutaties matchen doe je niet met de hand.** `suggest_bank_mutation_matches`
  (`moneybird_mcp/bank_matching.py`) reproduceert wat Moneybirds eigen transactiescherm
  voorstelt, maar deterministisch: referentie in de bankomschrijving (op alfanumeriek
  vergeleken, minimaal 4 tekens zodat een kort factuurnummer niet toevallig matcht), exact
  openstaand bedrag, IBAN van de tegenpartij, contactnaam. Richting bepaalt de zoekruimte:
  inkomend kan alleen een verkoopfactuur voldoen, uitgaand alleen een inkoopfactuur of bon.
  Confidence is een *tier* (`exact`/`strong`/`possible`), geen score — een getal suggereert
  precisie die er niet is. Een even goede runner-up wordt als `ambiguous` gemeld en nooit
  weggetiebreakt; dat is precies het geval (vaste maandfactuur van hetzelfde bedrag, betaald
  zonder referentie) waarin een automatische keuze stilzwijgend fout boekt. De tool schrijft
  niets: koppelen loopt onverminderd via `prepare_link_bank_mutation_booking`. Tegen echte
  reeds-gekoppelde mutaties gevalideerd: top-1 correct in elk beslisbaar geval, de enige
  echte gelijkstand als `ambiguous` gemeld.
- **`sepa_fields.remi` zegt wát een afschrijving is; `contra_account_name` niet.** De
  tegenrekeningnaam is alleen de partij ("Interpolis"), terwijl de SEPA-omschrijving het
  contract én de gedekte periode draagt (`"ZIB polis 350259527 Periode 01.02.2026 -
  01.05.2026"`). Eén verzekeraar incasseert routineus meerdere polissen — zakelijk per
  kwartaal, privé per maand — die op naam en soms zelfs op bedrag niet te onderscheiden zijn.
  `formatting.bank_description` leest `remi` met `message` als terugval;
  `financial_mutation_search_record` indexeert de volledige tekst plus `eref` en zet een op
  `MAX_BANK_DESCRIPTION_CHARS` afgekapte kopie als `description` op de hit. `sref` blijft er
  bewust buiten: een ondoorzichtige scheme-UUID voegt alleen ruis toe. Zoeken op een
  polisnummer selecteert daardoor precies één reeks, zonder `fetch` per mutatie.
- **Gearchiveerde contacten vallen standaard buiten de synchronisatiefeed.** Dat hield elke
  gearchiveerde leverancier uit de zoekindex: zijn facturen waren vindbaar, het contact zelf
  niet, dus een leveranciersnaam liet zich nooit tot een `contact_id` herleiden. De
  ongedocumenteerde `include_archived=true` lost dat op en werkt óók op
  `/contacts/synchronization.json` (live gemeten: 1 id zonder, 7 mét). `filter=archived:true`
  werkt *niet* — Moneybird negeert het stilzwijgend en geeft gewoon de actieve contacten
  terug. De gearchiveerde records dragen dezelfde `version`, dus incrementele sync blijft
  intact. `contact_search_record` markeert ze met `[gearchiveerd]` in de titel en
  `state: "archived"`, want zo'n contact bezit nog wel zijn historie maar kan niet zonder
  dearchiveren opnieuw gefactureerd worden.
- **De playbook is óók een tool, en dat is de enige route die overal werkt.** Een MCP
  *resource* wordt door de client gelezen, niet door het model: Claude Desktop vereist dat de
  gebruiker hem handmatig aanhecht en ChatGPT-connectors lezen geen willekeurige resources.
  `get_bookkeeping_guide(topic)` (in `tools/catalogue.py`, secties uit
  `guidance.PLAYBOOK_TOPICS`) maakt dezelfde inhoud per onderwerp modelaanroepbaar. Let op de
  parser: een *ongenummerde* `###`-kop blijft bij zijn `##`-sectie. Dat is niet cosmetisch —
  `### Afronding` onder `## 3. BTW` draagt de hele-euro-afrondingsregel, en losknippen zou die
  stil uit het `btw`-onderwerp laten verdwijnen. `tests/test_bookkeeping_guide.py` pint dat.
- **Boekingsregels (bank/transaction rules) are not in the API** — see
  `moneybird_mcp/playbooks/boekhoud_playbook.md` and the memory note. Don't try to read them;
  `moneybird_request` answers every spelling (`transaction_rules`, `boekingsregels`, ...)
  with a message saying so and pointing at `created_at` vs `processed_at` instead.
- **Sommige rapporten zijn maand-gebonden**: `cash_flow`, `tax`, `debtors` en `creditors`
  accepteren maximaal één maand (`this_month`, `202606`); de `*_aging`-rapporten willen een
  hele maand als peildatum. Alleen `profit_loss`, `balance_sheet`, `general_ledger` en de
  `*_by_contact`/`*_by_project`-rapporten slikken `this_year`. (Live geverifieerd:
  `{"error":"Period cannot exceed 1 month"}`.) Die grens is hard: `this_quarter`,
  `prev_quarter`, `this_year`, een dagbereik over twee maanden en de maandbereik-syntax
  `202604..202606` falen allemaal, en er is geen parameter die het opheft (`grouping=quarter`
  wordt genegeerd). Wel is de grens *maximaal* een maand, niet *precies* een kalendermaand —
  `20260401..20260430` werkt. Een kwartaal haal je per maand op en tel je zelf op
  (`vat_settlement.month_periods`). Let op het verschil met de lijst-endpoints, die juist géén
  `period:YYYYMM` accepteren maar wél een datumbereik.
  `client.get_report` bewaakt deze grens sinds 2026-08-02 zelf: een te lange periode wordt
  geweigerd mét de exacte maanden om los op te halen, in plaats van Moneybirds kale
  `Period cannot exceed 1 month`. De maanden worden bewust *niet* automatisch opgeteld —
  de rapportvormen verschillen en een stil verkeerd totaal is erger dan een extra call.
- **Een memoriaal heeft geen header-`description`.** Moneybird laat het veld weg uit het
  teruggegeven `general_journal_document`-record (live geverifieerd 2026-08-01), dus meesturen
  liet `verify_general_journal_payload` op elke zo aangemaakte boeking falen.
  `prepare_create_general_journal_document` zet een meegegeven `description` daarom op elke regel
  die er zelf geen heeft; de btw-afwikkeling zet hem sowieso per regel.
- **Betalingen en bankkoppeling hebben eigen guarded tools**: `prepare_register_payment`
  (verkoopfactuur/inkoopfactuur/bon), `prepare_link_bank_mutation_booking` /
  `prepare_unlink_bank_mutation_booking` (bankmutatie ↔ factuur/document/grootboekcategorie)
  en `prepare_create_credit_invoice`. Voor een reeks bestaande directe bankboekingen die naar
  een ander grootboek moeten, gebruik je `prepare_reclassify_bank_mutation_bookings` →
  `execute_approved_action`: volledige preflight op mutatieversie en
  bronboeking, exact bedrag, nacontrole van state/open bedrag, plus herstelpoging bij mislukte
  relink. Moneybird biedt geen transactie over meerdere mutaties, dus partial failures worden
  expliciet als zodanig geaudit. Gebruik deze flows, geen handmatige constructies.
  Let op de afwijkende API-conventie (live bevestigd op 2026-07-29): de
  `link_booking`-request verwacht `price_base` als **positieve grootte**, terwijl Moneybird op de
  teruggegeven betaling/grootboekboeking een ondertekend `price` gebruikt. De client vertaalt
  daarom het ondertekende toolbedrag aan de HTTP-grens. De uitvoerder controleert daarna ook het
  teruggegeven teken, `amount_open` en de verwerkte status; alleen "er verscheen een koppeling" is
  niet voldoende bewijs van succes. Als `price` in de prepare-call leeg is, wordt de actuele
  ondertekende `amount_open` al in de approval en payload ingevuld; er gaat nooit een lege prijs
  naar Moneybird. Bewijst de nacontrole dat vóór en na identiek zijn, dan is de zichtbare status
  `failed`, niet `completed_with_errors`.
  Een directe koppeling aan een omzet- of kostenrekening maakt géén btw-boeking en accepteert
  geen `tax_rate_id`: het volledige bedrag landt op het grootboek. De prepare-preview moet dit
  bij `revenue`, `expenses`, `direct_costs` en `other_income_expenses` expliciet waarschuwen;
  bedragen inclusief btw horen via een factuur/document of een expliciet gebalanceerd
  memoriaal met aparte btw-regels te lopen.
- **Provider-owned duplicatie is geen regeltemplate.**
  `prepare_create_credit_invoice` bindt de volledige originele factuur als precondition, maar
  na Moneybirds `duplicate_creditinvoice` worden alleen het exact genegeerde incl.-btw-totaal
  en de conceptstatus geverifieerd. Moneybird mag een `Creditfactuur voor factuur ...`-kopregel
  toevoegen en aantallen in plaats van prijzen negeren; voorspelde regels leveren daarom geen
  betrouwbaar verificatiesignaal. Batch-updates weigeren onbekende/inapplicable velden en een
  lege patch zowel bij prepare als defensief bij execute.
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

## Performance architecture (verified 2026-08-07)

- **Moneybird throttelt per IP-adres: 150 requests per 5 minuten, en slechts 50 per 5
  minuten voor `/reports/`.** Dat is het echte plafond, niet de latency van een losse call.
  `moneybird_mcp/rate_budget.py` observeert het budget per bucket; dure scans vragen
  `affordable_batches()` en stoppen eerlijk in plaats van het budget van de rest van de taak
  op te maken. Een 429 waarvan het venster de retry-cap overschrijdt faalt meteen mét bucket
  en wachttijd. **Moneybirds headers volgen de IETF RateLimit-draft níét** (live gemeten
  2026-08-07): `RateLimit-Remaining` is *seconden tot reset* (gelijk aan `Reset` min nu, en
  groter dan `Limit`), `RateLimit-Reset` is een absolute Unix-epoch, en het echte
  requestaantal staat in het ongedocumenteerde `RateLimit-RequestsRemaining`. Een `remaining`
  boven `limit` wordt daarom weggegooid. Consequentie voor hosting: alle tenants op één IP
  delen 150/5min — vraag Moneybird om per-administratie-limieten vóór je hosting bouwt.
- **Toestandsfilters verschillen per documenttype en falen stil.** `state:open` op een
  inkoopfactuur wordt geaccepteerd en geeft nul rijen: een onbetaalde inkoopfactuur is `late`
  of `new`, nooit `open`. Alleen een onbekende statusnaam geeft HTTP 400. Moneybird accepteert
  pipe-gescheiden alternatieven, dus de hele onbetaalde set kost één request; gebruik
  `config.UNPAID_SALES_INVOICE_STATES` en `config.UNPAID_DOCUMENT_STATES`.
- **`moneybird_mcp/reference_cache.py`** cachet grootboekrekeningen, btw-tarieven en de
  administratie-membershipcheck kortstondig in-process, gesleuteld op een gezouten digest van
  het token plus het administratie-id, en staat uit in `hosted_request_only`. Live gemeten:
  herhaalde `list_ledger_accounts` van ~390–630 ms (43 KB) naar ~0 ms, herhaalde `search` van
  ~107 ms naar ~5 ms — die membership-round-trip domineerde, want het lokale indexwerk kost
  ~6 ms. Faalt een loader, dan wordt er niets gecachet. TTL's via
  `MONEYBIRD_REFERENCE_CACHE_SECONDS` / `MONEYBIRD_MEMBERSHIP_CACHE_SECONDS` (`0` = uit).
- De runnable server gebruikt standaard **`full`** tool discovery. Compacte discovery
  (`search`) verkleint de catalogus van 85 tools / 84.399 bytes naar 9 tools / 7.218 bytes,
  maar die catalogus staat in de *gecachete* promptprefix van de client, dus die besparing is
  vrijwel gratis — terwijl elke taak een extra `search_tools`/`call_tool`-ronde kost en elk
  `search_tools`-antwoord zelf 6–12 KB ongecachete output is. BM25 rangschikt bovendien op
  Engelstalige beschrijvingen: `meterstanden factureren` gaf nul tools en `betaling boeken op
  factuur` liet `prepare_register_payment` volledig weg. Zet compacte modus alleen aan met
  `--tool-discovery search` / `MCP_TOOL_DISCOVERY=search` voor clients die de volledige lijst
  niet aankunnen. Vergroot de catalogus niet zonder reden: de juiste oplossing voor te veel
  tools is minder tools, niet een zoeklaag ervoor.
  De legacy entrypoint importeert tools pas na CLI/expliciete env-file-verwerking, zodat ook
  `python moneybird_mcp_server.py --tool-discovery search` werkelijk omschakelt.
  `call_tool` is in die modus een expliciet read-only proxy: alleen tools met
  `readOnlyHint=true` (ook `prepare_*`) mogen erdoor. Schrijfexecutors staan niet in de
  zoekresultaten, worden door de proxy geweigerd en kunnen evenmin rechtstreeks op naam worden
  aangeroepen via FastMCP's onderliggende catalogus. Elke uitvoering loopt via de altijd
  zichtbare, destructief geannoteerde `execute_approved_action`, zodat de MCP-client zijn
  bevestigingsbeleid daadwerkelijk kan toepassen. Factuurpreviews lossen btw/grootboek eerst uit
  een expliciete regel, daarna uit een gekozen product en pas daarna uit de laatste contactfactuur;
  zonder zo'n verifieerbare btw-bron moet de caller `tax_rate_id` expliciet aanleveren.
- `moneybird_mcp/http_transport.py` beheert één luie `httpx`-connection pool. Authenticatie blijft
  per request/tenant en staat nooit op de gedeelde client. In een live meting kostte de eerste
  identieke GET circa 0,29 s en hergebruikte GETs circa 0,05–0,08 s.
- `MoneybirdTaskContext` cachet referentiedata binnen één tool-invocation en haalt bekende
  document-, factuur- en mutatie-id's in groepen van maximaal 100 via de sync-endpoints. Cache
  nooit tussen taken/tenants. De bankbatch gebruikt hierdoor voor `N <= 100` ongeveer `5 + 2N`
  API-calls voor prepare+execute in plaats van `2 + 6N`, inclusief een aparte eindverificatie.
- Duurzame JSON/FTS-sync is alleen beschikbaar in local/network-single-user mode. Hosted request
  mode gebruikt uitsluitend de gedeeltelijke live zoekfallback en weigert `sync_search_index`.
  `read_document_attachment` blijft daar uitgeschakeld ondanks het disposable begrensde
  parserproces: hosted capacity, backpressure, abusebeleid en lifecycle-controls ontbreken.
- De zes sync-feeds lopen begrensd parallel (maximaal drie workers), met een lock per
  administratie en atomaire JSON-save. `updated_at` is de freshness-tijd; alleen
  `content_updated_at` triggert een FTS-rebuild. Een live no-change sync daalde van 4,21 s naar
  0,69–0,86 s (laatste herhaling: 0,71 s).
- `ToolTelemetryMiddleware` en de HTTP-client verzamelen begrensde, in-memory
  latency/call-count/retry-statistieken. `get_server_status` leest die lokaal. Telemetrie bevat
  geen tokens, queryparameters, bodies of responses; numerieke record-id's worden uit
  endpointnamen verwijderd. Metrics worden gegroepeerd op een afgekorte, token-afgeleide
  pseudonieme scope. Dat label filtert procesmetrics maar is geen tenantidentiteit of
  autorisatiegrens. `get_server_status` en elk prepare-resultaat tonen ook de mechanische
  capability mode; een geweigerde read-only uitvoering blijft pending maar schrijft een
  `policy_blocked` audit-event.
- Duplicate-suppression blijft geldig na een latere `failed`/`partial_failure`-auditregel.
  Alleen een expliciete, nieuwere `invalidated`-regel heft een bewezen succes voor exact
  dezelfde fingerprint op; een later succes sluit die fingerprint opnieuw.
- **Productprijswijzigingen zijn product-only en gebruiken een afgeleide semantische fingerprint.**
  Gebruik `audit_products` en `analyse_product_price_adjustment` vóór
  `prepare_bulk_update_product_prices`. De prepare-tool leidt de fingerprint af uit
  ingangsdag, selectie, strategie en afronding. Die voorkomt een tweede uitvoering na
  succes en blokkeert het claimen van een nieuwe approval zolang een gedeeltelijke of
  ambigue poging onopgelost is. De flow rekent uitsluitend met `Decimal`, controleert
  alle opgeslagen `updated_at`-versies vóór de eerste PATCH en
  leest elk product na de write opnieuw. Hij wijzigt geen bestaande facturen, periodieke
  facturen, abonnementssjablonen of subscriptions en doet daar ook geen impliciete claim over.
  Product PATCH is altijd direct: een datum vóór of na vandaag is analysis-only en kan niet
  worden gebackdatet of ingepland.

## API coverage reference

`docs/moneybird_api_coverage.md` is the generated catalogue of **all 296 operations** in the
official Moneybird OpenAPI spec, annotated with what this server covers (dedicated tool,
`moneybird_request` read, or not exposed). Check it before wrapping a new endpoint or before
answering "does the API support X?". Its header explains how to regenerate it from the spec
asset bundled on developer.moneybird.com.

## Where things live

- `moneybird_mcp/tools/` — MCP tool definitions, split by domain (`sales.py`, `bank.py`,
  `payments.py`, `contacts.py`, `ledger.py`, `purchases.py`, `reference.py`,
  `reports.py`, `core.py`, `sales_batches.py`, `workflows.py`, `catalogue.py`,
  `products.py`, `approvals.py`).
  `_registry.py` holds the FastMCP
  instance + server instructions; `_context.py` is the patchable indirection tests use
  (`mock.patch.object(moneybird_mcp.tools._context, "get_client", ...)`); `_writes.py` is
  the shared write machinery — new guarded writes use `stage_write` +
  `run_approved_write`, don't hand-roll the approval/audit plumbing. `_params.py` holds
  the shared `Annotated[..., Field(...)]` parameter types (Limit, Period, ApprovalId,
  ReportName, ...) that give MCP clients per-parameter descriptions and enums — use them
  in new tool signatures; its Literal enums are kept in sync with `config.py` by
  `tests/test_tool_params.py`.
- `moneybird_mcp/server.py` — the runnable entrypoint (`build_config` + `main`). The
  `moneybird-mcp` console script (see `pyproject.toml`) defaults to **stdio** for local
  MCP clients and defaults server state to `~/.moneybird-mcp`; `python
  moneybird_mcp_server.py` keeps the legacy SSE default for existing deployments. Every
  network transport requires `MCP_AUTH_TOKEN`; a non-loopback bind also requires a real trusted
  TLS proxy plus `MCP_TRUSTED_TLS_PROXY=true`.
  `mcpb/` + `scripts/build_mcpb.py` build the Claude Desktop extension bundle
  (`dist/*.mcpb`; platform-specific because dependencies are vendored into it).
- `moneybird_mcp/client.py` — HTTP client + endpoint methods. Every endpoint it calls is
  checked against `docs/moneybird_api_paths.json` by
  `tests/test_client_spec_conformance.py`, so a typo'd path fails the suite.
- `moneybird_mcp/http_transport.py` — shared keep-alive connection pool; request credentials stay
  tenant-scoped in `client.py`. `moneybird_mcp/task_context.py` provides invocation-scoped batch
  loading, and `moneybird_mcp/telemetry.py` + `performance_middleware.py` provide privacy-safe
  local performance counters.
- `moneybird_mcp/tool_discovery.py` — compact FastMCP BM25 Tool Search configuration and the
  always-visible core tool set.
- `moneybird_mcp/workflow_catalogue.py` — kleine typed, versioned registry met alleen
  volledig geïntegreerde productworkflows. `list_supported_workflows` exposeert hem;
  `scripts/render_workflow_catalogue.py --check` keeps `docs/workflow-catalogue.md`
  generated from that source. Product audit/calculation logic lives in
  `moneybird_mcp/product_workflows.py`; its MCP boundary and guarded executor live in
  `tools/products.py`.
- `moneybird_mcp/vat_settlement.py` — btw-afwikkeling: gross-vs-reported comparison (reverse-charge
  aware, each side judged separately), settlement-account resolution by name with id overrides,
  period-end derivation, declared-amount validation, and the balanced memoriaal builder. The
  tools live in `tools/ledger.py` under their own `settle_vat_period` write contract: the
  executor re-reads gross movements, the administration lock and existing settlements
  immediately before dispatch, aborts on any drift from the approved snapshot, and afterwards
  proves the period's VAT accounts actually cleared to zero. Its duplicate fingerprint is the
  settled period, not the journal wording, so a second attempt under a different reference is
  still suppressed.
- `moneybird_mcp/purchase_reconcile.py` — write-payload builders for reference-based and exact
  PDF-derived purchase reconciliation. It scales or validates line prices, maps them onto
  existing lines, and records pre/post-write expectations.
- `moneybird_mcp/write_contracts.py` — required versioned WriteSpec registry and shared
  controlled-field/financial-line comparison helpers. `tools/approvals.py` asserts that
  its action keys exactly match the registry, so a new executor cannot silently omit its
  precondition, verifier, occurrence identity, or reconciliation rule.
- `moneybird_mcp/purchase_review.py` — read-only supplier-history retrieval and advisory anomaly
  detection behind `review_purchase_invoices`. Description-similarity checks are optional;
  deterministic state and supplier-pattern checks remain available independently. Tests live in
  `tests/test_purchase_reconcile.py` en `tests/test_purchase_review.py`. PDF-reading design note:
  `docs/reading_pdf_attachments.md`.
  Leveranciershistorie komt bij voorkeur uit de lokale sync-index, die per document een
  `contact_id` bewaart: dat noemt exact de op te halen id's. Moneybird heeft geen
  contactfilter op inkoopdocumenten, dus het alternatief is elk document in de administratie
  ophalen en client-side filteren. Die scan bestaat nog als fallback (nieuwste eerst, met
  budgetstop), maar zijn oude vroege-exit vergeleken tegen de *match*-limiet en ging in de
  praktijk dus nooit af.
- `moneybird_mcp/bank_matching.py` — deterministische kandidaatmatching achter
  `suggest_bank_mutation_matches`. Puur en zonder MCP- of clientkennis, dus volledig testbaar;
  tests in `tests/test_bank_matching.py`.
- `moneybird_mcp/reference_cache.py` en `moneybird_mcp/rate_budget.py` — kortstondige
  referentiecache en geobserveerd rate-limitbudget. Tests in `tests/test_reference_cache.py`.
- `moneybird_mcp/oauth_scopes.py`, `oauth_store.py`, `oauth.py`, `auth_cli.py` — the OAuth
  stack, layered so only `auth_cli.py` changes for a hosted HTTPS-callback flow. See the
  OAuth section above for the four rules that must not be undone. Tests in
  `tests/test_oauth.py` (protocol, store, scopes, redaction, credential precedence) and
  `tests/test_oauth_cli.py` (the three commands, administration selection, console-script
  dispatch). `moneybird_mcp/auth.py` is unrelated: it is the MCP transport's shared-secret
  middleware, not Moneybird authentication.
- `moneybird_mcp/config.py` — constants, `MoneybirdError`, explicit env-file parsing, and `data_dir()`
  (where approvals DB / audit logs / sync caches live; override with
  `MONEYBIRD_MCP_DATA_DIR`). Approvals are persisted in SQLite and survive restarts.
- `moneybird_mcp/search_fts.py` — SQLite FTS5 layer derived from the JSON sync index (the
  durable store stays JSON; the FTS file is a rebuildable cache keyed on
  `content_updated_at`, not a no-change freshness refresh).
  `search` tries FTS (AND then OR prefix match, bm25-ranked), then substring, then live.
  Zoekresultaten dragen `date`, `amount`, `state`, `contact_id` en voor bankmutaties
  `description` mee, zodat kiezen tussen hits meestal geen `fetch` per kandidaat meer kost
  (één verkoopfactuur is ~9,7 KB ruw record). Verander je de vorm van een searchrecord,
  bump dan **beide** schemaversies:
  `sync.RECORD_SCHEMA_VERSION` (incrementele sync herbouwt records alleen bij een gewijzigde
  Moneybird-`version`, dus zonder bump bereikt een veldwijziging ongewijzigde records nooit)
  en `search_fts.FTS_SCHEMA_VERSION` (kolomwijziging; de FTS-file wordt gedropt en opnieuw
  gevuld).
- `moneybird_mcp/playbooks/boekhoud_playbook.md` — btw rules, categorization, consistency
  checklist, bank-mutation diagnosis. Read it before a bookkeeping task. Bereikbaar als
  resource én per onderwerp via `get_bookkeeping_guide`; onderwerpindeling in
  `guidance.PLAYBOOK_TOPICS`.
- `scripts/` — runnable read-only/reclassify scripts (good examples of the patterns above).
- `docs/releasing.md` — the release checklist. A push or merge never publishes.
  Releases require an explicit default-branch `workflow_dispatch` with the exact
  package version and full commit SHA. Never bump or dispatch a version as a
  drive-by edit.
- `.github/workflows/` — `ci.yml` runs the suite on main + PRs (Ubuntu 3.11–3.14 plus
  Windows 3.11), a lowest-direct-dependency lane, reproducibility/SBOM checks, and
  distribution inspection. `security.yml` always runs pinned Bandit and a full-history
  Gitleaks scan; CodeQL additionally runs for public repositories or when the repository
  variable `ENABLE_CODEQL` is explicitly set to `true` after Code Security is enabled.
  `release.yml` is a manual, default-branch-only state machine: it requires exact
  version and full-SHA inputs, refuses any existing PyPI version/tag/release, gates
  the exact source SHA through the full test/dependency/artifact matrix, creates
  and re-verifies the tag before Trusted Publishing, re-verifies it after any
  environment-approval delay, and compares the tested candidate's filenames/hashes
  with the exact downloaded PyPI wheel/sdist.
  Trusted Publishing emits attestations; the workflow cryptographically verifies their
  repository identity, generates a reproducible CycloneDX SBOM from the exact published
  wheel, then creates the GitHub release once and verifies the final tag, exact
  package/SBOM names, and digests. Partial PyPI publication fails closed and forces
  selection of the next legal version. These workflow checks are defense in depth:
  the live repository still needs a `pypi` environment restricted to protected
  `main` plus a `v*` tag ruleset that prevents updates and deletion. The
  solo-maintainer beta has no independent human deployment reviewer; that residual
  limitation must remain explicit.
  Both workflows gate on `scripts/check_dist_hygiene.py`, which asserts the wheel
  ships only the `moneybird_mcp` package and that no `.env`/tokens/approvals DB/sync
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

`tests/conftest.py` guards the suite's own environment. If pytest's temp root
(`<tempdir>/pytest-of-<user>`) exists but cannot be enumerated — which happens when it was
created by a process running under a different security context — every `tmp_path` test would
otherwise fail during fixture setup and read as a repository failure. The guard redirects the
run to a fresh basetemp and prints a loud notice in both the header and the summary. It is
pinned by `tests/test_pytest_environment.py`; do not silence it, because the notice is the
only signal that the machine (not the code) needs attention.

`tests/test_env_file_boundary.py` spawns subprocesses with a deliberately stripped environment.
Two Windows-specific traps live there: the per-user site-packages directory is derived from
`%APPDATA%`, which the allowlist drops on purpose, so the import path is resolved from the
running interpreter instead (`_import_paths`); and every `subprocess.run` needs an explicit
`stdin=subprocess.DEVNULL`, because inheriting an invalid stdin handle raises `WinError 6`
before the probe runs. Keep both when adding a probe there.

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
  daardoor niet meer van het OAuth-token af. Zet het niet terug naar HTTPS zonder de volgende
  val te kennen:
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
