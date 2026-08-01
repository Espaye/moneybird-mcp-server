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

## Installeren met `pip`

```bash
python -m pip install --upgrade moneybird-mcp
```

Start de lokale stdio-server, die standaard in alleen-lezenmodus begint:

```bash
moneybird-mcp
```

Het consolecommando communiceert via stdio. Normaal gesproken start de MCP-client dit proces. Het rechtstreeks uitvoeren in een gewone terminal is vooral nuttig om de configuratie te controleren of `--help` te bekijken.

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

Lokale en geauthenticeerde modi voor één gebruiker kunnen de OAuth-autorisatiecodeflow van Moneybird gebruiken wanneer `MONEYBIRD_ACCESS_TOKEN` ontbreekt.

1. Registreer een applicatie bij Moneybird.
2. Configureer `MONEYBIRD_OAUTH_CLIENT_ID` en `MONEYBIRD_OAUTH_CLIENT_SECRET`.
3. Voer het volgende uit:

```bash
python scripts/oauth_login.py --env-file /absolute/path/operator.env
```

De helper bewaart OAuth-tokens in de gegevensmap van Moneybird MCP. De huidige lokale helper vraagt de scopes aan die in de repository zijn gedocumenteerd; controleer deze voordat je toestemming geeft. Een gehoste productiedienst vereist een afzonderlijke HTTPS-callback, gebruikersidentiteit, opslag voor toestemmingen, een intrekkingsontwerp en een scheiding tussen administraties.

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
