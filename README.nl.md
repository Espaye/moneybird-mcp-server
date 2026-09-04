# Moneybird MCP

**Taal:** [English](README.md) · **Nederlands**

[![PyPI](https://img.shields.io/pypi/v/moneybird-mcp.svg)](https://pypi.org/project/moneybird-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/moneybird-mcp.svg)](https://pypi.org/project/moneybird-mcp/)
[![CI](https://github.com/Espaye/moneybird-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/Espaye/moneybird-mcp-server/actions/workflows/ci.yml)

> **Onofficiële community-integratie.** Dit project is niet ontwikkeld, goedgekeurd, ondersteund of gecontroleerd door Moneybird B.V.
>
> **Bèta 0.8.1.** De ondersteunde opstelling is een lokale MCP-server via stdio. De server start technisch afgedwongen in alleen-lezenmodus. Experimentele schrijfacties vereisen een expliciete lokale inschakeling en gecontroleerde goedkeuring.

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

Een persoonlijk API-token is de eenvoudige, ondersteunde manier om dit lokaal te draaien: één inloggegeven dat je zelf al beheert, zonder applicatieregistratie.

### Optioneel: OAuth met je eigen geregistreerde applicatie

Voor ontwikkeling, of voor self-hosters die refresh-tokens en scoped toegang willen, kan de server verbinden via de OAuth-flow van Moneybird met **een OAuth-applicatie die je zelf registreert**:

```bash
moneybird-mcp auth login --env-file /absolute/path/moneybird-mcp.env
moneybird-mcp auth status
moneybird-mcp auth logout
```

Dit is niet de standaardopstelling voor publiek gebruik, en het pakket bevat geen gedeelde applicatiegegevens. Een OAuth Client Secret authenticeert de *applicatie*, niet de gebruiker, en kan dus niet worden meegeleverd in een installeerbaar pakket — wat aan iedereen wordt gedistribueerd is geen geheim. Lokale OAuth betekent daarom je eigen Client ID en Client Secret meebrengen.

Bestaan er zowel een persoonlijk token als een OAuth-verbinding, dan wint het persoonlijke token; `moneybird-mcp auth status` vertelt welke actief is. Volledige uitleg: [Moneybird OAuth](docs/oauth.md) (Engelstalig).

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
- Onverwerkte banktransacties koppelen aan de openstaande facturen die ze betalen, met per kandidaat de onderbouwing, en het eerlijk melden wanneer twee kandidaten even goed passen.
- Moneybird-rapporten lezen, waaronder winst-en-verliesrekening, balans, grootboek, btw-, debiteuren- en crediteurenrapporten.
- Inkoopfacturen, factuurverzendinstellingen, bankmutaties en boekhoudkundige inconsistenties controleren.
- Eén inkoopfactuur met een exact passende groep bankmutaties afwikkelen via één voorvertoning en akkoord, inclusief factuurverwerking en eindcontrole.
- Productgegevens controleren en beveiligde bulkprijswijzigingen met exacte decimale voorbeelden berekenen.
- PDF-bijlagen lokaal lezen wanneer de optionele PDF-afhankelijkheid is geïnstalleerd.
- Een lokale zoekindex bouwen voor snellere gerangschikte zoekresultaten.
- Het boekhoud-playbook per onderwerp lezen (btw, btw-afwikkeling, bankmutaties, categoriseren, consistentie) als tool, niet alleen als MCP-resource.
- Beveiligde voorbeelden van schrijfacties voorbereiden wanneer schrijfacties expliciet zijn ingeschakeld.

De server biedt standaard de volledige toolcatalogus aan: toolschema's staan in de gecachete promptprefix van de client, dus ze opsommen is goedkoop, terwijl ze pas op aanvraag ontdekken elke taak een extra modelronde kost. Clients die de volledige lijst niet aankunnen draaien `--tool-discovery search` voor compacte Tool Search. Bekijk het [tooloverzicht](docs/tool-reference.md) en de [dekking van de Moneybird API](docs/moneybird_api_coverage.md).

Gebruik `list_supported_workflows` om de kleine set uitkomsten te vinden die van begin tot eind geïntegreerd en getest zijn. De gegenereerde [workflowcatalogus](docs/workflow-catalogue.md) vermeldt risico, modus, versie, voorwaarden, verificatie en beperkingen. De producttools voeren zelf hun concrete administratie- en recordpreflight uit.

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
| `MONEYBIRD_ACCESS_TOKEN` | geen | Persoonlijk Moneybird API-token; gaat voor op een OAuth-verbinding |
| `MONEYBIRD_ADMINISTRATION_ID` | de bij OAuth-login gekozen administratie, anders automatisch wanneer eenduidig | Te gebruiken administratie |
| `MONEYBIRD_OAUTH_CLIENT_ID` / `_SECRET` | geen | Je eigen geregistreerde OAuth-applicatie, voor `auth login` |
| `MONEYBIRD_OAUTH_SCOPES` | `full` | Scopeprofiel of expliciete lijst die bij login wordt aangevraagd |
| `MONEYBIRD_OAUTH_PROFILE` | `default` | Welke opgeslagen OAuth-verbinding deze server gebruikt; `auth login --profile` schrijft hem |
| `MONEYBIRD_CAPABILITY_MODE` | `read_only` | `read_only` of `write_enabled` |
| `MONEYBIRD_MCP_DATA_DIR` | `~/.moneybird-mcp` voor het geïnstalleerde commando | Lokale goedkeuringen, auditgegevens, OAuth-gegevens en zoekstatus |
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

Iedere HTTP/SSE-listener vereist `MCP_AUTH_TOKEN`, ook op loopback. Niet-loopbacklisteners worden geweigerd tenzij expliciet een vertrouwde TLS-proxy is geconfigureerd. Het netwerktransport is bedoeld voor één vertrouwde gebruiker en biedt geen identiteit voor meerdere gebruikers of tenantisolatie.

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
- [Connecting through Moneybird OAuth](docs/oauth.md)
- [Tooloverzicht](docs/tool-reference.md)
- [Deployment and safety](docs/deployment-and-safety.md)
- [Levenscyclus van lokale gegevens](docs/data-lifecycle.nl.md)
- [Beveiligingsbeleid](SECURITY.md)
- [Ondersteuning](SUPPORT.md)
- [Bijdragen](CONTRIBUTING.md)
- [Wijzigingslogboek](CHANGELOG.md)
- [Dekking van de Moneybird API](docs/moneybird_api_coverage.md)
- [Releaseproces](docs/releasing.md)
- [Roadmap](docs/roadmap.md)

De technische referentie-, beveiligings-, ontwikkel- en releasedocumentatie blijft voorlopig Engelstalig.

## Ondersteuning en status

Dit is een communityproject vóór versie 1.0. Er is geen gegarandeerde reactietijd, beschikbaarheid, gegevensherstel, boekhoudkundige juistheid of belastingadvies.

Gebruik [GitHub Issues](https://github.com/Espaye/moneybird-mcp-server/issues) voor reproduceerbare fouten en functieverzoeken zonder geheimen of klantgegevens. Meld kwetsbaarheden privé volgens [SECURITY.md](SECURITY.md).

## Licentie

Dit project is **source-available en geen OSI-goedgekeurde open source**. Het wordt verspreid onder de MIT License met de **Commons Clause License Condition v1.0**.

Persoonlijk gebruik, intern gebruik binnen een organisatie, inspectie en aanpassing zijn toegestaan. Voor het verkopen van de software, het commercieel aanbieden van de functionaliteit als dienst, of commerciële herverpakking is een afzonderlijke commerciële licentie nodig. Neem voor commerciële licenties contact op met de repository-eigenaar via [GitHub Issues](https://github.com/Espaye/moneybird-mcp-server/issues). De volledige voorwaarden in [LICENSE](LICENSE) zijn bepalend.
