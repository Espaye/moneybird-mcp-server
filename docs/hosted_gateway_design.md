# Hosted gateway design

Status: **the repository contains an M1 loopback demo, not a production hosted
service.** Do not expose `gateway/` to the internet or treat it as a supported
multi-user deployment.

The accepted M2 production boundary and first vertical slice are recorded in
[M2 architecture decisions](m2_architecture_decisions.md). That decision does not
promote or modify this demo.

The local PyPI and `.mcpb` distributions remain the supported path. A future hosted
product would need a separate identity, authorization, storage, and operations layer.

## Current containment

The demo starts the MCP app in `hosted_request_only` credential mode. In that mode:

- only gateway-injected request credentials are accepted; environment and local OAuth
  fallback are disabled;
- tools may perform live Moneybird reads only;
- all writes are refused, even if `MONEYBIRD_CAPABILITY_MODE=write_enabled`;
- durable search synchronization and reads from JSON/SQLite/FTS caches are refused;
- attachment download and PDF parsing are refused.

This makes the demo useful for exercising OAuth, routing, and live read isolation
without presenting the repository's local durable state as a hosted tenant boundary.
It does not make the demo production-ready.

Hosted live search is a safe but incomplete fallback. It may make several sequential
Moneybird API calls and scans only bounded first pages; it is not a production search
solution. Production search requires a principal/grant-bound index, authorization and
revocation checks before reads, asynchronous synchronization, deletion/retention jobs,
rate limits, monitoring, backup, and tested recovery. That system is not built here.

The standalone MCP server has a different network mode:
`network_single_user`. Every SSE or streamable-HTTP listener requires
`MCP_AUTH_TOKEN`; a non-loopback bind also requires
`MCP_TRUSTED_TLS_PROXY=true`. That static bearer secret protects one server instance.
It is not user identity, delegated authorization, or a multi-tenant gateway.

## What M1 actually implements

`python -m gateway` runs a loopback-only, in-process demo:

1. A minimal web flow starts Moneybird OAuth and validates callback state.
2. The demo creates a 128-bit random user/profile identifier and a random personal
   URL key.
3. It stores user-to-profile and token data in local plaintext JSON files.
4. Requests to `/u/<key>/mcp` are mapped to that profile. Client-supplied Moneybird
   credential headers are stripped and the selected profile's credentials are
   injected into the in-process MCP request.
5. The MCP app resolves only that authenticated request context and performs live
   Moneybird reads.

JSON updates are serialized within the process, written through an atomic replace,
and given best-effort owner-only permissions (`0600`). Those measures reduce ordinary
local file corruption and disclosure; they do not provide encryption, cross-process
coordination, transactional identity storage, or a production secret store.

Other important limitations:

- the personal secret is embedded in the URL, so browser history, access logs,
  referrers, screenshots, or copied links can disclose it;
- there is no end-user MCP OAuth resource-server flow, session management, key
  rotation UI, account recovery, or robust revocation/deletion workflow;
- the demo automatically selects the first Moneybird administration instead of
  presenting and persisting an explicit administration choice;
- there is no TLS termination, fixed canonical public origin, proxy trust policy,
  rate limiting, quota system, job isolation, backpressure, monitoring, backup, or
  restore design;
- the process-local storage and locking model does not support horizontal scaling;
- the gateway package is source-only and is intentionally absent from the wheel,
  sdist, and `.mcpb`.

The URL key and injected request context are demo routing mechanisms, not proof of a
human's identity or confirmation of a bookkeeping change.

## Production trust boundaries

A hosted service must own and enforce:

- authenticated user identity and account lifecycle;
- an OAuth callback with fixed registered origins and durable, one-time state;
- encrypted credential storage, key management, rotation, revocation, deletion,
  export, backup, and restore;
- explicit Moneybird administration selection and authorization revalidation;
- a gateway credential that is not exposed in URLs or logs;
- tenant-aware quotas, audit access, incident response, and retention policy;
- TLS and a narrowly configured trusted-proxy boundary;
- isolation for any future durable index or document parser;
- an external confirmation channel if writes are ever introduced.

The MCP server should continue to own Moneybird tool semantics. The hosted boundary
must not rely on administration IDs alone, local SQLite keying, prompt text, or an
approval token as cross-tenant authorization.

## Milestones and release gates

1. **M0 (done):** local MCP server, OAuth helpers, compact discovery, and explicit
   credential/capability modes.
2. **M1 (done):** loopback OAuth/routing demo with forced live-read-only containment.
3. **M2 (not built):** identity, encrypted durable credential storage, explicit
   administration choice, revocation/deletion, TLS/proxy policy, abuse controls, and
   operational recovery. An invite-only read-only alpha is acceptable only after
   those controls are implemented and reviewed.
4. **M3 (not designed):** an AI-agent transport may be layered only through the
   proven M2 tenant/credential boundary. A generic hosted MCP endpoint would need
   its own OAuth resource-server release gate. Hosted writes remain out of scope.
5. **M4 (not designed):** polished customer product concerns such as public
   onboarding, billing, subscriptions, and support.

The M2 ADR settles the auth/runtime/database direction and a stable domain pattern;
the owner must still confirm the actual apex, regions, accounts, and live provider
configuration. Brand, billing, model choice, and whether to offer an embedded chat
remain later product decisions. Deploying the M1 demo behind TLS does not by itself
satisfy M2.
