# Security policy

Moneybird MCP handles bookkeeping data and can perform financial mutations. Treat every
credential, cache, approval, audit event, and attachment as sensitive.

## Supported versions

This project is pre-1.0. Security fixes are made on the latest released version and `main`.
Older releases may not receive backports. The package is experimental and is not a substitute
for an accountant, access-control system, or independently verified payment system.

## Supported deployment posture

- **Local stdio:** primary supported path; credentials come from local configuration or
  the local OAuth store. The capability mode defaults to `read_only`.
- **Authenticated single-user network:** experimental. Every SSE/HTTP listener requires
  `MCP_AUTH_TOKEN`; non-loopback binds also require
  `MCP_TRUSTED_TLS_PROXY=true` and a correctly configured TLS terminator. The static
  bearer secret does not provide multi-user identity.
- **Request-context integration:** `hosted_request_only` accepts credentials only from
  trusted per-request context and refuses writes, durable search state, and attachment
  parsing. It is an advanced integration boundary, not a complete identity system.

Release automation also depends on external repository controls. The `pypi`
environment must accept deployments only from protected `main`, and a `v*` tag
ruleset must prevent updates and deletion. Publication requires a manual dispatch
with the exact version and full default-branch commit SHA; pushes and merges do not
publish.

This solo-maintainer beta does not claim independent human deployment approval.
The manual dispatch, protected branch, restricted environment, immutable-version
checks, exact-artifact handoff, and deliberate final operator checkpoint are the
proportionate controls. Add an independent required reviewer and prevent
self-review if a suitable additional maintainer becomes available.

## Reporting a vulnerability

Do not open a public issue containing credentials, customer data, exploit details, or other
sensitive material.

Use GitHub's private vulnerability reporting flow:

<https://github.com/Espaye/moneybird-mcp-server/security/advisories/new>

Include:

- the affected revision/version and deployment mode;
- a minimal reproduction with synthetic data;
- realistic impact and prerequisites;
- whether a Moneybird token, administration, cache, approval, or write is involved;
- any proposed mitigation.

Revoke any credential that may have been exposed. We will acknowledge a complete report as
soon as practical, coordinate validation and remediation privately, and publish an advisory
after a fix or mitigation is available. Do not rely on an unconfirmed response deadline for
urgent containment: disable network exposure and writes immediately.

## Current trust boundary

- Local stdio is the primary supported deployment.
- Model instructions and tool annotations are not security controls.
- A prepare/execute approval ID is not, by itself, proof that a human confirmed a write.
- Network and request-context deployments require explicit fail-closed identity and capability modes.
- `write_enabled` is available only for local and authenticated single-user operation;
  request-context mode refuses writes unconditionally.
- Moneybird fields and attachment text are untrusted model input.

See [the threat model](docs/threat_model.md) and [data handling](docs/data_handling.md).
