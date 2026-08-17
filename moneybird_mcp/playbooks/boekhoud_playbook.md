# Boekhoud-playbook (Moneybird MCP)

Dit is het diepe naslagwerk voor het AI-model dat via deze server een Moneybird-administratie
helpt opschonen, categoriseren en uitleggen. Lees dit wanneer een gebruiker een
boekhoud-taak start. De korte, altijd-geladen regels staan in de server-instructie;
dit document geeft de verdieping.

> Belangrijk: jij bent een assistent, **geen registeraccountant of fiscalist**. Je bereidt
> voor en legt uit; de gebruiker en diens boekhouder bekrachtigen. Zeg dit ook met zoveel
> woorden bij twijfelgevallen of fiscale keuzes.
>
> De server start in `read_only`. Schrijven bestaat alleen voor lokaal gebruik of
> `network_single_user` wanneer de beheerder
> `MONEYBIRD_CAPABILITY_MODE=write_enabled` expliciet aanzet. In
> `hosted_request_only` worden alle writes geweigerd, ongeacht die instelling.

---

## 1. Gouden regels (niet onderhandelbaar)

1. **Nooit schrijven zonder expliciete bevestiging.** Als schrijven door de beheerder is
   aangezet, loopt elke wijziging via een
   `prepare_*`-tool → toon de preview aan de gebruiker → wacht op een duidelijk "ja" →
   pas dán `execute_approved_action` met dat approval_id. Eén goedkeuring geldt voor één
   voorbereide actie, niet voor de volgende. Een `approval_id` bindt de voorbereide
   payload, maar bewijst op zichzelf niet dat een mens buiten het model heeft bevestigd;
   het MCP-clientkanaal moet die bevestiging betrouwbaar organiseren.
2. **Verzin nooit gegevens.** Geen factuurnummers, referenties, bedragen, data of
   tegenpartijen die je niet uit de administratie of van de gebruiker hebt. Ontbreekt iets,
   vraag het of laat het leeg — vul het niet "logisch" in.
3. **Verifieer het actiespecifieke resultaat na elke wijziging.** Een hercategorisering of
   incl/excl-omzetting mag
   het documenttotaal nooit veranderen. Reken het na (oude som == nieuwe som tot op de cent)
   en meld het expliciet. Meld een gedeeltelijke, ambigue of mislukte uitkomst nooit als
   volledig succes.
4. **Bij twijfel: voorstellen, niet doorvoeren.** Weet je een grootboek/btw-keuze niet zeker,
   geef dan een voorstel mét onderbouwing en de regel waarop je je baseert, en vraag om
   akkoord. Gok nooit stilzwijgend.
5. **Niets verwijderen of overschrijven zonder het eerst te bekijken.** Spreekt wat je vindt
   de aanname tegen, meld dat in plaats van door te zetten.
6. **Houd het controleerbaar.** Werk in batches met een preview, en leun op de audit-log.

---

## 1b. Sync-index (alleen lokaal of `network_single_user`)

`search` werkt het best op een **lokale sync-index**. Zonder index valt `search` terug op een
live-scan die (a) onvolledig is en (b) stukloopt zodra er veel data is — `financial_mutations`
geeft dan HTTP 400 ("too many ... use sync API"). De sync-index lost dat op.

In `hosted_request_only` leest `search` uitsluitend live uit Moneybird en kan het resultaat
gedeeltelijk zijn. Daar worden zowel `sync_search_index` als toegang tot de duurzame
JSON/SQLite/FTS-index geweigerd.

Wanneer bouw/ververs je de index met `sync_search_index`:

- **Vóór** elke achterstand-, categorisatie- of heel-jaar-taak. Doe dit als eerste stap.
- Zodra een `search`-resultaat `"source": "live_fallback"` of een `"warnings"`-veld bevat →
  draai `sync_search_index` en zoek opnieuw.
- **Na** wijzigingen of bij recente data: opnieuw draaien. Het is goedkoop, want het haalt
  alleen gewijzigde records op (versioned sync).

Belangrijk om te weten:

