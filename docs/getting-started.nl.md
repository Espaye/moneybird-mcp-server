# Aan de slag

**Taal:** [English](getting-started.md) · **Nederlands**

Deze handleiding beschrijft de ondersteunde lokale installatie van Moneybird MCP.

> Dit is een onofficiële community-integratie en is niet ontwikkeld, goedgekeurd, ondersteund of gecontroleerd door Moneybird B.V.

## Vereisten

- Python 3.11 of nieuwer
- een MCP-client, zoals Claude Desktop, Claude Code, Cursor of een andere compatibele client
- een Moneybird-administratie
- een nieuw Moneybird API-token

Maak en beheer tokens via Moneybird. Behandel een persoonlijk API-token als een wachtwoord: plak het niet in een chat, GitHub-issue, schermafbeelding, logbestand of gecommit configuratiebestand.

Trek een token eerst in wanneer het al openbaar is gemaakt.

## Aanbevolen installatie met `uvx`

Installeer [uv](https://docs.astral.sh/uv/) en voeg deze configuratie toe aan de MCP-client:

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

Herstart de client. Een nuttige eerste opdracht is:

```text
Toon de Moneybird-administraties die beschikbaar zijn voor deze verbinding en toon daarna de serverstatus.
```

Laat `MONEYBIRD_ADMINISTRATION_ID` weg wanneer het token maar één administratie kan bereiken. Wanneer het token meerdere administraties kan bereiken, stel je na het opvragen daarvan het exacte administratie-ID in.

Claude Code bewaart MCP-registraties met een scope. De standaardscope `local` geldt alleen voor het huidige project. Gebruik `--scope user` voor een privéconfiguratie die in elk project beschikbaar moet zijn. Projectscope schrijft een gedeeld `.mcp.json`-bestand en mag geen persoonlijk Moneybird-token bevatten. Zijn de tools in een andere map afwezig terwijl de server verbonden is, controleer de scope dan met `claude mcp get moneybird`.

## Installeren met `pip`

```bash
python -m pip install --upgrade moneybird-mcp
```

Start de lokale stdio-server, die standaard in alleen-lezenmodus begint:

```bash
moneybird-mcp
```

Het consolecommando communiceert via stdio. Normaal gesproken start de MCP-client dit proces. Het rechtstreeks uitvoeren in een gewone terminal is vooral nuttig om de configuratie te controleren of `--help` te bekijken.

Sluit op Windows iedere MCP-client die dit commando uitvoert vóór een installatie of upgrade met `pip`. Anders kan het vergrendelde `moneybird-mcp.exe` ervoor zorgen dat `pip` met `WinError 32` stopt nadat een deel van de oude installatie al is verwijderd. Houd de client gesloten en voer de installatieopdracht opnieuw uit om dit te herstellen. De aanbevolen `uvx`-opstelling vervangt dit gebruikte consoleprogramma niet tijdens een upgrade.

## Optionele PDF-ondersteuning

Het lezen van PDF-bijlagen is bewust optioneel:

```bash
python -m pip install --upgrade "moneybird-mcp[pdf]"
```

Zonder deze extra afhankelijkheid blijft de rest van de server werken. De bijlagetool meldt dan dat PDF-ondersteuning ontbreekt.

## Expliciet omgevingsbestand

Het pakket laadt nooit automatisch een `.env`-bestand uit de werkmap. Daardoor kan een niet-vertrouwde startmap niet ongemerkt de inloggegevens, administratiekeuze, capability-modus of het netwerkbeleid aanpassen.

Een expliciet geselecteerd bestand kan het volgende bevatten:

```env
MONEYBIRD_ACCESS_TOKEN=your-token-here
MONEYBIRD_ADMINISTRATION_ID=
MONEYBIRD_CAPABILITY_MODE=read_only
MONEYBIRD_MCP_DATA_DIR=
MCP_TOOL_DISCOVERY=search
```

Selecteer het met een absoluut pad:

```bash
moneybird-mcp --env-file /absolute/path/moneybird-mcp.env
```

Waarden die al in de omgeving van het bovenliggende proces aanwezig zijn, hebben voorrang op waarden in het bestand. De capability-modus blijft `read_only`, tenzij een uiteindelijke waarde deze expliciet op `write_enabled` zet.

## Bijwerken

Met `pip`:

```bash
python -m pip install --upgrade moneybird-mcp
```

Sluit op Windows eerst de MCP-client. Als een eerdere poging `WinError 32` meldde, voer de opdracht dan opnieuw uit terwijl de client gesloten blijft.

Laat `uvx` indien nodig het pakket opnieuw ophalen:

```bash
uvx --refresh-package moneybird-mcp moneybird-mcp
```

Bekijk de beschikbare opties:

```bash
moneybird-mcp --help
```

## Verwijderen

Voor een installatie met `pip`:

```bash
python -m pip uninstall moneybird-mcp
```

Het verwijderen van het pakket verwijdert `~/.moneybird-mcp` niet. Die map kan OAuth-inloggegevens, goedkeuringen, auditgeschiedenis en zoekstatus bevatten. Bekijk [Levenscyclus van lokale gegevens](data-lifecycle.nl.md).

## OAuth in plaats van een persoonlijk token

Aanbevolen. Lokale en geauthenticeerde modi voor één gebruiker kunnen verbinden via de OAuth-autorisatiecodeflow van Moneybird, zodat je nooit zelf een Moneybird-token hoeft over te typen.

1. Registreer een **externe applicatie** op <https://moneybird.com/user/applications/new> met redirect-URI `urn:ietf:wg:oauth:2.0:oob`.
2. Zet `MONEYBIRD_OAUTH_CLIENT_ID` en `MONEYBIRD_OAUTH_CLIENT_SECRET` in de omgeving of in een expliciet gekozen bestand. Dit zijn applicatiegegevens, geen tokens; behandel het secret als een wachtwoord en commit het nooit.
3. Voer het volgende uit:

```bash
moneybird-mcp auth login --env-file /absolute/path/operator.env
```

4. Open de getoonde autorisatie-URL, keur de applicatie goed en plak **alleen** de korte autorisatiecode die Moneybird laat zien.
5. Het commando verifieert de verbinding, kiest de administratie (en vraagt het als er meerdere zijn) en bewaart alles in de gegevensmap van Moneybird MCP. Start je MCP-client daarna gewoon.

Beheer de verbinding met `moneybird-mcp auth status` en `moneybird-mcp auth logout`. Geen van beide toont ooit een token of het client secret. `logout` verwijdert uitsluitend de lokale gegevens: Moneybird publiceert geen intrekkingsendpoint, dus toegang trek je in op <https://moneybird.com/user/applications>.

`python -m moneybird_mcp.oauth_login` blijft werken en is hetzelfde commando; in een clone is `python scripts/oauth_login.py` een gelijkwaardige wrapper.

Bekijk de aangevraagde scopes met `moneybird-mcp auth scopes` en beperk ze zo nodig met `--scopes`. De volledige uitleg, inclusief de onderbouwing per scope en de voorrangsregels, staat in [Connecting through Moneybird OAuth](oauth.md) (Engelstalig).

De out-of-band-redirect is een lokaal/ontwikkelmechanisme. Een gehoste productiedienst vereist een afzonderlijke HTTPS-callback, gebruikersidentiteit, opslag voor toestemmingen, een intrekkingsontwerp en een scheiding tussen administraties.

## Claude Desktop-extensie

Vanuit een clone van de repository:

```bash
python scripts/build_mcpb.py
```

Hiermee wordt een platformspecifiek `.mcpb`-pakket in `dist/` gemaakt. Het pakket bevat de afhankelijkheden, maar vereist nog steeds een compatibele systeeminstallatie van Python. De instellingen gebruiken standaard lokale inloggegevens en de alleen-lezencapability.

## Problemen oplossen

### Het commando wordt niet gevonden

Gebruik dezelfde Python-omgeving waarin het pakket is geïnstalleerd:

```bash
python -m pip show moneybird-mcp
python -m pip install --upgrade moneybird-mcp
```

Voor MCP-clients voorkomt `uvx` de meeste PATH-problemen.

### Er is meer dan één administratie beschikbaar

Roep `list_administrations` aan en stel daarna `MONEYBIRD_ADMINISTRATION_ID` in op het vereiste ID.

### Een bijlage kan niet worden gelezen

Installeer de PDF-extra en herstart de MCP-client:

```bash
python -m pip install --upgrade "moneybird-mcp[pdf]"
```

### Een schrijftool wordt geweigerd

Dat is het verwachte gedrag in de standaardmodus. Lees [Deployment and safety](deployment-and-safety.md) voordat je `write_enabled` overweegt.
