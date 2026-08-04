# Moneybird MCP

**Language:** **English** · [Nederlands](https://github.com/Espaye/moneybird-mcp-server/blob/main/README.nl.md)

[![PyPI](https://img.shields.io/pypi/v/moneybird-mcp.svg)](https://pypi.org/project/moneybird-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/moneybird-mcp.svg)](https://pypi.org/project/moneybird-mcp/)
[![CI](https://github.com/Espaye/moneybird-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/Espaye/moneybird-mcp-server/actions/workflows/ci.yml)

> **Unofficial community integration.** This project is not developed, endorsed, supported, or audited by Moneybird B.V.
>
> **Beta 0.6.1.** The supported setup is a local MCP server over stdio. It starts mechanically read-only. Experimental writes require an explicit local opt-in and supervised approval.

Use Claude, ChatGPT, Cursor, or another MCP client to search and work with a Moneybird administration. The server can read contacts, invoices, documents, bank mutations, reports, and locally indexed bookkeeping data.

## Get started

You need Python 3.11 or newer, an MCP client, and a fresh [Moneybird API token](https://developer.moneybird.com/authentication).

### Recommended: run with `uvx`

Add this server configuration to your MCP client:

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

Restart the client and ask it to list your Moneybird administrations.

`MONEYBIRD_ADMINISTRATION_ID` is optional when the token can access only one administration. Never paste a real Moneybird token into a chat, issue, log, or committed file.

Claude Code registrations are scoped. Its default `local` scope is available only in the current project; use `--scope user` when Moneybird should be available from every project. If `claude mcp list` says connected but a different project shows no tools, check `claude mcp get moneybird` and re-add the configuration at user scope. Do not use project scope for a configuration containing a personal token, because project scope writes a shared `.mcp.json` file.

### Install with `pip`

```bash
python -m pip install --upgrade moneybird-mcp
moneybird-mcp
```

For PDF attachment reading:

```bash
python -m pip install --upgrade "moneybird-mcp[pdf]"
```

Package page: [moneybird-mcp on PyPI](https://pypi.org/project/moneybird-mcp/)

On Windows, quit every MCP client that is running `moneybird-mcp` before installing or upgrading with `pip`; Windows cannot replace the locked console executable. If `pip` reports `WinError 32`, keep the client closed and run the install command again to repair the partial installation. The recommended `uvx` setup avoids upgrading that in-use console script.

## Upgrade

With `pip`:

```bash
python -m pip install --upgrade moneybird-mcp
```

On Windows, close the MCP client first. If an earlier attempt failed with `WinError 32`, rerun the same command while the client remains closed.

To force `uvx` to refresh its cached package metadata:

```bash
uvx --refresh-package moneybird-mcp moneybird-mcp
```

Check the installed command and available options:

```bash
moneybird-mcp --help
```

## What it can do

- Search contacts, sales invoices, purchase invoices, receipts, general journals, and bank mutations.
- Read Moneybird reports, including profit and loss, balance sheet, general ledger, VAT, debtor, and creditor reports.
- Review purchase invoices, invoice-delivery settings, bank mutations, and bookkeeping inconsistencies.
- Read PDF attachments locally when the optional PDF dependency is installed.
- Build a local search index for faster ranked search.
- Prepare guarded write previews when writes have been explicitly enabled.

The server uses compact Tool Search by default, so an MCP client does not need to load every tool schema at startup. See the [tool reference](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/tool-reference.md) and [Moneybird API coverage](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/moneybird_api_coverage.md).

## Read-only and write modes

The server starts mechanically read-only. This is the default and needs no flag:

```text
MONEYBIRD_CAPABILITY_MODE=read_only
```

Experimental writes are available only in local or authenticated single-user deployments:

```text
MONEYBIRD_CAPABILITY_MODE=write_enabled
```

Writes use durable prepare/execute approvals and action-specific verification. This is safety machinery, not independent proof that a human approved the action. Keep destructive-tool confirmation enabled in the MCP client and review every preview.

## Configuration

The most useful settings are:

| Setting | Default | Purpose |
|---|---|---|
| `MONEYBIRD_ACCESS_TOKEN` | none | Moneybird personal API token |
| `MONEYBIRD_ADMINISTRATION_ID` | automatic when unambiguous | Administration to use |
| `MONEYBIRD_CAPABILITY_MODE` | `read_only` | `read_only` or `write_enabled` |
| `MONEYBIRD_MCP_DATA_DIR` | `~/.moneybird-mcp` for installed stdio | Local approvals, audit, OAuth, and search state |
| `MCP_TOOL_DISCOVERY` | `search` | Compact discovery; use `full` for older clients |
| `MCP_TRANSPORT` | `stdio` | `stdio`, `http`, or legacy `sse` |

The package never discovers `.env` files automatically. Use an MCP-client environment block or an explicitly selected file:

```bash
moneybird-mcp --env-file /absolute/path/moneybird-mcp.env
```

See [Getting started](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/getting-started.md) for complete setup examples.

## Deployment boundary

| Mode | Intended use | Status |
|---|---|---|
| Local stdio | One user on one machine | Supported default |
| Authenticated HTTP/SSE | One trusted user behind authentication and TLS | Experimental |
| Hosted multi-user service | Multiple users or organisations | Not implemented |

Every HTTP/SSE listener requires `MCP_AUTH_TOKEN`, including loopback. Non-loopback listeners are refused unless a trusted TLS proxy is explicitly configured. The included gateway is a demonstration, not a production hosted product.

See [Deployment and safety](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/deployment-and-safety.md), [Security policy](https://github.com/Espaye/moneybird-mcp-server/blob/main/SECURITY.md), and the [threat model](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/threat_model.md).

## Local data

Installed stdio runs store local state in `~/.moneybird-mcp` unless `MONEYBIRD_MCP_DATA_DIR` is set. This can include:

- OAuth access and refresh tokens;
- the approvals SQLite database;
- per-administration audit logs;
- search indexes and caches;
- privacy-safe local telemetry.

These files are not encrypted by this project. Restrict access to the directory and read [Local data lifecycle](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/data-lifecycle.md) before backing up or deleting it.

## Documentation

- [Getting started](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/getting-started.md)
- [Tool reference](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/tool-reference.md)
- [Deployment and safety](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/deployment-and-safety.md)
- [Local data lifecycle](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/data-lifecycle.md)
- [Security policy](https://github.com/Espaye/moneybird-mcp-server/blob/main/SECURITY.md)
- [Support](https://github.com/Espaye/moneybird-mcp-server/blob/main/SUPPORT.md)
- [Contributing](https://github.com/Espaye/moneybird-mcp-server/blob/main/CONTRIBUTING.md)
- [Changelog](https://github.com/Espaye/moneybird-mcp-server/blob/main/CHANGELOG.md)
- [Moneybird API coverage](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/moneybird_api_coverage.md)
- [Release process](https://github.com/Espaye/moneybird-mcp-server/blob/main/docs/releasing.md)

## Support and status

This is a pre-1.0 community project. There is no guaranteed response time, uptime, data recovery, bookkeeping correctness, or tax advice.

Use [GitHub Issues](https://github.com/Espaye/moneybird-mcp-server/issues) for reproducible bugs and feature requests without secrets or customer data. Report vulnerabilities privately as described in [SECURITY.md](https://github.com/Espaye/moneybird-mcp-server/blob/main/SECURITY.md).

## Licence

This project is **source-available, not OSI-approved open source**. It is distributed under the MIT License with the **Commons Clause License Condition v1.0**.

Personal use, internal organisational use, inspection, and modification are permitted. Selling the software, offering a paid hosted service based substantially on it, or commercially repackaging it requires a separate commercial licence. For commercial licensing, contact the repository owner through [GitHub Issues](https://github.com/Espaye/moneybird-mcp-server/issues). The complete terms in [LICENSE](https://github.com/Espaye/moneybird-mcp-server/blob/main/LICENSE) govern.