- De index is **per administratie** (elke tenant z'n eigen cachebestand) en een **momentopname**
  (`updated_at` staat in het resultaat). Het is geen live spiegel.
- De index respecteert de filters waarmee je 'm bouwt (standaard `period:this_year`). Voor oudere
  jaren geef je een ruimer `*_filter` mee, anders mist `search` die records.
- `sync_search_index` schrijft alleen een **lokaal cachebestand** en wijzigt niets in
  Moneybird. Het kan daarom in lokaal of `network_single_user` gebruik ook in
  `read_only` draaien; in `hosted_request_only` wordt het geweigerd.

---

## 2. De standaard-werkwijze voor elke schrijfactie

Deze sectie geldt alleen als een beheerder schrijven expliciet heeft aangezet in lokaal of
`network_single_user` gebruik. Een assistent mag de capability-instelling niet zelf omzeilen.

1. **Lezen** — haal de relevante documenten/regels op (`list_*`, `search`, `fetch`,
   `moneybird_request` voor niet-gewrapte endpoints). Geeft de gebruiker een exact
   inkoopfactuurnummer, gebruik dan `get_purchase_invoice_by_reference`; zo voorkom je een brede,
   onvolledige live-search.
2. **Analyseren** — bepaal per regel wat er moet veranderen en waarom.
3. **Voorbereiden** — roep de juiste `prepare_*`-tool aan; die geeft een `approval_id` +
   preview terug.
4. **Tonen** — laat de preview als tabel zien: wat verandert, van → naar, en het effect op
   het totaal (moet gelijk blijven bij herclassificatie).
5. **Bevestigen** — wacht op een expliciet "ja" van de gebruiker.
6. **Uitvoeren** — roep `execute_approved_action` aan met het `approval_id` (de
   uitvoering loopt via die ene, destructief geannoteerde tool).
7. **Verifiëren** — haal het bijgewerkte document op, controleer totaal en versie, en meld het
   resultaat eerlijk (ook als er iets misging).

De toolannotatie, preview en het `approval_id` helpen een actie af te bakenen, maar zijn geen
onafhankelijk identiteits- of menselijk-bevestigingsmechanisme.

Bestaat één opdracht uit zowel correcties op inkoopfacturen als herclassificaties van directe
bankboekingen, maak dan één taakpreview met `prepare_bookkeeping_correction_batch`. De workflow
hergebruikt de bestaande guarded acties, controleert bij een gemengde batch alle versies en
bronboekingen vóór de eerste write en levert één `approval_id`. Na het expliciete akkoord voert
`execute_approved_action` die exacte combinatie uit. Moneybird biedt geen transactie over
verschillende objecten; rapporteer `completed_with_errors` en de geaudite partial progress dus
als herstelstatus, niet als volledig succes.

Bij een afwijkende inkoopfactuur in lokaal of `network_single_user` gebruik: lees eerst de PDF
met `read_document_attachment`. In `hosted_request_only` is downloaden en parsen uitgeschakeld.
Als de PDF
de echte regelbedragen bevat, geef die als exacte `desired_lines` aan
`prepare_reconcile_purchase_invoice` met een korte `source_note`. Gebruik alleen de
referentiefactuurmodus wanneer de actuele regels niet uit de bron zijn af te leiden; proportioneel
schalen blijft dan een expliciet te bevestigen aanname. De approval bevat een documentversie en
wordt bij tussentijdse wijzigingen veilig geweigerd.

---

## 3. BTW (NL) — snel beslismodel

| Situatie | Tarief / behandeling |
|---|---|
| Normale goederen/diensten | **21% btw** |
| Voeding, boeken, personenvervoer, e.d. | **9% btw** |
| Export/intracommunautair, verlegd | **0% / btw verlegd** |
| Privé-uitgave of onttrekking | **Geen btw** (je trekt geen voorbelasting terug) |
| Verzekeringen, postzegels, sommige vrijgestelde diensten | **Geen / vrijgesteld** |
| Bankkosten, pakketkosten, rente en andere financiële diensten | **Geen btw** (vrijgesteld) |

Vuistregels:
- Op een **privé-deel** of een **onttrekking** boek je **Geen btw** — anders zou je ten
  onrechte voorbelasting terugvragen.
- **Bankkosten zijn altijd vrijgesteld van btw.** Financiële diensten vallen onder de
  btw-vrijstelling, dus een bankafschrijving voor pakket- of transactiekosten kent geen
  voorbelasting. Zo'n mutatie hoort daarom **rechtstreeks op het grootboek Bankkosten**
  (`prepare_link_bank_mutation_booking` met `booking_type: LedgerAccount`); de waarschuwing
  dat een directe grootboekboeking geen btw-post aanmaakt is hier dus juist het gewenste
  gedrag, en geen reden om alsnog een factuur of memoriaal te zoeken.
- `prices_are_incl_tax` moet **binnen vergelijkbare facturen consistent** zijn. Bij "Geen btw"
  maakt incl/excl rekenkundig niets uit, maar zet het gelijk voor uniformiteit. Reken bij een
  omzetting van excl→incl de btw-belaste regels om (× (1 + tarief)) zodat het totaal gelijk blijft.

### Afronding: het aangegeven bedrag is nooit gelijk aan het exacte saldo

De aangifte wordt in **hele euro's** ingevuld en mag **in het voordeel van de ondernemer**
worden afgerond: verschuldigde btw naar beneden, voorbelasting naar boven. Het betaalde bedrag
ligt daardoor structureel **onder** het exacte saldo uit de administratie. Dat is correct.

Rekenvoorbeeld: verschuldigd 5.225,75 → 5.225; voorbelasting 801,70 → 802; te betalen
5.225 − 802 = **4.423**, tegen een exact saldo van 4.424,05. Het voordeel is hier €1,05 en
groeit met elke extra ingevulde rubriek.

- **Meld een verschil niet als fout** voordat je het tegen deze regel hebt nagerekend.
- **Hardcodeer geen tolerantie** (zoals "hooguit €1,99"). Hoe meer rubrieken zijn ingevuld,
  hoe groter het afrondingsvoordeel kan zijn. Er bestaat geen veilige bovengrens.
- **Leid het aangegeven bedrag nooit af.** Vraag het aan de gebruiker of lees het uit
  Moneybirds btw-overzicht of de ingediende aangifte. `prepare_vat_settlement_journal`
  eist het daarom als expliciete parameter.
- Het verschil is een resultaatpost en hoort naar **Afrondingsverschillen**, niet naar het
  btw-saldo.

---

## 3b. Btw-afwikkeling: de aangifteperiode schoonboeken

Een betaling aan de Belastingdienst boeken is **de helft** van de verwerking. Moneybird boekt
verkoop-btw op *Te betalen btw* en voorbelasting op *Te vorderen btw*, maar het indienen van de
aangifte verplaatst geen van beide saldi. Zonder afwikkelboeking lopen die rekeningen kwartaal
na kwartaal door en wordt de btw-positie op de balans betekenisloos — ook als elke losse
betaling correct geboekt is.

**Diagnose.** Lopen *Te betalen btw* en *Te vorderen btw* over meerdere kwartalen op zonder ooit
leeg te lopen, dan wordt de aangifte buiten Moneybird om gedaan. Signaleer dat; saldeer niet
eigenhandig en niet met één grote verzamelboeking — herstel per aangifteperiode, op basis van de
werkelijk ingediende aangiftes.

**Bruto versus netto — de valkuil.** Verlegde btw (`btw verlegd`, intracommunautair) wordt
geboekt als verschuldigd **én** als aftrekbaar voor hetzelfde bedrag. Daardoor staan beide
grootboekrekeningen bruto hoger, terwijl het netto te betalen bedrag ongewijzigd blijft. Twee
gevolgen:

1. De afwikkelboeking moet de **bruto** mutaties schoonboeken. Boek je alleen wat in het
   btw-rapport zichtbaar is, dan blijft het verlegde bedrag aan beide kanten staan.
2. Een gelijk verschil aan beide kanten dat elkaar netto opheft is **geen afwijking**. Leg uit
   waar het vandaan komt in plaats van het te melden. Alleen het deel dat *niet* wegvalt is een
   echte discrepantie.

Zo herken je het: de bruto grootboekstand ligt aan *beide* kanten exact hetzelfde bedrag hoger
dan het btw-rapport, terwijl het netto saldo tot op de cent gelijk blijft. Dat gelijke bedrag is
de verlegde btw over de periode.

> **Let op wat dit wél en niet aantoont.** Een gelijk verschil aan beide kanten bewijst alleen
> dat de verlegde btw netto wegvalt. Het zegt **niets** over de vraag of die mutaties bij deze
> periode horen. Dat volgt uit het opgegeven datumbereik en uit de datums van de onderliggende
> records — controleer dat apart en leun er niet op dat de twee bedragen toevallig gelijk zijn.

**Werkwijze.**

1. `analyze_vat_settlement(period)` — toont de bruto grootboekmutaties, de gerapporteerde
   rubrieken en of een gat tussen beide door verlegde btw wordt verklaard. Controleert bruto en
   netto **apart**.
2. Vraag het werkelijk aangegeven en betaalde bedrag (zie de afrondingsregel in §3).
3. `prepare_vat_settlement_journal(period, reference, declared_amount, ...)` — bouwt het
   memoriaal: bruto *Te betalen btw* debet, bruto *Te vorderen btw* credit, het aangegeven bedrag
   credit op de afrekenrekening, en het restant naar Afrondingsverschillen.

   Het memoriaal balanceert per definitie, dus élke fout in het bedrag verdwijnt anders geruisloos
   naar Afrondingsverschillen en komt daarna als "geverifieerd" terug. Daarom weigert de preflight:

   - een periode die al een afwikkeling met dezelfde referentie heeft, of geen bruto mutatie kent;
   - een journaaldatum óf periode-einde op/vóór `period_locked_until` (beide, zodat een latere
     datum geen vergrendeld kwartaal alsnog afwikkelt);
   - een journaaldatum die niet gelijk is aan het periode-einde — laat `date` leeg, dan wordt die
     automatisch gekozen;
   - een aangegeven bedrag dat niet in hele euro's is, of dat verder van de grootboekpositie ligt
     dan het afronden van het aantal rubrieken kan verklaren (afgeleide grens, geen vaste marge);
   - een onverklaarde bruto-versus-gerapporteerde afwijking.

   `allow_date_outside_period` en `allow_unexplained_difference` zetten de laatste twee opzettelijk
   opzij; ze worden in de approval vastgelegd. Gebruik ze pas als de oorzaak vaststaat.

   Een periode met netto nul maar wél bruto mutatie (alleen verlegde btw) is juist wél afwikkelbaar
   — anders blijft die staan.
4. Na akkoord: `execute_approved_action` (de actie is `settle_vat_period`). De executor leest
   grootboekstanden, slotdatum en bestaande afwikkelingen **opnieuw** vlak vóór de write en breekt
   af bij elk verschil met de goedgekeurde momentopname; achteraf controleert hij niet alleen het
   document maar ook dat de btw-rekeningen van de periode werkelijk op nul staan.
5. Boek de bankbetaling apart op de afrekenrekening. Die debiteert wat het memoriaal
   crediteerde, waarna de rekening voor die periode op nul staat.

De analyse zoekt alleen de rekeningen die zij werkelijk leest; een afrondingsrekening is daar
geen voorwaarde. De prepare-flow vindt *Te betalen btw*, *Te vorderen btw* en *Betaalde en/of
ontvangen btw* op naam en zoekt *Afrondingsverschillen* pas op als het aangegeven bedrag een
niet-nulafrondingsregel oplevert. Elke gebruikte rekening is per id te overschrijven. Ontbreekt
de benodigde afrondingsrekening, dan noemt de fout `rounding_ledger_account_id`, verwijst zij naar
`prepare_create_ledger_account` en toont zij hooguit drie plausibele kandidaten — vraag het aan
de gebruiker in plaats van te gokken.

Twee technische randvoorwaarden:

- **Het `tax`-rapport is hard begrensd op één maand.** Alles wat langer is geeft
  `{"error":"Period cannot exceed 1 month"}` — `this_quarter`, `prev_quarter`, `this_year`, een
  dagbereik over twee maanden én de maandbereik-syntax `202604..202606`. Er is geen parameter die
  dat opheft (`grouping=quarter` wordt genegeerd). Let op de exacte grens: hij is *maximaal* één
  maand, niet *precies* een kalendermaand — `20260401..20260430` werkt gewoon. Een kwartaal haal
  je dus per maand op en tel je zelf op. (Live geverifieerd 2026-08-01.)
- De afwikkeltool eist daarbovenop een **bereik van hele maanden** (`20260401..20260630`):
  symbolische perioden en halve maanden worden geweigerd in plaats van stilzwijgend verkeerd
  opgeteld. Die eis is van de tool, niet van de API.
- Een memoriaal heeft in Moneybird **geen header-omschrijving**: het veld ontbreekt in het
  teruggegeven record (live geverifieerd 2026-08-01). Stuur je hem toch op documentniveau mee,
  dan faalt de nacontrole op elke boeking. `prepare_create_general_journal_document` schuift een
  meegegeven `description` daarom door naar elke regel die er zelf geen heeft.

---

## 4. Privé vs. zakelijk en onttrekkingen

- **Zakelijke kosten** → kostenrekening (expenses/direct_costs). Aftrekbaar, btw (indien van
  toepassing) terugvorderbaar.
- **Privé-deel** van een gemengde uitgave → **Onttrekkingen** (eigen vermogen), **Geen btw**.
  Dit verlaagt de winst niet en vordert geen btw terug.
- **Gemengd** (bijv. internet/telefoon 50/50): splits in een zakelijke regel (kosten, met btw)
  en een privé-regel (Onttrekkingen, geen btw). Houd de verhouding expliciet en consistent
  over vergelijkbare facturen.
- Betalingen aan een **meewerkend familielid** kunnen als kostenpost óf als onttrekking lopen —
  dat is een fiscale keuze met gevolgen. Wijzig dit niet eigenhandig; signaleer en laat de
  boekhouder bevestigen.

---

## 5. Categoriseren: hoe kies je een grootboek?

1. Haal de geldige grootboeken op met `list_ledger_accounts` (gebruik echte `ledger_account_id`'s;
   alleen `expenses`, `direct_costs`, `other_income_expenses` zijn boekbaar op inkoopdocumenten).
2. Kijk naar **leverancier + omschrijving + bedrag** om de aard te bepalen.
3. Wees **consistent**: dezelfde soort uitgave van dezelfde leverancier → hetzelfde grootboek,
   dezelfde omschrijvingsstijl, dezelfde btw-behandeling.
4. Twijfel je tussen twee rekeningen, kies de meest specifieke en **leg je keuze uit**; markeer
   onzekere posten apart zodat de gebruiker/boekhouder ze kan nalopen.
5. Voer herclassificatie uit met `prepare_reclassify_document_lines`. Elke `entries`-regel:

   ```json
   {
     "document_kind": "purchase_invoice",   // of "receipt"
     "document_id": "<document-id>",
     "detail_id": "<regel-id>",              // of "row_order": <n>
     "ledger_account_id": "<grootboek-id>"   // of "ledger_account_name": "<naam>"
   }
   ```

   Optioneel per regel: `tax_rate_id`, `description`. Toon de preview, wacht op akkoord, en
   rond af met `execute_approved_action`.

---

## 6. Consistentie-checklist (uniform verwerken)

Pas deze checks toe wanneer je "alles op dezelfde manier" wilt zetten binnen een set
vergelijkbare facturen (bijv. één leverancier, één heel jaar):

- [ ] **Grootboek**: zelfde soort regel → zelfde `ledger_account_id`.
- [ ] **BTW**: zelfde tarief/behandeling; privé/onttrekking = Geen btw.
- [ ] **`prices_are_incl_tax`**: overal gelijk (incl. of excl.), totalen onveranderd.
- [ ] **Omschrijving**: vaste, herkenbare bewoording per regelsoort.
- [ ] **Aantal/notatie**: uniform (bijv. kaal getal `1` i.p.v. een mix van `1`, `1 x`, `1.0`).
- [ ] **Periode (dienstperiode)**: ingevuld en sluitend (bij abonnementen een doorlopende
      reeks zonder gaten/overlap), formaat `JJJJMMDD..JJJJMMDD`.
- [ ] **Referentie**: laat staan als je het juiste nummer niet hebt — **verzin het nooit**.

---

## 7. Scenario-recepten

### A. Achterstallige boekhouding wegwerken
1. Inventariseer: `list_purchase_documents` (kind purchase_invoice of receipt; en `moneybird_request` voor
   andere bronnen) over de periode; identificeer ongecategoriseerde of inconsistente regels.
2. Groepeer per leverancier/soort.
3. Stel per groep een categorisering voor (grootboek + btw + omschrijving) mét onderbouwing.
4. Toon als tabel, vraag akkoord, voer batchgewijs door via `prepare_reclassify_document_lines`
   → approval → `execute_approved_action`.
5. Verifieer totalen en rapporteer wat is verwerkt en wat is overgeslagen (en waarom).

### B. Een heel jaar categoriseren
- Werk **per kwartaal** om overzicht te houden.
- Bouw eventueel eerst de zoekindex met `sync_search_index`.
- Houd een lopende lijst van gehanteerde mappings aan zodat het hele jaar consistent is
  (zie §6). Lever aan het eind een samenvatting per grootboek.

### C. De cijfers uitleggen
- Haal `get_financial_report("profit_loss")` en `("balance_sheet")` (en zo nodig
  `("general_ledger")`) voor de
  periode op.
- Vat samen in mensentaal: omzet, grootste kostenposten, resultaat, opvallende verschuivingen.
- Noem de paar cijfers die er echt toe doen; vermijd een muur van getallen. Wijs op posten die
  controle verdienen (bijv. een ongewoon hoge "diversen"/ongecategoriseerd).

### D. Maand/kwartaal afsluiten + btw-check
- Controleer of alle inkoopdocumenten gecategoriseerd zijn en een correcte btw-behandeling
  hebben.
- Check bankmutaties (`list_financial_mutations`) op niet-gekoppelde posten.
- Signaleer afwijkingen; voer niets door zonder akkoord.

### E. "Waarom is deze bankmutatie niet automatisch verwerkt?"
Een veelgestelde vraag. Een bankmutatie is "verwerkt" als hij gekoppeld is aan een **document**
(factuur/bon) of aan een **grootboekcategorie**.

> **Eerst matchen, dan diagnosticeren.** Wil je onverwerkte mutaties *wegwerken* (niet
> uitzoeken waarom er één blijft hangen), gebruik dan `suggest_bank_mutation_matches`. Die
> doet dezelfde koppeling die Moneybirds eigen transactiescherm voorstelt, maar dan
> deterministisch: referentie in de omschrijving, exact openstaand bedrag, IBAN van de
> tegenpartij, contactnaam — met per kandidaat de reden die is afgegaan. Reconstrueer dat
> niet met de hand uit `debtors`/`creditors` en factuurlijsten: dat kost veel meer calls
> tegen een limiet van 150 per 5 minuten, en gokken op bedrag alleen is precies hoe een
> verkeerde koppeling ontstaat.
>
> Let op de uitkomst `ambiguous`: die betekent dat meerdere facturen even goed passen —
> typisch een vaste maandfactuur van hetzelfde bedrag zonder factuurnummer in de
> omschrijving. Dat is niet op te lossen door de eerste te kiezen; vraag het de gebruiker.
>
> Geeft de tool een sterke `group_match`, dan tellen twee of meer uitgaande mutaties van
> dezelfde tegenpartij uniek en exact op tot het volledige open bedrag van één
> inkoopfactuur. Presenteer die complete groep meteen als één voorstel. Gebruik na akkoord
> `prepare_settle_purchase_invoice_from_bank_mutations`: één approval preflight alle
> mutaties en de factuur, koppelt de groep, verwerkt een nog `new` staande factuur zonder
> boekingsregels of btw te veranderen, en verifieert de eindstatus `paid`. Bij een
> alternatieve subset of concurrerende factuur blijft de uitkomst `ambiguous`.

Voor de diagnose van één blijvend onverwerkte mutatie werk je zo:

1. **Haal de mutatie op** en lees de sleutelvelden:
   - `state`: `processed` of `unprocessed`.
   - `payments`: gevuld → gekoppeld aan een **document** (factuur/bon).
   - `ledger_account_bookings`: gevuld → geboekt op een **categorie** (grootboek).
   - leeg op beide + `unprocessed` → nog **niet verwerkt**.
   - `contra_account_name` / `contra_account_number` (IBAN tegenpartij), `amount` (negatief =
     uitgaand), `sepa_fields.remi` (omschrijving/mededeling).
2. **Vergelijk met de historie van dezelfde tegenpartij** (filter op `contra_account_number`
   over meerdere maanden). Zo zie je het normale patroon: gaat deze tegenpartij normaal naar een
   **categorie** of naar een **factuur**? Wat is dan deze keer anders?
3. **Inkomende betaling die niet matcht?** Auto-matching aan een verkoopfactuur lukt alleen bij
   overeenkomst op **bedrag + IBAN-tegenrekening + referentie/factuurnummer**. Veelvoorkomende
   oorzaak: het bedrag past bij een openstaande factuur, maar die staat op een **ander contact**
   dan waar de betalende IBAN aan hangt (bijv. een handelsnaam vs. de persoon), of de mededeling
   is niet gelijk aan het Moneybird-factuurnummer. Dan durft Moneybird niet automatisch te
   koppelen. Oplossing: handmatig koppelen, of de IBAN/contacten gelijktrekken.
4. **Uitgaande betaling die niet matcht?** Auto-koppeling aan een inkoopfactuur lukt alleen als
   er een **openstaande** inkoopfactuur is die past. Is de bijbehorende factuur er nog niet
   (bijv. de maandfactuur is nog niet ingeboekt) of al betaald, dan blijft de mutatie staan.
5. **Boekingsregel vermoed? Let op de grens (zie §8): de API toont boekingsregels niet.** Je kunt
   niet uitlezen óf er een regel is of hoe die staat ingesteld. Leid het gedrag af uit de
   tijdstempels: vergelijk `created_at` (import) met `processed_at`.
   - Verwerkt **in dezelfde minuut** als import → wijst op automatisch boeken (of directe
     handmatige actie op dat moment).
   - Verwerkt **uren/dagen ná** een nachtelijke bankimport (import rond 00:50) → de regel doet
     hooguit een **voorstel** en iemand bevestigt het later handmatig; er boekt niets vanzelf.
   - `processed_at` = `null` op een net (vannacht) geïmporteerde mutatie → wacht simpelweg nog
     op handmatige bevestiging; dit is geen storing.
6. **Conclusie eerlijk formuleren.** Zeg wat je wél kunt vaststellen (uit het gedrag) en wat je
   **niet** kunt (de letterlijke regelinstelling). Verwijs de gebruiker voor de regelinstelling
   naar Moneybird zelf: **Instellingen → Boekhouding → Boekingsregels** (staat de regel op
   "automatisch verwerken" of op "voorstel doen"?).

Moeten meerdere reeds verwerkte bankmutaties van het ene rechtstreekse grootboek naar het
andere, gebruik dan `prepare_reclassify_bank_mutation_bookings` in plaats van losse
unlink/link-approvals. Selecteer per mutatie de exacte `ledger_account_booking_id`. De batch
controleert alle mutatieversies en bronboekingen voordat de eerste write plaatsvindt, behoudt het
ondertekende bedrag, verifieert `state` en `amount_open`, en probeert bij een mislukte doelkoppeling
de bronboeking te herstellen. Moneybird heeft hiervoor geen API-transactie over meerdere
mutaties; behandel `completed_with_errors` daarom als een expliciete herstelstatus, nooit als
succes.

Hoort die bankcorrectie bij een correctie van de onderliggende inkoopfactuur (bijvoorbeeld één
boekhoudersmail over beide), zet de factuurreconciliatie en bankherclassificatie samen in
`prepare_bookkeeping_correction_batch`. Zo krijgt de gebruiker één controleerbare preview en
worden alle onderdelen van de gemengde taak vóór de eerste wijziging opnieuw gecontroleerd.

Technische valkuil: Moneybird verwacht bij `link_booking` een positieve `price_base`, maar geeft
de nieuwe boeking terug met een ondertekend `price`. De MCP-client vertaalt dit automatisch.
Controleer na elke koppeling niet alleen dat er een nieuwe booking-id is, maar ook dat het
teruggegeven bedrag/teken klopt, `amount_open` de verwachte waarde heeft en een volledig gesloten
mutatie weer `processed` is.

> Periode-valkuil: `list_financial_mutations` met een ruime `period` geeft HTTP 400
> ("Too many financial mutations ... use sync API"). Vraag per **maand** op
> (`period:"JJJJMM01..JJJJMMnn"`) of gebruik de sync-index. Een enkele maand met
> `period:"JJJJMM"` kan ook 400 geven ("Period is invalid"); gebruik dan het datumbereik.

### F. Meterverbruik factureren

1. Gebruik `prepare_meter_usage_sales_invoices` met per meter `begin_reading` +
   `end_reading`, of met een expliciete `usage_kwh`.
2. Geef uitzonderingen aan via `action: "skip"`, `skip_meters` of een controleerbare
   `minimum_usage_kwh`. De preview moet elke overgeslagen meter en reden tonen.
3. Laat tarief, btw en grootboek uit de nieuwste passende meterregel overnemen. Ontbreekt
   zo'n regel, geef expliciete defaults; neem nooit automatisch de eerste huurregel over.
4. Gebruik een stabiele periodereferentie zoals `STROOM-2026-K2-B5` voor betrouwbare
   duplicaatcontrole.
5. Plan nieuwe facturen direct in dezelfde voorbereide batch. Bestaan de concepten al,
   gebruik `prepare_batch_schedule_sales_invoices`.
6. Na akkoord voert `execute_approved_action` uit én verifieert per factuur:
   totaal, status, factuurdatum en dat `sent_at` nog leeg is bij toekomstige verzending.

---

## 8. Bij twijfel / grenzen

- Onzeker over een fiscale keuze (aftrekbaarheid, privé/zakelijk, btw-tarief)? → **voorstellen +
  uitleggen + naar de boekhouder verwijzen**, niet zelf beslissen.
- Endpoint niet als tool beschikbaar? → `moneybird_request` (alleen lezen).
- **Boekingsregels (bankregels) zitten niet in de API.** Je kunt ze niet uitlezen of wijzigen —
  endpoints als `transaction_rules`, `bank_rules`, `automatic_bookings` geven 404. Bevestig dit
  eerlijk en leid regelgedrag af uit de mutatie-velden en tijdstempels (zie §7-recept E).
  Voor de letterlijke instelling: verwijs naar Moneybird → Instellingen → Boekhouding →
  Boekingsregels.
- **Btw-aangiftes zitten evenmin in de API.** `tax_returns`, `vat_returns`, `vat_documents`,
  `vat_declarations`, `tax_declarations` en `financial_years` geven allemaal 404; de OpenAPI-spec
  kent alleen `reports/tax` en `tax_rates`. `VatDocument` is wél een geldig `booking_type` bij
  `link_booking`, maar zonder endpoint is die id niet te achterhalen — bouw geen flow op een id
  uit een browser-URL. Praktisch gevolg: is de aangifte in Moneybird opgesteld, laat de gebruiker
  de mutatie daar aan koppelen; is hij extern ingediend, gebruik dan de afwikkelboeking uit §3b.
  Wat je wél kunt lezen is `period_locked_until` op de administratie — dat vertelt of een
  verstreken periode nog openstaat voor boeken.
- Iets dat groot/onomkeerbaar is (verwijderen, versturen, archiveren)? → extra expliciet
  bevestigen.
- Altijd afsluiten met een eerlijke status: wat is gedaan, wat is overgeslagen, wat verdient nog
  aandacht.
