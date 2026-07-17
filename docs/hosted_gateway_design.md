# Hosted gateway design — Moneybird MCP for non-technical users

Status: **design only, nothing built.** This is the architecture for the possible paid
product: a web app where a non-technical Moneybird user connects their bookkeeping in a
few clicks and gets a working MCP connection for Claude/ChatGPT — no pip, no config
files, no tokens to paste. The local-first distribution (PyPI wheel, `.mcpb` bundle)
remains the free, self-hosted path and the adoption funnel.

## Constraints (decided)

- The target user cannot do local MCP setup; first-run success in the web app is the
  yardstick for every choice.
- The product is AI-facing only: the user talks to an AI client, the model drives the
  guarded tools. No human-facing CLI/REPL.
- The preview-and-approve write flow and verified totals are non-negotiable — for a
  user who can't inspect raw API calls, they *are* the trust layer.
- Hosting domain and branding are TBD, published under the same pseudonymous identity
  as the package. It must not run on any existing company's domain or branding.

## What already exists in this repo (verified, load-bearing)

The MCP server is **already multi-tenant and gateway-ready**; the hosted product is a
*new front service*, not a rework of this one:

1. **Per-request tenancy.** `moneybird/credentials.py` resolves credentials per request:
   `X-Moneybird-Token` (+ optional `X-Moneybird-Administration-Id`) headers → env →
   local OAuth store. One running server serves many administrations; the token is the
   tenant boundary and is never logged.
2. **Per-administration state.** Approvals (SQLite), audit logs, and sync/FTS caches are
   keyed by administration id, so tenants cannot see each other's state.
3. **OAuth building blocks.** `moneybird/oauth.py` implements the authorization-code
   flow with an arbitrary `redirect_uri` and CSRF `state`
   (`build_authorize_url`, `exchange_authorization_code`,
   `parse_authorization_callback`, `refresh_access_token`), and a profile-keyed token
   store. The out-of-band variant used by `scripts/oauth_login.py` is just the
   local-dev special case of the same flow.
4. **Transports.** FastMCP serves stdio (local), SSE, and streamable HTTP; the HTTP
   modes are what the gateway fronts.

## Architecture

```
end user's MCP client (Claude/ChatGPT)
        │  MCP over HTTPS + per-user gateway key
        ▼
┌─ gateway web app (NEW, separate service) ─────────────────┐
│ sign-up / login            Moneybird OAuth (redirect flow) │
│ per-user token store (encrypted at rest)                   │
│ per-user MCP endpoint → proxy that injects                 │
│   X-Moneybird-Token / X-Moneybird-Administration-Id        │
└──────────────┬─────────────────────────────────────────────┘
               ▼  localhost / private network
   moneybird-mcp server (THIS repo, unmodified)
               ▼
        Moneybird REST API
```

- **The gateway owns users; the MCP server owns bookkeeping semantics.** The gateway
  never interprets tool calls; the MCP server never sees user accounts.
- **Onboarding flow:** sign up → "Connect Moneybird" → Moneybird OAuth consent
  (redirect flow with `state`) → gateway exchanges the code, stores tokens server-side
  → user gets a personal MCP endpoint URL + key (or a one-click Claude connector
  config) ready to paste into their AI client. Nothing else to configure.
- **Per-user gateway key, never the Moneybird token.** The end user's MCP client
  authenticates to the gateway with a revocable random key; the Moneybird token stays
  server-side. Compromise of the key is contained by revoking it, not the Moneybird
  grant.
- **Administration selection**: after OAuth, the gateway lists the token's
  administrations (`/administrations.json`) and stores the chosen id with the token, so
  the header pair is always complete and auto-selection ambiguity never reaches the
  end user.

## Security model

- Moneybird tokens: encrypted at rest, never logged, never sent to the browser after
  the OAuth exchange, only ever forwarded to the local MCP server over the private hop.
- OAuth `state` is generated per login attempt and validated on the callback
  (`parse_authorization_callback` enforces this); redirect URI is fixed and registered.
- Scopes: request only what the tool surface needs (`DEFAULT_OAUTH_SCOPES`).
- TLS at the edge (Cloudflare tunnel or equivalent); the header-credential path is
  TLS-only by design.
- This is financial data: per-tenant isolation must extend to any server-side sync/FTS
  caches the gateway enables (already keyed by administration id), and deletion of an
  account must delete tokens and cached data (GDPR).

## Open decisions (do not invent — decide with Sipke when concrete)

- Billing model and provider; free-tier boundaries.
- Domain and product name/branding.
- Bring-your-own-AI-client only, or an embedded chat (an Agent-SDK app talking to the
  same MCP endpoint) as the zero-setup tier.
- Where the gateway runs (VPS + tunnel vs. managed platform) and data residency (EU).
- Key rotation / session policy; whether Moneybird refresh-token rotation needs a
  scheduled job (Moneybird tokens currently do not expire).

## Milestones

1. **M0 (done):** library is gateway-ready — multi-tenant headers, per-administration
   state, redirect-capable OAuth with state validation.
2. **M1:** localhost end-to-end demo: a minimal web page that runs the redirect OAuth
   flow, stores the token under a profile, and proxies MCP requests with injected
   headers to a locally running `moneybird-mcp`.
3. **M2:** deploy that demo behind TLS on the chosen domain with per-user keys and an
   encrypted token store; invite-only alpha.
4. **M3:** accounts, billing, administration picker UI, revocation; public beta.
