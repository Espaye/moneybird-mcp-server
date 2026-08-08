# Moneybird OAuth (bring your own application)

**Language:** **English** · [Nederlands](getting-started.nl.md)

> This is an unofficial community integration and is not developed, endorsed, supported, or audited by Moneybird B.V.

## Who this is for

This page describes connecting Moneybird MCP through OAuth using **an OAuth
application you registered yourself**. That is the right path for two audiences:

- **Development and testing**, against the project's own registered application;
- **Self-hosters** who want OAuth semantics — a refresh token, scoped access,
  revocation from the Moneybird UI — and are willing to register an application
  and hold its Client Secret themselves.

**If you just want to use Moneybird MCP locally, use a personal API token
instead.** See [Getting started](getting-started.md). It is one environment
variable and no application registration.

### Why OAuth is not the default public setup

An OAuth Client Secret authenticates *the application*, not the user. It cannot
be shipped inside a source-available package installed from PyPI: anything
distributed to every user is not a secret, and a leaked application credential
affects every installation at once, not just one.

So this project does not, and will not, embed an application Client Secret in
the package. There is no shared "Moneybird MCP" application credential that
`pip install` hands you. Running the OAuth flow locally means using **your own**
registered application.

That is also why the personal API token remains the simple, supported public
path for local use: it is a per-user credential the user already controls, with
no application secret involved.

### Where this is heading

The hosted product solves this properly, and it is the reason the OAuth code
exists now:

| | Local OAuth (this page) | Future hosted service |
|---|---|---|
| Who registers the application | You | Us, once |
| Where the Client Secret lives | Your machine, in a file you select | Our backend, never on a client |
| How a user connects | CLI: paste an out-of-band code | Press **Connect Moneybird**, approve, done |
| Redirect URI | `urn:ietf:wg:oauth:2.0:oob` | An HTTPS callback |
| Where user tokens live | `~/.moneybird-mcp/moneybird_oauth_tokens.json` | Server-side, one token set per connection |

The out-of-band redirect is a development and local-integration mechanism; it
exists because a local CLI has no reachable callback endpoint. The hosted flow
will not use it.

The code is deliberately layered so the hosted implementation replaces only the
top of it — see [Reusable pieces](#reusable-pieces-for-the-hosted-flow) below.
Nothing about the hosted design requires a user to ever see a Client Secret,
an authorization code, or a token.

## Verification status

The flow on this page was run end to end against the live Moneybird service on
Windows on 2026-08-08, using a real registered application: the authorization
page, the out-of-band code exchange, `/administrations` under the OAuth grant,
administration selection, `auth status`, real read operations through the stored
connection with no `MONEYBIRD_ACCESS_TOKEN` set, and `auth logout`.

That also settles one thing the documentation alone could not: `/administrations`
documents no scope requirement, and a real OAuth grant does reach it — which is
what lets `auth login` verify a new connection before anything is stored.

## Set-up

### 1. Register your own external OAuth application

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

Moneybird documents six scopes and assigns them **per endpoint**. The grouping
is not the intuitive one, so the table below follows Moneybird's own endpoint
reference rather than the generic Authentication page. The machine-readable
source is `docs/moneybird_api_scopes.json`, generated from the official OpenAPI
spec by `scripts/render_api_scopes.py`; `tests/test_oauth_scopes.py` checks
every claim here against it.

| Tool area | Required | Notes |
|---|---|---|
| Sales invoicing | `sales_invoices` | Invoices, recurring invoices, credit invoices, sending, payments |
| Purchase administration | `documents` | Purchase invoices, receipts, general journal documents, attachments |
| Estimates | `estimates` | `list_estimates` only |
| Bank mutations | `bank` | Mutations and their bookings |
| Time registration | `time_entries` | `list_time_entries` only |
| Settings and reference data | `settings` | Financial **accounts**, products, projects, creating a ledger account |
| Reports: balance sheet, cash flow, general ledger | `bank` | Filed under bank, not settings |
| Reports: profit and loss, tax, journal entries | `documents` **and** `sales_invoices` | Both together; backs `analyze_vat_settlement` |
| Reports: debtors, revenue, subscriptions | `sales_invoices` | Debtors, debtors aging, revenue by contact/project, subscriptions |
| Reports: creditors, expenses, assets | `documents` | Creditors, creditors aging, expenses by contact/project, assets |

Three of these are easy to get wrong:

- **Reports do not share one scope.** Each report carries its own requirement,
  and **no report requires `settings`**. A connection with only `settings` can
  read none of them.
- **Financial accounts are `settings`; financial mutations are `bank`.** The two
  read as one feature and are scoped differently.
- **Products and projects are `settings`**, documented explicitly as such.

### Reachable without a scope of their own

| Area | Requirement |
|---|---|
| Contacts | Any one of `estimates`, `sales_invoices`, `documents`, `bank`, `settings` |
| Reading ledger accounts and tax rates | Any one of `settings`, `sales_invoices`, `documents`, `estimates` |
| Listing administrations | No scope required — which is why `auth login` can verify any new connection |

Because contacts are granted by any resource scope, a bookkeeping integration
never has to widen its request to reach them.

### Why all six are requested by default

Each of the six is required by at least one currently exposed tool, by an
endpoint that accepts no substitute:

| Scope | Only-justification |
|---|---|
| `sales_invoices` | `/sales_invoices` |
| `documents` | `/documents/*` |
| `estimates` | `/estimates` |
| `bank` | `/financial_mutations` |
| `time_entries` | `/time_entries` |
| `settings` | `/financial_accounts`, `/products`, `/projects`, `POST /ledger_accounts` |

`tests/test_oauth_scopes.py` proves that minimality from the snapshot: removing
any one scope must break a client endpoint, so if a tool removal ever makes a
scope unnecessary, the test fails and the request should shrink.

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

| Profile | Scopes | Unavailable |
|---|---|---|
| `full` (default) | all six | nothing |
| `bookkeeping` | `sales_invoices documents bank settings` | estimates, time registration (every report still works) |
| `invoicing` | `sales_invoices settings` | purchases, bank, estimates, time registration, and every report except the debtor/revenue/subscription group |

`moneybird-mcp auth scopes` prints the same breakdown, computed from the code
rather than copied. An unknown scope or profile is rejected before the browser
opens. If Moneybird grants less than was asked for, `auth login` says which
scopes are missing, because the affected tools would otherwise fail with a bare
authorization error in the middle of a later task.

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

## Reusable pieces for the hosted flow

Only the presentation layer is specific to the local out-of-band CLI:

| Concern | Module | Hosted reuse |
|---|---|---|
| Scope catalogue and rationale | `oauth_scopes.py` | Unchanged |
| Token model and storage interface | `oauth_store.py` | Implement `TokenStore` over per-tenant encrypted rows; every method already takes the connection's profile explicitly, so there is no global-connection assumption to unpick |
| URL construction, both grants, refresh | `oauth.py` | Unchanged. `generate_state` / `parse_authorization_callback(expected_state=…)` already implement the CSRF handling a redirect flow needs, which the OOB flow does not use |
| Administration selection | `oauth.py` + connection record | Unchanged |
| API authentication | `credentials.py`, `client.py` | Unchanged |
| Out-of-band prompt | `auth_cli.py` | Replaced by an HTTPS callback route |

What the hosted product still needs beyond this — user identity, grant
ownership, authorization, durable artifact ownership, trusted write
confirmation, and a per-IP rate-limit story — is in
[hosted gateway design](hosted_gateway_design.md).

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
