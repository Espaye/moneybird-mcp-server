"""M1 localhost demo of the hosted gateway (docs/hosted_gateway_design.md).

Run with ``python -m gateway`` and open http://127.0.0.1:8035 — "Connect
Moneybird" runs the redirect OAuth flow, stores the token server-side under a
per-user profile, and hands the user a personal MCP endpoint URL
(``/u/<gateway-key>/mcp``). Requests to that endpoint get the tenant headers
injected before they reach the (in-process) MCP app, so the Moneybird token
never travels to the MCP client.

Demo-grade on purpose: loopback-only (it refuses to bind anything else), the
user/key store is a plaintext JSON file in the data dir, and there are no
accounts. M2 (TLS deploy, encrypted token store, revocation UI) builds on this.
This package is intentionally NOT part of the published wheel.
"""
