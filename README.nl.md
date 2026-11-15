# Moneybird MCP

**Taal:** [English](README.md) · **Nederlands**

[![PyPI](https://img.shields.io/pypi/v/moneybird-mcp.svg)](https://pypi.org/project/moneybird-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/moneybird-mcp.svg)](https://pypi.org/project/moneybird-mcp/)
[![CI](https://github.com/Espaye/moneybird-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/Espaye/moneybird-mcp-server/actions/workflows/ci.yml)

Laat een AI-assistent (Claude, ChatGPT, Cursor of een andere MCP-client) je
[Moneybird](https://www.moneybird.nl/)-boekhouding lezen en ermee helpen.

> **Bèta 0.10.0.** Onofficieel: niet gemaakt, goedgekeurd of ondersteund door Moneybird B.V.
> De server start **alleen-lezen**: er verandert niets in Moneybird tenzij je schrijven zelf aanzet.

## Installeren in 3 stappen

1. Maak een [Moneybird API-token](https://developer.moneybird.com/authentication) en installeer
   [uv](https://docs.astral.sh/uv/) (vereist Python 3.11 of nieuwer).
2. Zet dit in de configuratie van je MCP-client:

   ```json
   {
     "mcpServers": {
       "moneybird": {
         "command": "uvx",
         "args": ["moneybird-mcp"],
         "env": { "MONEYBIRD_ACCESS_TOKEN": "your-token-here" }
       }
     }
   }
   ```

3. Herstart de client en vraag: *"Toon mijn Moneybird-administraties."*

Kan het token bij meer dan één administratie, stel dan ook `MONEYBIRD_ADMINISTRATION_ID` in.
Behandel het token als een wachtwoord: plak het nooit in een chat, issue, log of gecommit bestand.
Registreer de server in Claude Code met `--scope user`, zodat hij in elk project werkt; gebruik
nooit project-scope, want die schrijft het token in een gedeeld `.mcp.json`.

Meer opties (pip, pdf-ondersteuning, env-bestanden, OAuth, problemen oplossen):
[Aan de slag](docs/getting-started.nl.md).

## Wat de server kan

- Contacten, verkoop- en inkoopfacturen, bonnetjes, memoriaalboekingen en banktransacties zoeken en lezen.
- Rapporten lezen: winst-en-verlies, balans, grootboek, btw, debiteuren en crediteuren.
- Onverwerkte banktransacties koppelen aan de openstaande facturen die ze betalen, met onderbouwing.
- Inkoopfacturen controleren en inconsistenties in de boekhouding vinden.
- Producten controleren en bulkprijswijzigingen vooraf bekijken.
- Pdf-bijlagen lezen (installeer `moneybird-mcp[pdf]`).
- Wijzigingen in Moneybird doorvoeren, pas nadat je schrijven aanzet en elk voorstel goedkeurt.

Alle tools: [toolreferentie](docs/tool-reference.md) (Engels).

## Zo blijven wijzigingen veilig

- **Standaard alleen-lezen.** Schrijven vereist `MONEYBIRD_CAPABILITY_MODE=write_enabled`.
- **Elke wijziging gaat in twee stappen.** Een `prepare_*`-tool toont een voorstel; alleen
  `execute_approved_action` voert het uit, waarna het resultaat in Moneybird wordt gecontroleerd.
  De AI kan beide aanroepen, dus laat de bevestigingsvragen van je client aan en lees elk voorstel.
- **Lokale gegevens blijven op je computer** in `~/.moneybird-mcp`: goedkeuringen, auditlog,
  zoekindex en OAuth-tokens. Ze zijn niet versleuteld, dus houd de map privé. Verwijderen van het
  pakket wist de map niet.
- **Netwerkmodus (HTTP) is experimenteel**, bedoeld voor één vertrouwde gebruiker, en vereist
  `MCP_AUTH_TOKEN`.

Details: [inzet en veiligheid](docs/deployment-and-safety.md) (Engels).

## Instellingen

| Instelling | Standaard | Wat het doet |
|---|---|---|
| `MONEYBIRD_ACCESS_TOKEN` | geen | Je Moneybird API-token |
| `MONEYBIRD_ADMINISTRATION_ID` | automatisch als er maar één is | Welke administratie wordt gebruikt |
| `MONEYBIRD_CAPABILITY_MODE` | `read_only` | Zet op `write_enabled` om wijzigingen toe te staan |
| `MONEYBIRD_MCP_DATA_DIR` | `~/.moneybird-mcp` | Waar lokale gegevens staan |
| `MCP_TOOL_DISCOVERY` | `full` | `search` toont een kleine toolset voor clients die de volledige lijst niet aankunnen |
| `MCP_TRANSPORT` | `stdio` | `http` voor netwerkmodus |

`.env`-bestanden worden nooit automatisch geladen; geef er een mee met
`moneybird-mcp --env-file /absoluut/pad`. OAuth met een eigen geregistreerde app kan ook:
[OAuth](docs/oauth.md) (Engels).

## Voor AI-agents

- **De server gebruiken:** begin met `get_server_status` en `list_administrations`. Roep voor
  hoe-vragen `get_bookkeeping_guide(topic)` aan. Bij schrijven altijd: `prepare_*` → voorstel tonen
  → wachten op een expliciet "ja" van de gebruiker → `execute_approved_action` → de controle melden.
- **Deze repository aanpassen:** lees eerst [AGENTS.md](AGENTS.md).

## Documentatie

| Ik wil… | Lees |
|---|---|
| Installeren, bijwerken of problemen oplossen | [Aan de slag](docs/getting-started.nl.md) |
| Alle tools zien | [Toolreferentie](docs/tool-reference.md) |
| Schrijven, netwerkmodus en beveiliging begrijpen | [Inzet en veiligheid](docs/deployment-and-safety.md), [dreigingsmodel](docs/threat_model.md) |
| Lokale gegevens back-uppen of wissen | [Levenscyclus lokale gegevens](docs/data-lifecycle.nl.md) |
| Alle documentatie bekijken | [Documentatieoverzicht](docs/README.md) |
| Bijdragen | [Bijdragen](CONTRIBUTING.md), [changelog](CHANGELOG.md) |

## Ondersteuning

Een communityproject vóór 1.0: geen gegarandeerde reactietijd, beschikbaarheid of juiste
boekhouding, en geen belastingadvies. Meld bugs via [GitHub Issues](https://github.com/Espaye/moneybird-mcp-server/issues)
zonder tokens of klantgegevens; meld kwetsbaarheden privé volgens [SECURITY.md](SECURITY.md).

## Licentie

Dit project is **source-available en geen OSI-goedgekeurde open source**: de MIT License met de
**Commons Clause License Condition v1.0**. Persoonlijk en intern gebruik, inspectie en aanpassing
zijn vrij. Voor verkopen, commercieel aanbieden als dienst of commerciële herverpakking is een
afzonderlijke commerciële licentie nodig; neem voor commerciële licenties contact op met de
eigenaar via [GitHub Issues](https://github.com/Espaye/moneybird-mcp-server/issues). De volledige
voorwaarden in [LICENSE](LICENSE) zijn bepalend.
