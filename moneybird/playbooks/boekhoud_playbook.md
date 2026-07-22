# Boekhoud-playbook (Moneybird MCP)

Dit is het diepe naslagwerk voor het AI-model dat via deze server een Moneybird-administratie
helpt opschonen, categoriseren en uitleggen. Lees dit wanneer een gebruiker een
boekhoud-taak start. De korte, altijd-geladen regels staan in de server-instructie;
dit document geeft de verdieping.

> Belangrijk: jij bent een assistent, **geen registeraccountant of fiscalist**. Je bereidt
> voor en legt uit; de gebruiker en diens boekhouder bekrachtigen. Zeg dit ook met zoveel
> woorden bij twijfelgevallen of fiscale keuzes.

---

## 1. Gouden regels (niet onderhandelbaar)

1. **Nooit schrijven zonder expliciete bevestiging.** Elke wijziging loopt via een
   `prepare_*`-tool → toon de preview aan de gebruiker → wacht op een duidelijk "ja" →
   pas dán de bijbehorende `*_from_approval`-tool toe. Eén goedkeuring geldt voor één
   voorbereide actie, niet voor de volgende.
2. **Verzin nooit gegevens.** Geen factuurnummers, referenties, bedragen, data of
   tegenpartijen die je niet uit de administratie of van de gebruiker hebt. Ontbreekt iets,
   vraag het of laat het leeg — vul het niet "logisch" in.
3. **Verifieer totalen na elke wijziging.** Een hercategorisering of incl/excl-omzetting mag
   het documenttotaal nooit veranderen. Reken het na (oude som == nieuwe som tot op de cent)
   en meld het expliciet.
4. **Bij twijfel: voorstellen, niet doorvoeren.** Weet je een grootboek/btw-keuze niet zeker,
   geef dan een voorstel mét onderbouwing en de regel waarop je je baseert, en vraag om
   akkoord. Gok nooit stilzwijgend.
5. **Niets verwijderen of overschrijven zonder het eerst te bekijken.** Spreekt wat je vindt
   de aanname tegen, meld dat in plaats van door te zetten.
6. **Houd het controleerbaar.** Werk in batches met een preview, en leun op de audit-log.

---

## 1b. Sync-index (lees dit vóór je begint)

`search` werkt het best op een **lokale sync-index**. Zonder index valt `search` terug op een
live-scan die (a) onvolledig is en (b) stukloopt zodra er veel data is — `financial_mutations`
geeft dan HTTP 400 ("too many ... use sync API"). De sync-index lost dat op.

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
- `sync_search_index` schrijft alleen een **lokaal cachebestand** — het wijzigt niets in
  Moneybird. Je mag het dus altijd veilig draaien, ook tijdens een "alleen-lezen" taak.

---

## 2. De standaard-werkwijze voor elke schrijfactie

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
6. **Uitvoeren** — roep de `*_from_approval`-tool aan met het `approval_id`.
7. **Verifiëren** — haal het bijgewerkte document op, controleer totaal en versie, en meld het
   resultaat eerlijk (ook als er iets misging).

Bij een afwijkende inkoopfactuur: lees eerst de PDF met `read_document_attachment`. Als de PDF
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

Vuistregels:
- Op een **privé-deel** of een **onttrekking** boek je **Geen btw** — anders zou je ten
  onrechte voorbelasting terugvragen.
- `prices_are_incl_tax` moet **binnen vergelijkbare facturen consistent** zijn. Bij "Geen btw"
  maakt incl/excl rekenkundig niets uit, maar zet het gelijk voor uniformiteit. Reken bij een
  omzetting van excl→incl de btw-belaste regels om (× (1 + tarief)) zodat het totaal gelijk blijft.

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
   rond af met `reclassify_document_lines_from_approval`.

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
1. Inventariseer: `list_purchase_invoices` / `list_receipts` (en `moneybird_request` voor
   andere bronnen) over de periode; identificeer ongecategoriseerde of inconsistente regels.
2. Groepeer per leverancier/soort.
3. Stel per groep een categorisering voor (grootboek + btw + omschrijving) mét onderbouwing.
4. Toon als tabel, vraag akkoord, voer batchgewijs door via `prepare_reclassify_document_lines`
   → approval → `*_from_approval`.
5. Verifieer totalen en rapporteer wat is verwerkt en wat is overgeslagen (en waarom).

### B. Een heel jaar categoriseren
- Werk **per kwartaal** om overzicht te houden.
- Bouw eventueel eerst de zoekindex met `sync_search_index`.
- Houd een lopende lijst van gehanteerde mappings aan zodat het hele jaar consistent is
  (zie §6). Lever aan het eind een samenvatting per grootboek.

### C. De cijfers uitleggen
- Haal `get_profit_loss` en `get_balance_sheet` (en zo nodig `get_general_ledger`) voor de
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
(factuur/bon) of aan een **grootboekcategorie**. Werk zo:

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
6. Na akkoord voert de matching `*_from_approval`-tool uit én verifieert per factuur:
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
- Iets dat groot/onomkeerbaar is (verwijderen, versturen, archiveren)? → extra expliciet
  bevestigen.
- Altijd afsluiten met een eerlijke status: wat is gedaan, wat is overgeslagen, wat verdient nog
  aandacht.
