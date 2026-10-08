# Documentation map

[AGENTS](../AGENTS.md) → relevant [roadmap task](roadmap.md) → applicable
[architecture boundary](architecture.md) → affected code/tests.

| Need | Authority |
|---|---|
| Setup | [English](getting-started.md), [Dutch](getting-started.nl.md) |
| Scope and active work | [Roadmap](roadmap.md) |
| Package/API and safety boundaries | [Architecture](architecture.md), [deployment](deployment-and-safety.md), [threat model](threat_model.md) |
| Tools / workflows / API accounting | [Tools](tool-reference.md), [generated workflows](workflow-catalogue.md), [API snapshot coverage](moneybird_api_coverage.md) |
| OAuth and local state | [OAuth](oauth.md), [data handling](data_handling.md), [lifecycle](data-lifecycle.md) |
| Version and publication | `pyproject.toml` is source version; [changelog](../CHANGELOG.md) is released/unreleased history; [releasing](releasing.md) owns mandatory publication gates |

Use **implemented**, **offline-tested**, **published** and **live-verified** precisely. A commit/version
bump is not PyPI publication; a test proves only its named environment/contract. This documentation
covers this package alone and does not mirror external product plans or deployment queues.
`python scripts/check_docs.py` checks links/anchors and source facts offline without installing dependencies.
