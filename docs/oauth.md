# Connecting through Moneybird OAuth

**Language:** **English** · [Nederlands](getting-started.nl.md)

This is the recommended way to connect Moneybird MCP to a Moneybird account. No
Moneybird token is ever copied by hand.

> This is an unofficial community integration and is not developed, endorsed, supported, or audited by Moneybird B.V.

## Two authentication choices

| | Recommended: OAuth | Advanced: personal API token |
|---|---|---|
| What you configure | An application's client id and secret, once | A token string |
| Where tokens live | `moneybird_oauth_tokens.json` in the state directory, written by the CLI | Wherever you put `MONEYBIRD_ACCESS_TOKEN` |
| Renewal | A refresh token is stored and used automatically | Manual |
| Administration | Chosen during login and remembered | `MONEYBIRD_ADMINISTRATION_ID` |

Both remain fully supported. If both are configured, the personal token in
`MONEYBIRD_ACCESS_TOKEN` wins — see [Precedence](#precedence).

## Set-up

### 1. Register an external OAuth application

Open <https://moneybird.com/user/applications/new> and register an **external
application** (not a personal API token). Set the redirect URI to exactly:

```text
urn:ietf:wg:oauth:2.0:oob
```

Moneybird then shows a **Client ID** and a **Client Secret**.

### 2. Supply the client id and secret

These are application credentials, not your tokens. Put them in the parent
process environment, or in a file you select explicitly:

```env
MONEYBIRD_OAUTH_CLIENT_ID=your-client-id
MONEYBIRD_OAUTH_CLIENT_SECRET=your-client-secret
```

The client secret is a password. Do not commit it, paste it into a chat or
issue, or place it in a repository file. No `.env` is ever discovered
automatically; a file is used only when its path is passed with `--env-file`.

### 3. Log in

```bash
moneybird-mcp auth login --env-file /absolute/path/moneybird-mcp.env
```

The command:

1. prints the authorization URL and tries to open your browser (the URL is
   always printed, so headless and remote hosts work — add `--no-browser` to
   skip the launch entirely);
2. waits while you approve the application in Moneybird;
3. asks you to paste **only** the short authorization code Moneybird displays;
4. exchanges that code for an access token and a refresh token;
5. verifies the connection by listing the administrations it can reach;
6. selects the administration, asking you when there is more than one;
7. saves everything locally.

### 4. Start the MCP client normally

Nothing else to configure. The server picks up the stored connection on its own.

## Managing the connection

```bash
moneybird-mcp auth status    # which identity is configured, and from where
moneybird-mcp auth logout    # delete the local credentials
moneybird-mcp auth scopes    # what each requested scope is for
```

`auth status` prints no token, no client secret, and no fingerprint of either.
It reports presence, granted scopes, expiry, the selected administration, and
which credential source actually wins.

### Logout is not revocation

Moneybird publishes **no OAuth token revocation endpoint**. `auth logout`
deletes this machine's stored credentials; the authorization itself stays valid
until you withdraw it in Moneybird at
<https://moneybird.com/user/applications>. The command says so every time.

## Where credentials are stored

In the server state directory (`MONEYBIRD_MCP_DATA_DIR`, defaulting to
`~/.moneybird-mcp` for the installed `moneybird-mcp` command):

```text
~/.moneybird-mcp/moneybird_oauth_tokens.json
```

The file is written atomically and restricted to the owner where the platform
supports POSIX mode bits; on Windows it relies on the directory ACL. It is in
`.gitignore` and excluded from the wheel and sdist by
`scripts/check_dist_hygiene.py`.

Because `MONEYBIRD_MCP_DATA_DIR` decides the location, log in with the same
value the server runs with. `auth login` prints the exact path it wrote to, and
`auth status` prints the path it reads from.

Multiple connections can coexist under `--profile NAME`; the default profile is
`default`.

## Precedence

Credential sources are tried in a fixed order, so the active Moneybird identity
is never a surprise:

1. **Request context** — only in `hosted_request_only` mode, never locally.
2. **`MONEYBIRD_ACCESS_TOKEN`** — a personal API token in the environment.
3. **The stored OAuth connection.**

The administration follows the same rule: an explicit
`MONEYBIRD_ADMINISTRATION_ID` overrides the one chosen at login. `auth status`
states which one is active and flags the override when both are present.

Hosted request mode never reads the local OAuth store.

## Scopes

Moneybird documents six scopes. This server requests all six by default,
because it is a general bookkeeping assistant and a connection that cannot see
the bank feed or purchase invoices is broken rather than safer.

| Scope | Covers | Example tools |
|---|---|---|
| `sales_invoices` | Sales invoices, recurring invoices, credit invoices, sending, payments on sales invoices | `list_sales_invoices`, `prepare_create_sales_invoice_draft`, `prepare_send_sales_invoice` |
| `documents` | Purchase invoices, receipts, general journal documents, attachments | `list_purchase_documents`, `prepare_reconcile_purchase_invoice`, `prepare_vat_settlement_journal` |
| `estimates` | Quotations | `list_estimates` |
| `bank` | Financial accounts, mutations, bank bookings | `list_financial_mutations`, `suggest_bank_mutation_matches`, `prepare_link_bank_mutation_booking` |
| `time_entries` | Time registration | `list_time_entries` |
| `settings` | Ledger accounts, tax rates, workflows, document styles, custom fields — and, by inference, products, projects and reports | `list_ledger_accounts`, `list_tax_rates`, `get_financial_report` |

Contacts need no scope of their own: Moneybird grants contact access with any
of `sales_invoices`, `documents`, `estimates`, `bank` or `settings`.

The mapping for `/products`, `/projects` and `/reports` is an inference —
Moneybird does not document which scope covers them. `moneybird-mcp auth scopes`
marks those rows, and `moneybird_mcp/oauth_scopes.py` is the machine-readable
source.

### Scopes are not a write policy

Moneybird scopes are per resource family and have **no read-only variant**:
`documents` grants reading *and* rewriting purchase invoices. Whether this
server may write is a separate, locally enforced decision —
`MONEYBIRD_CAPABILITY_MODE` plus the prepare/approve/execute flow. Requesting
fewer scopes does not make a connection safer to write with, and enabling
writes does not require broader scopes.

### Requesting fewer scopes

```bash
moneybird-mcp auth login --scopes bookkeeping
moneybird-mcp auth login --scopes "sales_invoices settings"
```

or set `MONEYBIRD_OAUTH_SCOPES`. Named profiles:

| Profile | Scopes |
|---|---|
| `full` (default) | all six |
| `bookkeeping` | `sales_invoices documents bank settings` |
| `invoicing` | `sales_invoices settings` |

An unknown scope or profile is rejected before the browser opens. If Moneybird
grants less than was asked for, `auth login` says which scopes are missing,
because the affected tools would otherwise fail with a bare authorization error
in the middle of a later task.

## Token expiry and refresh

Moneybird states that access tokens do not currently expire, and asks
integrations to store the refresh token and be ready for that to change. This
server does both:

- expiry metadata is honoured when Moneybird sends it, with a 60-second margin;
- a token with no expiry metadata is never refreshed, so ordinary use costs no
  extra request;
- a refresh that omits the refresh token or the scopes leaves the stored values
  in place instead of clearing them;
- a failed refresh raises and leaves the stored credentials untouched — a
  network problem never costs you your refresh token;
- neither grant is retried automatically: an authorization code is single-use,
  and a refresh may rotate the refresh token.

## The out-of-band redirect is a local mechanism

`urn:ietf:wg:oauth:2.0:oob` suppresses the redirect and makes Moneybird display
the code in the browser, which is what lets a local CLI complete the flow with
no reachable callback endpoint. It is a development and local-integration
mechanism.

A future hosted product will register an HTTPS callback instead. The code is
already split so that only the first step changes: URL construction, the code
exchange, the refresh, the token model, credential storage, administration
selection and API authentication live below the CLI, and credential storage is
an interface (`moneybird_mcp/oauth_store.py`) that a per-tenant database
implementation can replace. See
[hosted gateway design](hosted_gateway_design.md) for what production would
additionally require.

## Troubleshooting

**`invalid_client`** — the client id or secret was rejected. Check them against
the application at <https://moneybird.com/user/applications>.

**`invalid_grant`** — the authorization code was already used, expired, or was
issued for a different redirect URI. Run `auth login` again and paste the new
code promptly.

**"That does not look like a Moneybird authorization code"** — paste only the
code, with no surrounding text or URL.

**`auth status` says no credentials but you just logged in** — the two runs used
different `MONEYBIRD_MCP_DATA_DIR` values. Compare the path in the login output
with the one `auth status` prints.

**More than one administration** — run
`moneybird-mcp auth login --administration ID`, or set
`MONEYBIRD_ADMINISTRATION_ID`. Nothing is selected silently.
