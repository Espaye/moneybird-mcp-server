# Moneybird MCP

**Language:** **English** · [Nederlands](https://github.com/Espaye/moneybird-mcp-server/blob/main/README.nl.md)

[![PyPI](https://img.shields.io/pypi/v/moneybird-mcp.svg)](https://pypi.org/project/moneybird-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/moneybird-mcp.svg)](https://pypi.org/project/moneybird-mcp/)
[![CI](https://github.com/Espaye/moneybird-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/Espaye/moneybird-mcp-server/actions/workflows/ci.yml)

Let an AI assistant (Claude, ChatGPT, Cursor or any other MCP client) read and help with your
[Moneybird](https://www.moneybird.nl/) bookkeeping.

> **Beta 0.10.0.** Unofficial: not made, endorsed or supported by Moneybird B.V.
> It starts **read-only**: nothing in Moneybird changes unless you switch writes on yourself.

## Set up

1. Create a [Moneybird API token](https://developer.moneybird.com/authentication).
2. Ask your coding agent (Claude Code, Codex, Cursor, …):
   *"Install the Moneybird MCP server `moneybird-mcp` with my Moneybird API token."*
3. Restart the client and ask: *"List my Moneybird administrations."*

Installing by hand, PDF support, OAuth and troubleshooting:
[Getting started](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/getting-started.md).

## What it can do

- Search and read contacts, sales and purchase invoices, receipts, journals and bank transactions.
- Read reports: profit and loss, balance sheet, general ledger, VAT, debtors and creditors.
- Match unprocessed bank transactions to the open invoices they pay, with the evidence.
- Review purchase invoices and find bookkeeping inconsistencies.
- Audit products and preview bulk price changes.
- Read PDF attachments (install `moneybird-mcp[pdf]`).
- Make changes in Moneybird, only after you switch writes on and approve each preview.

Every tool: [tool reference](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/tool-reference.md).

## How changes stay safe

- **Read-only by default.** Writes need `MONEYBIRD_CAPABILITY_MODE=write_enabled`.
- **Every change has two steps.** A `prepare_*` tool shows a preview; only
  `execute_approved_action` applies it, after which the result is checked against Moneybird.
  The AI can call both, so keep your client's confirmation prompts on and read every preview.
- **Local data stays on your machine** in `~/.moneybird-mcp`: approvals, audit log, search index
  and OAuth tokens. It is not encrypted, and uninstalling does not delete it.
- **Network (HTTP) mode is experimental** and meant for one trusted user; it requires
  `MCP_AUTH_TOKEN`.

Details: [deployment and safety](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/deployment-and-safety.md).

## Settings

| Setting | Default | What it does |
|---|---|---|
| `MONEYBIRD_ACCESS_TOKEN` | none | Your Moneybird API token |
| `MONEYBIRD_ADMINISTRATION_ID` | automatic if there is only one | Which administration to use |
| `MONEYBIRD_CAPABILITY_MODE` | `read_only` | Set `write_enabled` to allow changes |
| `MONEYBIRD_MCP_DATA_DIR` | `~/.moneybird-mcp` | Where local data is stored |
| `MCP_TOOL_DISCOVERY` | `full` | `search` shows a small tool set for clients that cannot take the full list |
| `MCP_TRANSPORT` | `stdio` | `http` for network mode |

`.env` files are never loaded automatically; pass one with `moneybird-mcp --env-file /absolute/path`.
OAuth with your own registered app is also possible: [OAuth](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/oauth.md).

## For AI agents

- **Installing:** run it as a stdio server with `uvx moneybird-mcp` (needs [uv](https://docs.astral.sh/uv/)
  and Python 3.11+) and pass the token as `MONEYBIRD_ACCESS_TOKEN`. Add `MONEYBIRD_ADMINISTRATION_ID`
  when the token reaches more than one administration. In Claude Code:
  `claude mcp add moneybird --scope user -e MONEYBIRD_ACCESS_TOKEN=<token> -- uvx moneybird-mcp`.
  Never use project scope: it writes the token into a shared `.mcp.json`.
- **Using the server:** start with `get_server_status` and `list_administrations`. For how-to
  questions call `get_bookkeeping_guide(topic)`. For writes, always: `prepare_*` → show the
  preview → wait for the user's explicit "yes" → `execute_approved_action` → report the verification.
- **Changing this repository:** read [AGENTS.md](https://github.com/Espaye/moneybird-mcp-server/blob/main/AGENTS.md) first.

## Documentation

| I want to… | Read |
|---|---|
| Install, upgrade or troubleshoot | [Getting started](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/getting-started.md) |
| See every tool | [Tool reference](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/tool-reference.md) |
| Understand writes, network mode and security | [Deployment and safety](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/deployment-and-safety.md), [threat model](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/threat_model.md) |
| Back up or delete local data | [Local data lifecycle](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/data-lifecycle.md) |
| Browse all documentation | [Documentation map](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/README.md) |
| Contribute | [Contributing](https://github.com/Espaye/moneybird-mcp-server/blob/main/CONTRIBUTING.md), [changelog](https://github.com/Espaye/moneybird-mcp-server/blob/main/CHANGELOG.md) |

## Support

A pre-1.0 community project: no guaranteed response time, uptime or bookkeeping correctness,
and no tax advice. Report bugs in [GitHub Issues](https://github.com/Espaye/moneybird-mcp-server/issues);
report vulnerabilities privately as described in
[SECURITY.md](https://github.com/Espaye/moneybird-mcp-server/blob/main/SECURITY.md).

## Licence

This project is **source-available, not OSI-approved open source**: the MIT License with the
**Commons Clause License Condition v1.0**. Personal and internal use, inspection and modification
are free. Selling it, offering it commercially as a service or repackaging it commercially needs a
separate commercial licence; for commercial licensing, contact the owner through
[GitHub Issues](https://github.com/Espaye/moneybird-mcp-server/issues). The full terms in
[LICENSE](https://github.com/Espaye/moneybird-mcp-server/blob/main/LICENSE) govern.
