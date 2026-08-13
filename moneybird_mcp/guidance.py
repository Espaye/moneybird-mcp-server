"""User-facing guidance for the model: a reference resource and scenario prompts.

This is the "skill" layer. The MCP tools are the hands; this module is the craft.
It exposes:

* a read-only **resource** (``moneybird://playbook/bookkeeping``) holding the deep
  bookkeeping playbook, loaded on demand; and
* a small set of **prompts** (the named scenarios a user can invoke), each of which
  carries the hard guard-rails inline and points at the playbook for depth.

Registration is imperative via :func:`register_guidance` so this module does not need
to import the ``mcp`` instance (avoiding a circular import with ``tools``).
"""
from __future__ import annotations

import re
from pathlib import Path

PLAYBOOK_PATH = Path(__file__).with_name("playbooks") / "boekhoud_playbook.md"
PLAYBOOK_URI = "moneybird://playbook/bookkeeping"

# The non-negotiable rails, repeated inline in every prompt so they hold even when a
# client does not auto-attach resources.
GUARDRAILS = """\
Werk volgens deze vaste regels:
1. Schrijf NOOIT zonder expliciete bevestiging: gebruik een prepare_*-tool, toon de
   preview, wacht op een duidelijk "ja", en voer dan het teruggegeven approval_id uit met
   execute_approved_action.
2. Verzin NOOIT gegevens (factuurnummers, referenties, bedragen, data, tegenpartijen).
   Ontbreekt iets, vraag het of laat het leeg.
3. Verifieer na elke wijziging dat het documenttotaal ongewijzigd is (tot op de cent) en
   meld dat expliciet.
4. Bij twijfel: stel voor met onderbouwing en vraag akkoord; gok nooit stilzwijgend.
5. Je bent geen registeraccountant of fiscalist. Verwijs fiscale keuzes naar de boekhouder.
Lees voor de diepere werkwijze, btw-regels en categorisatie de resource %s.""" % PLAYBOOK_URI


def load_playbook() -> str:
    """Return the bookkeeping playbook markdown (read fresh so edits need no restart)."""
    try:
        return PLAYBOOK_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (
            "# Boekhoud-playbook ontbreekt\n\n"
            f"Verwacht bestand niet gevonden: {PLAYBOOK_PATH}.\n"
            "Val terug op de gouden regels in de server-instructie."
        )


# --------------------------------------------------------------------------- #
# Topic-addressable playbook
#
# The playbook is also published as an MCP *resource*, but a resource is read by
# the client, not by the model: Claude Desktop requires the user to attach one by
# hand and ChatGPT connectors do not read arbitrary resources at all. In those
# clients the deepest bookkeeping knowledge in this server is unreachable exactly
# when it is needed. A tool is always model-callable, so the same document is
# addressable per topic below — per topic rather than whole, because a 434-line
# dump for a single VAT question is its own kind of unusable.
# --------------------------------------------------------------------------- #

_HEADING = re.compile(r"^(#{2,3})\s+(.*)$")
_SECTION_KEY = re.compile(r"^([0-9]+[a-z]?|[A-Z])\.")

# topic -> (section keys in playbook order, one-line summary for the tool result)
PLAYBOOK_TOPICS: dict[str, tuple[tuple[str, ...], str]] = {
    "gouden_regels": (
        ("1", "2"),
        "Niet-onderhandelbare regels en de standaard-werkwijze voor elke schrijfactie.",
    ),
    "sync_index": (
        ("1b",),
        "Wanneer je de lokale zoekindex bouwt of ververst, en waarom search zonder index onvolledig is.",
    ),
    "btw": (
        ("3",),
        "BTW-beslismodel (NL): tarieven, privé/onttrekking, incl/excl-consistentie en de afrondingsregel.",
    ),
    "btw_afwikkeling": (
        ("3b",),
        "De aangifteperiode schoonboeken met een memoriaal: verlegde btw, bruto vs. gerapporteerd, afrondingsverschil.",
    ),
    "prive_zakelijk": (
        ("4",),
        "Privé versus zakelijk en onttrekkingen.",
    ),
    "categoriseren": (
        ("5",),
        "Hoe je een grootboekrekening kiest.",
    ),
    "consistentie": (
        ("6",),
        "Consistentie-checklist om een reeks documenten uniform te verwerken.",
    ),
    "bankmutaties": (
        ("7E",),
        "Waarom een bankmutatie niet automatisch is verwerkt, en wat je wel en niet kunt zien.",
    ),
    "achterstand": (
        ("7A", "7B"),
        "Achterstallige boekhouding wegwerken en een heel jaar categoriseren.",
    ),
    "cijfers_uitleggen": (
        ("7C", "7D"),
        "De cijfers uitleggen en een maand of kwartaal afsluiten inclusief btw-check.",
    ),
    "meterverbruik": (
        ("7F",),
        "Meterverbruik factureren.",
    ),
    "grenzen": (
        ("8",),
        "Bij twijfel: waar dit hulpmiddel ophoudt en de boekhouder begint.",
    ),
}


