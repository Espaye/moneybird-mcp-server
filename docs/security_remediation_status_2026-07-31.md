# Security remediation status for the 0.4.0 candidate

This document maps the findings in the historical
[`security_readiness_review_2026-07-30.md`](security_readiness_review_2026-07-30.md)
to the current candidate. It does not rewrite that review or claim that its
revision already contained these controls.

Supported release profile: local stdio, mechanically read-only by default, with
experimental operator-enabled local writes. The gateway remains a localhost
demonstration. No status below is a production-hosting approval.

| Finding | Current status | Current implementation and evidence |
|---|---|---|
| F-01 | contained for the supported local/read-only profile | `capabilities.py` defaults to read-only and hosted request mode rejects writes. Approval IDs are explicitly documented as model-visible, not trusted human confirmation. `test_capabilities.py` covers the mechanical gates. Independently trusted confirmation remains required for hosting or any hosted writes. |
| F-02 | fixed | SQLite claim/attempt state in `safety.py` atomically permits one execution. Concurrency/replay coverage is in `test_safety_kernel.py`. |
| F-03 | fixed | Closed typed outcomes prevent failed, partial, ambiguous, or verification-failed work from becoming verified success. Covered by `test_safety_kernel.py`, `test_write_contract_regressions.py`, and action safety suites. |
| F-04 | fixed | Dispatch phases, leases, unresolved outcomes, occurrence identities, and the operator-only reconciliation CLI block automatic repetition. Covered by safety and write-contract regression tests. |
| F-05 | fixed | `credentials.py` has explicit local, single-user, and request-only modes; hosted request mode has no environment/OAuth fallback. Covered by `test_credential_modes.py` and `test_gateway_demo.py`. |
| F-06 | contained for the supported local/read-only profile | Local cached reads revalidate current administration membership; hosted request mode cannot read or build durable indexes. Covered by `test_cache_authorization.py` and `test_search_fts.py`. A hosted principal/grant-owned index is not built. |
| F-07 | fixed | Administration and record path construction is confined and numeric IDs are validated. Covered by `test_path_confinement.py` and client spec-conformance tests. |
| F-08 | not required for the supported profile but required for hosting | Every standalone listener has static edge authentication, but the demo URL key is not public MCP OAuth. Public MCP edge OAuth and non-URL sessions remain unbuilt. |
| F-09 | not required for the supported profile but required for hosting | Local files have best-effort permissions; gateway JSON remains plaintext demo storage. Hosting requires durable principals/sessions/grants and encrypted token storage. |
| F-10 | not required for the supported profile but required for hosting | Local OAuth state helpers exist, but durable session-bound, one-time hosted state and a canonical public origin remain unbuilt. |
| F-11 | not required for the supported profile but required for hosting | Local mode permits omission only when exactly one reachable administration can be selected. The demo still selects its first administration; hosting requires explicit persisted selection. |
| F-12 | fixed | Approval/audit state is administration-scoped and transactional safety decisions use SQLite rather than legacy JSONL success suppression. Covered by `test_safety_kernel.py` and `test_state_permissions.py`. |
| F-13 | fixed | `write_contracts.py` is a fail-closed, versioned registry for every exposed approval executor. Covered by `test_write_contract_regressions.py`. |
| F-14 | fixed | `telemetry.py` normalizes paths and stores no raw IDs, URLs, query values, bodies, or responses. Covered by `test_performance.py`. Hosting still needs an end-to-end proxy/log review. |
| F-15 | contained for the supported local/read-only profile | `client.py` pins validated numeric public addresses while preserving TLS hostname verification and strips the Moneybird bearer; `attachments.py` bounds bytes/type/magic/pages/text and uses a disposable worker. `test_attachments.py` covers redirect, DNS, size, type, magic, timeout, and isolation behavior. Hosted parsing stays disabled pending capacity and lifecycle controls. |
| F-16 | contained for the supported local/read-only profile | Untrusted Moneybird/PDF content cannot change the hard default capability. A trusted confirmation boundary and adversarial model evaluation are still required before hosted writes. |
| F-17 | fixed | Money verification uses normalized decimal semantics and controlled-field comparison. Covered by `test_money_verification.py` and write-contract regression tests. |
| F-18 | fixed | The clean-process default is mechanically read-only; writes require `write_enabled`, and hosted request mode refuses them regardless. Covered by `test_capabilities.py`. |
| F-19 | not required for the supported profile but required for hosting | Local bounded-parallel sync remains supported. Hosted jobs, quotas, rate limits, monitoring, backpressure, backup, and recovery are not built. |
| F-20 | still open | Domain logic is substantially separated, but MCP adapters are not yet a fully provider-neutral application core. This does not block the supported local/read-only beta. |
| F-21 | contained for the supported local/read-only profile | CI has tests, minimum dependencies, audit, Ruff, coverage, reproducible builds, hygiene, smoke installs, SBOM, pinned Actions, and provenance checks. Publication is manual-only from the default branch, requires the exact version and full commit SHA, refuses any existing PyPI version/tag/release, and passes the tested artifacts between jobs. External GitHub environment/tag controls remain required. |
| F-22 | not required for the supported profile but required for hosting | No provider-neutral model reliability/cost evaluation exists. It is mandatory before product/model claims or hosted writes. |
| F-23 | fixed | Gateway construction imports its dependencies and starts in the tested loopback-only configuration. Covered by `test_gateway_demo.py`. |

## Hosted blockers

The following remain explicit no-go items: public MCP edge OAuth; durable
principal/session/grant state; encrypted token storage; explicit administration
selection; account revocation/deletion/export; tenant-aware cache ownership;
principal/grant-bound search with revocation checks; asynchronous jobs, rate limits,
monitoring, backup and recovery; independently trusted human confirmation for
writes; and provider-neutral model evaluation.

## New configuration-boundary remediation

The 0.4.0 candidate also removes untrusted working-directory configuration
discovery. `config.load_env_file()` is invoked only for an explicit
`--env-file PATH`, resolves the selected path, validates variable names, and uses
`setdefault` so parent-process values win. `test_env_file_boundary.py` uses clean
subprocesses to prove a hostile current directory cannot change credentials,
administration, capability, transport, listener, proxy acknowledgement, auth
secret, data directory, OAuth configuration, or tool discovery.
