# Instructions for AI agents

This repository is `moneybird-mcp`: a standalone MCP server for Moneybird, published on PyPI and
source-available under MIT + Commons Clause (never call it "open source").

## Hard rules

1. Never commit secrets, customer data, local paths or personal details.
2. Never merge, publish a package, write to a live Moneybird administration, or change
   credentials without the owner's explicit permission.
3. Work on a new branch from `origin/main` and open one focused pull request.
4. Keep the safety guarantees intact: read-only by default, writes only through
   `prepare_*` → approval → `execute_approved_action` → verification, no blind retry of a write,
   no fallback to local credentials in request-context mode.
5. The package must keep working with no extension installed.

## Where to look

| Change | Read first |
|---|---|
| Any change | [Documentation map](docs/README.md), then [architecture](docs/architecture.md) for the files involved |
| A tool | `moneybird_mcp/tools/`, `write_contracts.py`, [tool reference](docs/tool-reference.md) |
| A write or approval | `safety.py`, [deployment and safety](docs/deployment-and-safety.md), [threat model](docs/threat_model.md) |
| Open tasks | [Roadmap](docs/roadmap.md) |
| A release | [Releasing](docs/releasing.md) |

## Checks to run

| You changed | Run |
|---|---|
| Documentation | `python scripts/check_docs.py` |
| Python code | `ruff check moneybird_mcp scripts tests moneybird_mcp_server.py` and the affected tests (`python -m pytest -q tests/<file>`) |
| Shared safety, client or lifecycle code | the full suite: `python -m pytest -q` |
| Workflow metadata | `python scripts/render_workflow_catalogue.py --check` |
| Dependency bounds | the tests with `-c requirements-minimum.txt` installed |

No real Moneybird credential is needed for any test. CI must stay green.

## Documentation rules

- Write each fact in one place and link to it. The version lives only in `pyproject.toml`.
- Claim only what the code enforces and the tests prove.
- Keep the English and Dutch README in step.
