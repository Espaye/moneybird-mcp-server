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

Claude Code stores MCP registrations at a scope. The default `local` scope applies only to the current project. Use `--scope user` for a private configuration that must be available in every project. Project scope writes `.mcp.json` for sharing and must not contain a personal Moneybird token. If a server is connected but its tools are absent in another directory, inspect its scope with `claude mcp get moneybird`.

## Install with `pip`

```bash
python -m pip install --upgrade moneybird-mcp
```

Run the local stdio server, which starts read-only by default:

```bash
moneybird-mcp
```

The console command communicates over stdio. Normally the MCP client starts it; running it in an ordinary terminal is mainly useful for checking configuration or viewing `--help`.

On Windows, close every MCP client running this command before a `pip` install or upgrade. Otherwise the locked `moneybird-mcp.exe` can make `pip` fail with `WinError 32` after removing part of the old installation. Keep the client closed and rerun the install command to repair it. The recommended `uvx` setup avoids replacing that in-use console script.

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

On Windows, quit the MCP client before running this command. If an earlier attempt reported `WinError 32`, rerun it with the client still closed.

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

## OAuth with your own registered application

The personal API token above is the simple, supported way to run this locally. OAuth is an option for **development** and for **self-hosters who register their own OAuth application** — it is not the default public setup.

The reason is not preference. An OAuth Client Secret authenticates the *application*, not the user, so it cannot be shipped inside an installable package: anything distributed to every user is not a secret, and a leaked application credential would affect every installation at once. This project therefore embeds no application credential, and there is no shared "Moneybird MCP" Client Secret that `pip install` gives you.

If you do want OAuth locally:

1. Register your own **external application** at <https://moneybird.com/user/applications/new> with redirect URI `urn:ietf:wg:oauth:2.0:oob`.
2. Put its `MONEYBIRD_OAUTH_CLIENT_ID` and `MONEYBIRD_OAUTH_CLIENT_SECRET` in the environment or an explicitly selected file. These are application credentials, not tokens; treat the secret like a password and never commit it.
3. Run:

```bash
moneybird-mcp auth login --env-file /absolute/path/operator.env
```

4. Open the printed authorization URL, approve the application, and paste **only** the short authorization code Moneybird displays.
5. The command stores the connection in the Moneybird MCP data directory, then verifies it and selects the administration (asking when there is more than one). Start the MCP client normally.

The exchange in step 4 spends the authorization code, so the tokens are stored before they are verified: if that check fails, the command says so and keeps the credentials rather than making you authorize again. Skipping the administration question is fine too — the connection stays stored without one, and a later login or `MONEYBIRD_ADMINISTRATION_ID` supplies it. Nothing is guessed.

Manage the connection with `moneybird-mcp auth status` and `moneybird-mcp auth logout`. Neither ever prints a token or the client secret. `logout` deletes local credentials only: Moneybird publishes no revocation endpoint, so access is withdrawn at <https://moneybird.com/user/applications>.

`python -m moneybird_mcp.oauth_login` still works and is the same command; in a source checkout `python scripts/oauth_login.py` is an equivalent wrapper.

Review the requested scopes with `moneybird-mcp auth scopes`, and narrow them with `--scopes` if wanted. Full detail, including the per-endpoint scope requirements and precedence rules, is in [Moneybird OAuth](oauth.md).

The out-of-band redirect is a local/development mechanism. A future hosted service will hold the application's Client Secret in its backend, let a user connect by pressing **Connect Moneybird** over an HTTPS callback, and store per-user tokens server-side. That service additionally needs user identity, a grant store, a revocation design, and a tenant boundary; none of it is built.

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