def _playbook_sections() -> dict[str, tuple[str, str]]:
    """Map a section key to ``(heading, body)``.

    Keys come from the heading's own numbering, so they survive rewording:
    ``## 3b. Btw-afwikkeling`` becomes ``3b`` and the recipe ``### E. ...`` under
    ``## 7. Scenario-recepten`` becomes ``7E``.

    An *unnumbered* ``###`` heading stays part of its parent section rather than
    becoming addressable on its own. That is not cosmetic: ``### Afronding`` under
    ``## 3. BTW`` carries the whole-euro rounding rule, and splitting it out would
    silently drop it from the ``btw`` topic — the exact gotcha the topic exists for.
    """
    sections: dict[str, tuple[str, str]] = {}
    current_key: str | None = None
    parent_key = ""
    lines: list[str] = []
    heading = ""

    def flush() -> None:
        if current_key:
            sections[current_key] = (heading, "\n".join(lines).strip())

    for line in load_playbook().splitlines():
        match = _HEADING.match(line)
        if not match:
            lines.append(line)
            continue
        level, title = len(match.group(1)), match.group(2).strip()
        key_match = _SECTION_KEY.match(title)
        if level == 3 and key_match is None:
            # Unnumbered subsection: keep it with its parent, heading and all.
            lines.append(line)
            continue
        flush()
        raw_key = key_match.group(1) if key_match else title[:12]
        if level == 2:
            parent_key = raw_key
            current_key = raw_key
        else:
            current_key = f"{parent_key}{raw_key}"
        heading = title
        lines = []
    flush()
    return sections


def playbook_topic(topic: str) -> dict[str, object]:
    """Return one topic's playbook sections, or the list of valid topics."""
    slug = str(topic or "").strip().lower().replace("-", "_")
    if slug not in PLAYBOOK_TOPICS:
        return {
            "error": (
                f"Unknown topic {topic!r}."
                if slug
                else "No topic given."
            ),
            "topics": {
                name: summary for name, (_, summary) in PLAYBOOK_TOPICS.items()
            },
        }
    keys, summary = PLAYBOOK_TOPICS[slug]
    sections = _playbook_sections()
    parts = [
        f"## {sections[key][0]}\n\n{sections[key][1]}"
        for key in keys
        if key in sections
    ]
    if not parts:
        # The playbook is edited by hand; a topic that no longer resolves must say
        # so rather than silently returning nothing.
        return {
            "topic": slug,
            "error": (
                f"The playbook has no section(s) {', '.join(keys)} any more. "
                f"Read the full document via the {PLAYBOOK_URI} resource."
            ),
        }
    return {
        "topic": slug,
        "summary": summary,
        "guidance": "\n\n---\n\n".join(parts),
        "other_topics": [name for name in PLAYBOOK_TOPICS if name != slug],
    }


def prompt_aan_de_slag() -> str:
    """Eerste kennismaking: wat kan deze boekhoudassistent en hoe blijft je administratie veilig."""
    return f"""\
Ik gebruik deze Moneybird-assistent voor het eerst. Leg me kort uit wat je voor me kunt doen
en laat het meteen zien met mijn eigen administratie.

{GUARDRAILS}

Werkwijze:
1. Stel jezelf in één alinea voor: je leest mijn Moneybird-administratie en kunt na mijn
   expliciete akkoord ook dingen wijzigen. Elke wijziging gaat via een preview
   (prepare_*-tool) en wordt pas na mijn "ja" uitgevoerd (execute_approved_action); er verandert
   dus nooit iets zonder dat ik het gezien heb.
2. Controleer de verbinding: haal list_administrations op en noem de administratie waar we
   in werken.
3. Geef me een eerste beeld van mijn boekhouding: get_financial_report("profit_loss",
   "this_year") en de openstaande posten (debtors en creditors; die rapporten accepteren
   maximaal één maand — gebruik "this_month"). Vat samen in mensentaal.
4. Noem daarna vijf concrete dingen die ik je zo kan vragen, bijvoorbeeld:
   - "Welke facturen staan nog open en wie moet ik aanmanen?"
   - "Verwerk mijn achterstallige bonnetjes en inkoopfacturen" (verwerk_achterstand)
   - "Koppel de onverwerkte banktransacties" (koppel_banktransacties)
   - "Maak een factuur voor klant X" of "crediteer factuur Y"
   - "Leg me mijn cijfers van dit kwartaal uit" (leg_cijfers_uit)
5. Vraag welke ik als eerste wil. Doe nog niets zonder akkoord; dit kennismakingsrondje is
   leesacties-alleen."""


