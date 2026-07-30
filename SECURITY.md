# Security policy

Moneybird MCP handles bookkeeping data and can perform financial mutations. Treat every
credential, cache, approval, audit event, and attachment as sensitive.

## Supported versions

This project is pre-1.0. Security fixes are made on the latest released version and `main`.
Older releases may not receive backports. The package is experimental and is not a substitute
for an accountant, access-control gateway, or independently verified payment system.

## Supported deployment posture

- **Local stdio:** primary supported path; credentials come from local configuration or
  the local OAuth store. The capability mode defaults to `read_only`.
- **Authenticated single-user network:** experimental. Every SSE/HTTP listener requires
  `MCP_AUTH_TOKEN`; non-loopback binds also require
  `MCP_TRUSTED_TLS_PROXY=true` and a correctly configured TLS terminator. The static
  bearer secret does not provide multi-user identity.
- **Hosted gateway:** the repository contains only a loopback demo and is a production
  no-go. Its MCP app is forced to `hosted_request_only`: live reads only, with all
  writes, durable search sync/cache access, and attachment parsing refused.

Do not expose the demo gateway or a local data directory as a hosted tenant boundary.

Release automation also depends on external repository controls. Before treating the
publication path as production-ready, restrict the `pypi` environment to `main`, add
an independent required reviewer, and protect `v*` tags with a repository ruleset.
The workflow's own ref/SHA/tag checks are defense in depth and cannot make an
otherwise unprotected tag immutable.

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
- Network and hosted deployments require explicit fail-closed identity and capability modes.
- `write_enabled` is available only for local and authenticated single-user operation;
  hosted request mode refuses writes unconditionally.
- Moneybird fields and attachment text are untrusted model input.

See [the threat model](docs/threat_model.md), [data handling](docs/data_handling.md), and the
[dated 2026-07-30 readiness review](docs/security_readiness_review_2026-07-30.md). That review
is a revision-specific historical snapshot, not the current implementation status.
