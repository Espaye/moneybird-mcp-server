# Threat model

## Scope and deployment status

Moneybird MCP reads bookkeeping data and, when explicitly enabled, can mutate
contacts, documents, invoices, payments, and bank bookings.

The primary supported deployment is local stdio. Authenticated
`network_single_user` is experimental. `hosted_request_only` is an advanced
request-context mode: it accepts credentials only from a trusted edge, performs
live Moneybird reads, and refuses writes, durable search state, and attachment
parsing. This repository does not supply multi-user identity or tenant isolation.

## Assets

- Moneybird access/refresh tokens and OAuth client credentials;
- administration membership and selected-administration intent;
- contacts, invoices, receipts, reports, mutations, attachment contents, and
  sync/FTS caches;
- write previews, approval payloads, claims, executions, audit events, and
  unresolved outcomes;
- MCP/session credentials, local logs, and backups.

## Trust boundaries

1. MCP client/model to server.
2. Network listener or trusted edge to an authenticated caller and Moneybird grant.
3. Grant to an explicitly authorized administration.
4. Server to Moneybird API and signed attachment-storage redirects.
5. Untrusted Moneybird/document content to model context.
6. Process to durable state, logs, release artifacts, and backups.

Prompts, tool descriptions, annotations, model reasoning, and an approval ID are not
independent authorization or human-confirmation boundaries.

## Current enforced controls

- Capability mode defaults to `read_only`.
- `write_enabled` applies only to local and authenticated single-user operation.
  `hosted_request_only` refuses every write executor regardless of that setting.
- All SSE/HTTP transports require `MCP_AUTH_TOKEN`. Non-loopback binds additionally
  require an explicit trusted-TLS-proxy acknowledgement.
- Request-context credentials come only from the trusted request context; no
  environment or local OAuth fallback is allowed.
- Request-context search performs live reads and revalidates administration access
  instead of reading a durable index. Sync and attachment parsing fail before cache, client, or
  parser access.
- Prepared writes bind a payload and use a durable atomic claim/outcome record to
  reduce replay and concurrent duplicate execution.
- Write executors use closed outcomes and action-specific postcondition checks where
  defined. Partial, failed, ambiguous, or verifier-failed work is not treated as
  verified success; this is not universal validation of bookkeeping correctness.
- Attachment downloads in supported local modes have HTTPS/public-address checks,
  DNS-to-TCP address pinning with normal TLS hostname verification, no bearer on
  the signed-storage request, and byte/type/magic/page/text limits. Parsing runs in
  a disposable worker with a wall-clock timeout and process-memory cap.
- Release automation verifies source/tag identity, minimum and current dependency
  graphs, reproducible artifacts, and the exact artifact matrix. It publishes
  through Trusted Publishing with PEP 740 attestations, cryptographically verifies
  the published provenance, creates a CycloneDX SBOM from the exact PyPI wheel, and
  repairs a GitHub release only from verified published files within the workflow.
  Pinned Bandit and a scheduled full-history Gitleaks scan are separate security
  signals. CodeQL is an additional signal when GitHub Code Security is available
  and deliberately enabled.

## Threats and current posture

| Threat | Current mitigation | Residual risk |
|---|---|---|
| Model self-approves a mutation | Preview/claim binds the action, but is not proof of human confirmation | Keep client-side destructive-tool confirmation enabled and supervise every write |
| Duplicate or ambiguous write | Atomic phase-aware claim and durable typed outcome; unresolved occurrences stay blocked; a local operator CLI records evidence-based resolution | Provider timeouts can still require manual reconciliation |
| Wrong administration | Explicit selection, numeric path confinement, and live membership revalidation before cache use | A credential with several administrations still requires careful operator selection |
| Cache disclosure after revocation | Local cache reads revalidate administration access | The operator remains responsible for deleting local files and backups |
| Credential leakage | Explicit credential modes, authenticated network transport, redacted errors and telemetry | Shell, proxy, client, and operating-system logs are outside the package boundary |
| False success | Every approval action has a versioned WriteSpec and controlled-field postcondition; closed failure/partial states cannot become success evidence | Provider/live-contract drift and crashes can require reconciliation |
| Route or redirect escape | Encoded API paths; signed-storage DNS addresses are validated and pinned through the TLS connection | Continue administration-pinning and adversarial route/redirect tests |
| Prompt injection | Content is treated as untrusted; hard capability gates do not depend on prompts | Model-visible approval is not independent human confirmation |
| Attachment exhaustion or parser exploit | Bounded download/page/text parsing plus a time/memory-limited disposable worker | Concurrent local callers still require operator-level resource controls |
| Telemetry/privacy leak | Structured, redacted operation telemetry | Review end-to-end logs and retention; treat pseudonyms as linkable |
| Supply-chain compromise | Dependency/Bandit/history-secret scans, optional CodeQL, minimum-version lane, SHA-pinned Actions, reproducible build check, SBOM, Trusted Publishing, and verified PyPI provenance | Ongoing dependency review and repository controls remain necessary |

## Current invariants

- A network listener does not start without its required static bearer configuration.
- A non-loopback network listener does not start without the trusted TLS-proxy
  acknowledgement.
- Missing request-context identity fails closed instead of falling back to local
  credentials.
- Request-context mode cannot dispatch a Moneybird mutation, build/read a durable search
  index, or download/parse an attachment.
- Read-only mode denies writes in the execution service, not only by hiding tools.
- A claimed action cannot be claimed a second time as a new execution.
- A Moneybird dispatch followed by lost response or process failure is not assumed to
  be success.
- Untrusted content cannot change credential or capability mode.

## Residual risk

- The local operator controls endpoint, filesystem, token, and client security.
- Dedicated data directories and current state files receive best-effort owner-only
  POSIX modes; Windows and mounted filesystems still require an operator-reviewed ACL.
- The live repository currently lacks the external `pypi` environment branch/reviewer
  policy and protected `v*` tag ruleset needed to make workflow release guards an
  independently enforced boundary.
- Local PDF isolation is per-document; concurrent callers still require
  operator-level resource controls.
- A model-callable approval flow cannot independently establish human intent.
- Some Moneybird operations are not transactionally composable; partial progress can
  require manual repair.
- Moneybird correctness/availability and legal, tax, or accounting advice are
  outside this project's assurance.

Revisit this model whenever a transport, provider, worker, webhook, stored artifact,
confirmation channel, or write action is introduced.