def prompt_koppel_banktransacties(period: str = "", limit: str = "10") -> str:
    """Koppel mutaties, met één akkoord voor een eenduidige volledige groep."""
    wanneer = period.strip() or "de afgelopen maand"
    return f"""\
Help me de onverwerkte banktransacties wegwerken voor periode "{wanneer}"
(maximaal {limit or '10'} mutaties in deze ronde).

{GUARDRAILS}

Werkwijze:
1. Draai suggest_bank_mutation_matches (eventueel met period per maand —
   period:"JJJJMM01..JJJJMMnn" — want een te ruime periode geeft HTTP 400). Die tool haalt de
   onverwerkte mutaties op én zoekt er deterministisch de passende openstaande factuur bij:
   referentie in de omschrijving, exact openstaand bedrag, IBAN van de tegenpartij en
   contactnaam. Doe dat matchwerk niet zelf opnieuw met rapporten en factuurlijsten.
2. Neem per mutatie de uitkomst over:
   - sterke `group_matches`: één inkoopfactuur wordt exact volledig gedekt door twee of meer
     mutaties van dezelfde tegenpartij. Presenteer de hele groep meteen als één voorstel;
   - suggestion "exact" of "strong": één voorstel met de meegeleverde evidence als onderbouwing;
   - suggestion "ambiguous": meerdere kandidaten passen even goed (bijvoorbeeld dezelfde
     klant met twee openstaande facturen van hetzelfde bedrag). Leg beide voor en vraag welke;
     kies er nooit zelf één;
   - suggestion "none": geen openstaande factuur past. Kijk hoe deze tegenpartij eerder is
     geboekt (zelfde contra_account_number) en stel een grootboekcategorie voor.
3. Presenteer per mutatie één voorstel met onderbouwing: koppelen aan factuur/document
   (booking_type SalesInvoice of Document) of direct aan een categorie (LedgerAccount).
   Twijfelgevallen zet je apart met wat er mist; die koppel je niet.
4. Na akkoord:
   - sterke volledige groep: prepare_settle_purchase_invoice_from_bank_mutations →
     execute_approved_action. Dit ene approval koppelt alle mutaties, verwerkt een nog nieuwe
     inkoopfactuur met ongewijzigde boekingsregels en verifieert status `paid`;
   - losse match: prepare_link_bank_mutation_booking → execute_approved_action.
   Rapporteer per koppeling de verificatie (payments/ledger_account_bookings na afloop).
5. Fout gekoppeld? Herstel met prepare_unlink_bank_mutation_booking →
   execute_approved_action.
6. Staat een reeks mutaties rechtstreeks op het verkeerde grootboek? Gebruik
   prepare_reclassify_bank_mutation_bookings met de exacte
   ledger_account_booking_id per mutatie. Die flow preflight de volledige batch,
   bewaart versies en bedragen, en probeert de bronboeking te herstellen als een
   doelkoppeling mislukt.
7. Sluit af met een eerlijke samenvatting: gekoppeld, overgeslagen (en waarom), en wat een
   boekingsregel in Moneybird zelf zou kunnen automatiseren (die regels staan niet in de API)."""


def prompt_verwerk_achterstand(
    period: str = "this_year",
    document_kind: str = "purchase_invoice",
) -> str:
    """Werk een achterstand aan in- en uitgaande documenten weg, gecategoriseerd en consistent."""
    return f"""\
Help me mijn achterstallige boekhouding wegwerken voor periode "{period}" \
(documenttype: {document_kind}).

{GUARDRAILS}

Werkwijze:
1. Bouw/ververs eerst de sync-index met sync_search_index, zodat search compleet en snel is
   (zonder index valt search terug op een onvolledige live-scan die op grote data stukloopt).
2. Inventariseer de documenten met de list_*-tools (en moneybird_request voor bronnen zonder
   eigen tool) over deze periode; zoek ongecategoriseerde of inconsistente regels.
3. Groepeer per leverancier/soort en stel per groep een categorisering voor: grootboek
   (list_ledger_accounts geeft geldige id's), btw-behandeling en een uniforme omschrijving —
   telkens met korte onderbouwing.
4. Toon het voorstel als tabel (van → naar, effect op totaal = ongewijzigd).
5. Voer na mijn akkoord batchgewijs door via prepare_reclassify_document_lines →
   execute_approved_action.
6. Verifieer de totalen en geef een eerlijke samenvatting: wat is verwerkt, wat is
   overgeslagen en waarom."""


