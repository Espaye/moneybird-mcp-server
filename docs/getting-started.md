# Getting started

**Language:** **English** · [Nederlands](getting-started.nl.md)

This guide covers the supported local installation of Moneybird MCP.

> This is an unofficial community integration and is not developed, endorsed, supported, or audited by Moneybird B.V.

## Requirements

- Python 3.11 or newer
- an MCP client such as Claude Desktop, Claude Code, Cursor, or another compatible client
- a Moneybird administration
- a fresh Moneybird API token

Create and manage tokens through Moneybird. Treat a personal API token like a password: do not paste it into a chat, GitHub issue, screenshot, log, or committed configuration file.

If a token has already been exposed, revoke it before continuing.

## Recommended setup with `uvx`

Install [uv](https://docs.astral.sh/uv/) and add this configuration to the MCP client:

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

Restart the client. A useful first request is:

```text
List the Moneybird administrations available to this connection, then show the server status.
```

Leave `MONEYBIRD_ADMINISTRATION_ID` out when the token can reach only one administration. When it can reach several, set the exact administration ID after listing them.

## Install with `pip`

```bash
python -m pip install --upgrade moneybird-mcp
```

Run the local stdio server, which starts read-only by default:

```bash
moneybird-mcp
```

The console command communicates over stdio. Normally the MCP client starts it; running it in an ordinary terminal is mainly useful for checking configuration or viewing `--help`.

## Optional PDF support

PDF attachment reading is intentionally optional:

```bash
python -m pip install --upgrade "moneybird-mcp[pdf]"
```

Without this extra, the rest of the server continues to work and the attachment tool reports that PDF support is missing.

## Explicit environment file

The package never loads a working-directory `.env` automatically. This prevents an untrusted launch directory from silently changing credentials, tenant selection, capability mode, or network policy.

An explicitly selected file may contain:

```env
MONEYBIRD_ACCESS_TOKEN=your-token-here
MONEYBIRD_ADMINISTRATION_ID=
MONEYBIRD_CAPABILITY_MODE=read_only
MONEYBIRD_MCP_DATA_DIR=
MCP_TOOL_DISCOVERY=search
```

Select it using an absolute path:

```bash
moneybird-mcp --env-file /absolute/path/moneybird-mcp.env
```

Values already present in the parent process environment take precedence over values in the file. Capability mode stays `read_only` unless a resolved value explicitly sets `write_enabled`.

## Upgrade

With `pip`:

```bash
python -m pip install --upgrade moneybird-mcp
```

With `uvx`, force a package refresh when needed:

```bash
uvx --refresh-package moneybird-mcp moneybird-mcp
```

Check the available options:

```bash
moneybird-mcp --help
```

## Uninstall

For a `pip` installation:

```bash
python -m pip uninstall moneybird-mcp
```

Uninstalling the package does not delete `~/.moneybird-mcp`. That directory may contain OAuth credentials, approvals, audit history, and search state. See [Local data lifecycle](data-lifecycle.md).

## OAuth instead of a personal token

Local and authenticated single-user modes can use Moneybird's OAuth authorization-code flow when `MONEYBIRD_ACCESS_TOKEN` is absent.

1. Register an application with Moneybird.
2. Configure `MONEYBIRD_OAUTH_CLIENT_ID` and `MONEYBIRD_OAUTH_CLIENT_SECRET`.
3. Run:

```bash
python -m moneybird.oauth_login --env-file /absolute/path/operator.env
```

This works for an installed package as well as a source checkout; in a checkout
`python scripts/oauth_login.py` is an equivalent wrapper.

The helper stores OAuth tokens in the Moneybird MCP data directory. The current local helper requests the scopes documented in the repository; review them before authorising. A production hosted service needs a separate HTTPS callback, user identity, grant store, revocation design, and tenant boundary.

## Claude Desktop extension

From a repository clone:

```bash
python scripts/build_mcpb.py
```

This creates a platform-specific `.mcpb` bundle in `dist/`. The bundle includes dependencies but still requires a compatible system Python. Its settings default to local credentials and read-only capability mode.

## Troubleshooting

### The command is not found

Use the same Python environment in which the package was installed:

```bash
python -m pip show moneybird-mcp
python -m pip install --upgrade moneybird-mcp
```

For MCP clients, `uvx` avoids most PATH problems.

### More than one administration is available

Call `list_administrations`, then set `MONEYBIRD_ADMINISTRATION_ID` to the required ID.

### An attachment cannot be read

Install the PDF extra and restart the MCP client:

```bash
python -m pip install --upgrade "moneybird-mcp[pdf]"
```

### A write tool is denied

That is expected in the default mode. Read [Deployment and safety](deployment-and-safety.md) before considering `write_enabled`.
