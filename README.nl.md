# Moneybird MCP

**Taal:** [English](README.md) · **Nederlands**

[![PyPI](https://img.shields.io/pypi/v/moneybird-mcp.svg)](https://pypi.org/project/moneybird-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/moneybird-mcp.svg)](https://pypi.org/project/moneybird-mcp/)
[![CI](https://github.com/Espaye/moneybird-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/Espaye/moneybird-mcp-server/actions/workflows/ci.yml)

> **Onofficiële community-integratie.** Dit project is niet ontwikkeld, goedgekeurd, ondersteund of gecontroleerd door Moneybird B.V.
>
> **Bèta 0.6.1.** De ondersteunde opstelling is een lokale MCP-server via stdio. De server start technisch afgedwongen in alleen-lezenmodus. Experimentele schrijfacties vereisen een expliciete lokale inschakeling en gecontroleerde goedkeuring.

Gebruik Claude, ChatGPT, Cursor of een andere MCP-client om een Moneybird-administratie te doorzoeken en ermee te werken. De server kan contacten, facturen, documenten, bankmutaties, rapporten en lokaal geïndexeerde boekhoudgegevens lezen.

## Aan de slag

Je hebt Python 3.11 of nieuwer, een MCP-client en een nieuw [Moneybird API-token](https://developer.moneybird.com/authentication) nodig.

### Aanbevolen: uitvoeren met `uvx`

Voeg deze serverconfiguratie toe aan je MCP-client:

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

Herstart de client en vraag deze om de beschikbare Moneybird-administraties te tonen.

`MONEYBIRD_ADMINISTRATION_ID` is optioneel wanneer het token maar toegang heeft tot één administratie. Plak een echt Moneybird-token nooit in een chat, issue, logbestand of bestand dat je commit.

Registraties in Claude Code hebben een scope. De standaardscope `local` is alleen beschikbaar in het huidige project; gebruik `--scope user` wanneer Moneybird vanuit elk project beschikbaar moet zijn. Als `claude mcp list` verbonden meldt maar een ander project geen tools toont, controleer dan `claude mcp get moneybird` en voeg de configuratie opnieuw toe met userscope. Gebruik geen projectscope voor een configuratie met een persoonlijk token, omdat projectscope een gedeeld `.mcp.json`-bestand schrijft.

### Installeren met `pip`

```bash
python -m pip install --upgrade moneybird-mcp
moneybird-mcp
```

Voor het lezen van PDF-bijlagen:

```bash
python -m pip install --upgrade "moneybird-mcp[pdf]"
```

Pakketpagina: [moneybird-mcp op PyPI](https://pypi.org/project/moneybird-mcp/)

Sluit op Windows iedere MCP-client die `moneybird-mcp` uitvoert voordat je met `pip` installeert of bijwerkt; Windows kan het vergrendelde consoleprogramma niet vervangen. Meldt `pip` `WinError 32`, houd de client dan gesloten en voer de installatieopdracht opnieuw uit om de gedeeltelijke installatie te herstellen. De aanbevolen `uvx`-opstelling voorkomt dat dit gebruikte consoleprogramma tijdens een upgrade wordt vervangen.

## Bijwerken

Met `pip`:

```bash
python -m pip install --upgrade moneybird-mcp
```

Sluit op Windows eerst de MCP-client. Als een eerdere poging met `WinError 32` mislukte, voer dan dezelfde opdracht opnieuw uit terwijl de client gesloten blijft.

Om `uvx` de pakketmetadata opnieuw te laten ophalen:

```bash
uvx --refresh-package moneybird-mcp moneybird-mcp
```

Bekijk het geïnstalleerde commando en de beschikbare opties:

```bash
moneybird-mcp --help
```

## Wat de server kan

- Contacten, verkoopfacturen, inkoopfacturen, bonnen, memoriaalboekingen en bankmutaties doorzoeken.
- Moneybird-rapporten lezen, waaronder winst-en-verliesrekening, balans, grootboek, btw-, debiteuren- en crediteurenrapporten.
- Inkoopfacturen, factuurverzendinstellingen, bankmutaties en boekhoudkundige inconsistenties controleren.
- PDF-bijlagen lokaal lezen wanneer de optionele PDF-afhankelijkheid is geïnstalleerd.
- Een lokale zoekindex bouwen voor snellere gerangschikte zoekresultaten.
- Beveiligde voorbeelden van schrijfacties voorbereiden wanneer schrijfacties expliciet zijn ingeschakeld.

De server gebruikt standaard compacte Tool Search. Daardoor hoeft een MCP-client niet bij het starten elk toolschema te laden. Bekijk het [tooloverzicht](docs/tool-reference.md) en de [dekking van de Moneybird API](docs/moneybird_api_coverage.md).

## Alleen-lezenmodus en schrijfacties

De server start technisch afgedwongen in alleen-lezenmodus. Hiervoor is geen instelling nodig:

```text
MONEYBIRD_CAPABILITY_MODE=read_only
```

Experimentele schrijfacties zijn alleen beschikbaar in lokale of geauthenticeerde opstellingen voor één gebruiker:

```text
MONEYBIRD_CAPABILITY_MODE=write_enabled
```

Schrijfacties gebruiken duurzame voorbereidings- en uitvoeringsgoedkeuringen met actiespecifieke verificatie. Dit zijn veiligheidsmaatregelen, maar geen onafhankelijk bewijs dat een mens de actie heeft goedgekeurd. Laat bevestiging voor destructieve tools ingeschakeld in de MCP-client en controleer ieder voorbeeld zorgvuldig.

## Configuratie

De belangrijkste instellingen zijn:

| Instelling | Standaard | Doel |
|---|---|---|
| `MONEYBIRD_ACCESS_TOKEN` | geen | Persoonlijk Moneybird API-token |
| `MONEYBIRD_ADMINISTRATION_ID` | automatisch wanneer eenduidig | Te gebruiken administratie |
| `MONEYBIRD_CAPABILITY_MODE` | `read_only` | `read_only` of `write_enabled` |
| `MONEYBIRD_MCP_DATA_DIR` | `~/.moneybird-mcp` voor geïnstalleerde stdio | Lokale goedkeuringen, auditgegevens, OAuth-gegevens en zoekstatus |
| `MCP_TOOL_DISCOVERY` | `search` | Compacte ontdekking; gebruik `full` voor oudere clients |
| `MCP_TRANSPORT` | `stdio` | `stdio`, `http` of legacy-`sse` |

Het pakket zoekt nooit automatisch naar `.env`-bestanden. Gebruik het omgevingsblok van de MCP-client of selecteer expliciet een bestand:

```bash
moneybird-mcp --env-file /absolute/path/moneybird-mcp.env
```

Bekijk [Aan de slag](docs/getting-started.nl.md) voor volledige installatievoorbeelden.

## Grens van de ondersteunde inzet

| Modus | Bedoeld gebruik | Status |
|---|---|---|
| Lokale stdio | Eén gebruiker op één computer | Ondersteunde standaard |
| Geauthenticeerde HTTP/SSE | Eén vertrouwde gebruiker achter authenticatie en TLS | Experimenteel |
| Gehoste dienst voor meerdere gebruikers | Meerdere gebruikers of organisaties | Niet geïmplementeerd |

Iedere HTTP/SSE-listener vereist `MCP_AUTH_TOKEN`, ook op loopback. Niet-loopbacklisteners worden geweigerd tenzij expliciet een vertrouwde TLS-proxy is geconfigureerd. De meegeleverde gateway is een demonstratie en geen gehost product voor productiegebruik.

Bekijk [Deployment and safety](docs/deployment-and-safety.md), het [beveiligingsbeleid](SECURITY.md) en het [dreigingsmodel](docs/threat_model.md).

## Lokale gegevens

Geïnstalleerde stdio-uitvoeringen bewaren lokale status standaard in `~/.moneybird-mcp`, tenzij `MONEYBIRD_MCP_DATA_DIR` is ingesteld. Dit kan onder meer bevatten:

- OAuth-toegangs- en vernieuwingstokens;
- de SQLite-database met goedkeuringen;
- auditlogboeken per administratie;
- zoekindexen en caches;
- lokaal bewaarde, privacyvriendelijke telemetrie.

Deze bestanden worden door dit project niet versleuteld. Beperk de toegang tot de map en lees [Levenscyclus van lokale gegevens](docs/data-lifecycle.nl.md) voordat je deze gegevens back-upt of verwijdert.

## Documentatie

- [Aan de slag](docs/getting-started.nl.md)
- [Tooloverzicht](docs/tool-reference.md)
- [Deployment and safety](docs/deployment-and-safety.md)
- [Levenscyclus van lokale gegevens](docs/data-lifecycle.nl.md)
- [Beveiligingsbeleid](SECURITY.md)
- [Ondersteuning](SUPPORT.md)
- [Bijdragen](CONTRIBUTING.md)
- [Wijzigingslogboek](CHANGELOG.md)
- [Dekking van de Moneybird API](docs/moneybird_api_coverage.md)
- [Releaseproces](docs/releasing.md)

De technische referentie-, beveiligings-, ontwikkel- en releasedocumentatie blijft voorlopig Engelstalig.

## Ondersteuning en status

Dit is een communityproject vóór versie 1.0. Er is geen gegarandeerde reactietijd, beschikbaarheid, gegevensherstel, boekhoudkundige juistheid of belastingadvies.

Gebruik [GitHub Issues](https://github.com/Espaye/moneybird-mcp-server/issues) voor reproduceerbare fouten en functieverzoeken zonder geheimen of klantgegevens. Meld kwetsbaarheden privé volgens [SECURITY.md](SECURITY.md).

## Licentie

Dit project is **source-available en geen OSI-goedgekeurde open source**. Het wordt verspreid onder de MIT License met de **Commons Clause License Condition v1.0**.

Persoonlijk gebruik, intern gebruik binnen een organisatie, inspectie en aanpassing zijn toegestaan. Voor het verkopen van de software, het aanbieden van een betaalde gehoste dienst die er in belangrijke mate op is gebaseerd, of commerciële herverpakking is een afzonderlijke commerciële licentie nodig. Neem voor commerciële licenties contact op met de repository-eigenaar via [GitHub Issues](https://github.com/Espaye/moneybird-mcp-server/issues). De volledige voorwaarden in [LICENSE](LICENSE) zijn bepalend.