def prompt_categoriseer_heel_jaar(year: str = "") -> str:
    """Categoriseer een volledig boekjaar, kwartaal voor kwartaal en onderling consistent."""
    target = year.strip() or "het lopende boekjaar"
    return f"""\
Categoriseer mijn boekhouding voor {target}, kwartaal voor kwartaal en onderling consistent.

{GUARDRAILS}

Werkwijze:
1. Bouw eerst de zoekindex met sync_search_index. Voor een ouder jaar geef je een ruimer
   filter mee (bijv. period:20250101..20251231) zodat search dat jaar ook dekt.
2. Behandel het jaar per kwartaal om overzicht te houden.
3. Houd een lopende lijst van gehanteerde mappings bij (zelfde soort uitgave → zelfde
   grootboek, btw en omschrijvingsstijl) zodat het hele jaar uniform is. Volg de
   consistentie-checklist uit het playbook (grootboek, btw, incl/excl, omschrijving,
   aantal-notatie, periode, referentie).
4. Stel per kwartaal de wijzigingen voor, wacht op akkoord en voer ze door via de
   prepare_* → execute_approved_action-flow.
5. Lever aan het eind een samenvatting per grootboek en een lijst van posten die nog
   menselijke/boekhouderscontrole verdienen."""


def prompt_diagnose_bankmutatie(zoekterm: str = "", period: str = "") -> str:
    """Zoek uit waarom een bankmutatie niet automatisch is gekoppeld aan een categorie of document."""
    wie = zoekterm.strip() or "de betreffende tegenpartij/mutatie"
    wanneer = period.strip() or "de relevante maand"
    return f"""\
Zoek uit waarom een bankmutatie niet automatisch is verwerkt (gekoppeld aan een categorie of
document). Het gaat om: {wie} (periode: {wanneer}).

Dit is in principe een leesopdracht — wijzig niets zonder expliciet akkoord (de guard-rails
hieronder blijven gelden mocht de oplossing een schrijfactie vergen).

{GUARDRAILS}

Werkwijze (zie playbook §7-recept E voor de details):
1. Haal de mutatie op met list_financial_mutations (filter per maand:
   period:"JJJJMM01..JJJJMMnn"; een te ruime periode geeft HTTP 400). Lees state, payments
   (= documentkoppeling), ledger_account_bookings (= categorieboeking), contra_account_number,
   amount en sepa_fields.remi.
2. Vergelijk met de historie van dezelfde tegenpartij (zelfde contra_account_number, meerdere
   maanden) om het normale patroon te zien: gaat dit normaal naar een categorie of een factuur,
   en wat is nu anders?
3. Inkomend en geen match? Controleer of bedrag + IBAN + referentie kloppen, en of de
   openstaande factuur niet op een ander contact staat dan de betalende IBAN. Uitgaand en geen
   match? Controleer of er een openstaande inkoopfactuur is die past.
4. Vermoed je een boekingsregel: zeg er eerlijk bij dat de API boekingsregels NIET toont. Leid
   het gedrag af uit created_at vs processed_at (zelfde minuut = automatisch; uren/dagen later
   na een nachtelijke import = slechts een voorstel dat handmatig is bevestigd; processed_at
   null op een verse import = wacht nog op bevestiging).
5. Formuleer een eerlijke conclusie: wat staat vast uit het gedrag, wat kun je niet zien (de
   regelinstelling), en verwijs voor die instelling naar Moneybird → Instellingen → Boekhouding
   → Boekingsregels. Stel zo nodig een concrete vervolgactie voor (handmatig koppelen,
   IBAN/contacten gelijktrekken, ontbrekende factuur inboeken) en vraag akkoord."""


