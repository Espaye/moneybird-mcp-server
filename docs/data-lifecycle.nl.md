# Levenscyclus van lokale gegevens

**Taal:** [English](data-lifecycle.md) · **Nederlands**

Moneybird MCP bewaart lokale operationele status. Deze pagina beschrijft het huidige gedrag; speciale subcommando's zoals `moneybird-mcp data ...` zijn nog niet geïmplementeerd.

## Standaardlocatie

Voor het geïnstalleerde stdio-commando is de standaardmap:

```text
~/.moneybird-mcp
```

Op Windows verwijst dit normaal gesproken naar:

```text
%USERPROFILE%\.moneybird-mcp
```

Stel een andere locatie in met:

```text
MONEYBIRD_MCP_DATA_DIR=/absolute/private/path
```

Het verouderde startpunt vanuit een repository-clone kan om compatibiliteitsredenen nog de werkmap gebruiken. Gebruik bij voorkeur expliciet een afgeschermde gegevensmap.

## Welke gegevens kunnen worden opgeslagen

Afhankelijk van de ingeschakelde functies kan de map het volgende bevatten:

| Gegevens | Doel | Gevoeligheid |
|---|---|---|
| `moneybird_approvals.sqlite3` | Voorbereide schrijfacties, uitvoeringsclaims, resultaten en bewijs voor reconciliatie | Hoog |
| `.moneybird_audit_log_<administration>.jsonl` | Auditexport van schrijfacties per administratie | Hoog |
| OAuth-tokenopslag | Moneybird-toegangs- en vernieuwingstokens | Kritiek |
| Sync-JSON- en SQLite FTS-bestanden | Lokale doorzoekbare kopieën van boekhoudgegevens | Hoog |
| Telemetriestatus | Begrensde lokale prestatie- en foutaggregaten | Gemiddeld |

De precieze namen van cachebestanden kunnen veranderen. Behandel de volledige map als gevoelige financiële gegevens.

Het project probeert op POSIX-systemen privébestandsrechten toe te passen. Op Windows en sommige gekoppelde bestandssystemen moet de beheerder zelf de map-ACL's instellen. Bestanden worden niet door Moneybird MCP versleuteld.

## Bewaartermijnen

Er is geen automatische gehoste bewaar- of verwijderdienst.

- Openstaande goedkeuringen verlopen normaal gesproken na 15 minuten.
- Geclaimde, gedeeltelijke, onduidelijke en niet-geverifieerde resultaten van schrijfacties blijven bewaard voor reconciliatie.
- Auditlogboeken en lokale indexen blijven bestaan totdat de beheerder deze verwijdert.
- OAuth-tokens blijven bestaan totdat ze worden verwijderd of ingetrokken.

## Back-up maken

Stop de MCP-server voordat je de map kopieert. Zo veranderen de SQLite-database met goedkeuringen en de zoekbestanden niet tijdens de back-up.

Een back-up kan actieve inloggegevens en boekhoudgegevens van klanten bevatten. Versleutel de back-up, beperk de toegang en stel bewust een bewaartermijn vast.

## Een alleen-lezeninstallatie opnieuw instellen

Wanneer de installatie nooit `write_enabled` heeft gebruikt, bestaat een volledige lokale reset doorgaans uit:

1. stop alle Moneybird MCP-processen;
2. trek het Moneybird-token in wanneer het niet meer wordt gebruikt;
3. maak een back-up van gegevens die behouden moeten blijven;
4. verwijder de geconfigureerde gegevensmap;
5. herstart de client en configureer zo nodig de inloggegevens opnieuw.

Door de map te verwijderen verdwijnen lokale indexen en OAuth-status. Hiermee worden geen gegevens uit Moneybird verwijderd.

## Een installatie die schrijfacties heeft gebruikt opnieuw instellen

Verwijder de database met goedkeuringen en de auditlogboeken niet zolang een actie geclaimd, gedeeltelijk uitgevoerd, onduidelijk of nog niet geverifieerd is.

Controleer eerst onopgeloste uitvoeringen:

```bash
python scripts/reconcile_execution.py --administration-id <id> list
```

Gebruik de helptekst van het script voor de exact ondersteunde acties:

```bash
python scripts/reconcile_execution.py --help
```

Los ieder geval op met onafhankelijk bewijs uit Moneybird. De reconciliatie-CLI vereist bewust expliciete beslissingen die door bewijs worden ondersteund.

Nadat onopgelost werk is afgehandeld en vereiste gegevens zijn geëxporteerd, stop je de server en verwijder je de gegevensmap.

## Alleen zoekstatus verwijderen

Stop de server voordat je cache- of FTS-bestanden verwijdert. Bewaar de database met goedkeuringen, auditlogboeken en OAuth-opslag, tenzij je deze bewust ook wilt verwijderen.

Omdat de precieze namen van cachebestanden implementatiedetails zijn, controleer je de geselecteerde gegevensmap en ieder bestand aan de hand van de huidige versie voordat je iets verwijdert. Een latere synchronisatie bouwt verwijderde zoekstatus opnieuw op vanuit Moneybird.

## OAuth-inloggegevens verwijderen

Stop de server, verwijder het lokale OAuth-tokenbestand uit de gegevensmap en trek indien van toepassing de applicatietoestemming in Moneybird in.

Wanneer `MONEYBIRD_ACCESS_TOKEN` in de procesomgeving of een expliciet omgevingsbestand staat, wordt die afzonderlijke inlogmethode niet verwijderd door alleen de OAuth-opslag te verwijderen.

## Het pakket verwijderen

```bash
python -m pip uninstall moneybird-mcp
```

Het verwijderen van het pakket verwijdert de gegevensmap niet. Verwijder lokale status afzonderlijk nadat je de bovenstaande controles hebt uitgevoerd.

## Geplande richting voor de CLI

Een toekomstige lifecycle-CLI zou veilig commando's kunnen aanbieden zoals:

```text
moneybird-mcp data status
moneybird-mcp data export
moneybird-mcp data purge-search
moneybird-mcp approvals list
moneybird-mcp approvals reconcile
```

Dit zijn ontwerpdoelen en geen huidige commando's. Een implementatie moet destructief opschonen weigeren zolang onopgeloste schrijfacties bestaan en mag standaard nooit inloggegevens of ruwe boekhoudgegevens afdrukken.