def prompt_leg_cijfers_uit(period: str = "this_year") -> str:
    """Leg de winst-en-verlies en balans voor een periode uit in begrijpelijke taal."""
    return f"""\
Leg mijn cijfers voor periode "{period}" uit in begrijpelijke taal.

Dit is een leesopdracht — wijzig niets.

Werkwijze:
1. Haal get_financial_report("profit_loss") en get_financial_report("balance_sheet") op
   (en zo nodig "general_ledger") voor deze
   periode.
2. Vat samen in mensentaal: omzet, de grootste kostenposten, het resultaat en opvallende
   verschuivingen. Noem de paar cijfers die er echt toe doen; vermijd een muur van getallen.
3. Wijs op posten die controle verdienen (bijv. een ongewoon grote "diversen" of
   ongecategoriseerde uitgaven) en stel concrete vervolgstappen voor.
4. Je bent geen fiscalist: presenteer dit als inzicht, niet als belastingadvies.

Zie voor context en vervolgacties (zoals categoriseren) de resource {PLAYBOOK_URI}."""


def prompt_factureer_meterverbruik(
    period_label: str = "",
    invoice_date: str = "",
    schedule_send_on: str = "",
) -> str:
    """Bereid een controleerbare batch meterverbruikfacturen voor."""
    return f"""\
Bereid meterverbruikfacturen voor periode "{period_label or 'onbekend'}" voor.
Factuurdatum: "{invoice_date or 'nog op te geven'}". Geplande verzenddatum:
"{schedule_send_on or 'niet automatisch inplannen'}".

{GUARDRAILS}

Werkwijze:
1. Neem per meter beginstand + eindstand of expliciet verbruik over. Reken verbruik na en
   corrigeer de brongegevens nooit stilzwijgend.
2. Leg per meter vast: customer_id, action (skip/draft/schedule/merge/separate) en eventueel
   een expliciete minimumgrens. Toon overgeslagen meters inclusief reden.
3. Gebruik prepare_meter_usage_sales_invoices. Laat tarief, btw en grootboek bij voorkeur
   afleiden uit de nieuwste passende meterregel; geef alleen expliciete defaults als de
   administratie geen eerdere passende regel heeft.
4. Toon de volledige preview: standen, kWh, tariefbron, bedragen, actie, verzenddatum,
   duplicaten en merge-waarschuwingen.
5. Wacht op expliciet akkoord en voer daarna uit met execute_approved_action.
6. Rapporteer de automatische verificatie: totaal, status, factuurdatum en sent_at per klant."""


def register_guidance(mcp) -> None:
    """Register the playbook resource and scenario prompts on the given FastMCP instance."""
    mcp.resource(
        PLAYBOOK_URI,
        name="boekhoud_playbook",
        description=(
            "Diep naslagwerk voor boekhoudtaken: gouden regels, btw, privé/zakelijk, "
            "categoriseren, consistentie-checklist en scenario-recepten. Lees dit bij de "
            "start van een boekhoudtaak."
        ),
        mime_type="text/markdown",
        tags={"boekhouding"},
    )(load_playbook)

    mcp.prompt(
        name="aan_de_slag",
        description="Eerste kennismaking: wat kan deze assistent met je boekhouding, hoe werkt het akkoord-mechanisme, en vijf dingen om als eerste te vragen.",
        tags={"boekhouding", "onboarding"},
    )(prompt_aan_de_slag)

    mcp.prompt(
        name="koppel_banktransacties",
        description="Koppel onverwerkte bankmutaties; stel een exact passende groep als één actie voor en voer die met één akkoord inclusief factuurverwerking uit.",
        tags={"boekhouding", "bank"},
    )(prompt_koppel_banktransacties)

    mcp.prompt(
        name="verwerk_achterstand",
        description="Werk achterstallige boekhouding weg: inventariseer, categoriseer en verwerk consistent (met akkoord per batch).",
        tags={"boekhouding"},
    )(prompt_verwerk_achterstand)

    mcp.prompt(
        name="categoriseer_heel_jaar",
        description="Categoriseer een volledig boekjaar, kwartaal voor kwartaal en onderling consistent.",
        tags={"boekhouding"},
    )(prompt_categoriseer_heel_jaar)

    mcp.prompt(
        name="leg_cijfers_uit",
        description="Lees de winst-en-verlies en balans en leg de cijfers uit in begrijpelijke taal (read-only).",
        tags={"boekhouding"},
    )(prompt_leg_cijfers_uit)

    mcp.prompt(
        name="diagnose_bankmutatie",
        description="Zoek uit waarom een bankmutatie niet automatisch is gekoppeld aan een categorie of document (let op: boekingsregels zijn niet via de API te lezen).",
        tags={"boekhouding"},
    )(prompt_diagnose_bankmutatie)

    mcp.prompt(
        name="factureer_meterverbruik",
        description="Bereken en factureer meterverbruik in één gecontroleerde batch met tariefhergebruik, uitzonderingen en planning.",
        tags={"boekhouding", "facturatie"},
    )(prompt_factureer_meterverbruik)
