# Data handling and retention

## Data classes

| Class | Examples | Current/default treatment |
|---|---|---|
| Secrets | Moneybird/MCP tokens, OAuth client secrets, gateway URL keys | Sensitive local configuration/state; never intentionally package or include in application telemetry |
| Financial personal data | Contacts, invoices, mutations, reports, attachment text | Read from Moneybird; local durable caches only outside hosted request mode |
| Safety state | Prepared payloads/hashes, claims, outcomes, audit events | Sensitive durable local SQLite/JSON state; approvals expire, execution/audit records may remain |
| Derived data | Sync JSON, SQLite/FTS index | Same sensitivity as source; administration-scoped local files; refused in hosted request mode |
| Operational metadata | Operation/status/duration/trace and token-derived pseudonym | Keep bounded and redacted; pseudonyms are still potentially linkable metadata |

## Local and single-user network modes

Durable state is stored under `MONEYBIRD_MCP_DATA_DIR`. Depending on features used,
this includes OAuth profiles, approval/outcome records, the audit log, and search
sync/FTS data. The operator controls filesystem permissions, backup, retention, and
deletion. Do not place this directory in a public or broadly synchronized folder.
The server applies best-effort owner-only modes (`0700` for an explicitly configured
data directory and `0600` for current state files). These POSIX modes are not a
substitute for reviewing Windows ACLs, container volumes, backups, and synchronized
folders.

`network_single_user` uses the same local state model. `MCP_AUTH_TOKEN` protects the
network endpoint, and non-loopback operation requires an explicitly trusted TLS
proxy, but these controls do not turn local files into a multi-tenant store.

## Gateway demo

The loopback gateway stores Moneybird OAuth profile data, user/profile mappings, and
personal URL keys in plaintext local JSON. Writes are serialized within the process,
use atomic replacement, and attempt owner-only file permissions. The store is not
encrypted, transactional across processes, horizontally scalable, or suitable for
production identity and secret storage.

The embedded MCP server is forced to `hosted_request_only`. It performs live reads
only and refuses:

- all bookkeeping writes;
- durable search synchronization and reads from JSON/SQLite/FTS data;
- attachment download and PDF parsing;
- fallback to environment credentials or the local OAuth selection path.

The gateway is a localhost demo and must not be exposed as a hosted service.

## Logs and telemetry

Application telemetry should contain only bounded operation/template labels, status,
duration/retry data, opaque traces, and a token-derived pseudonymous scope. Do not
add credentials, full URLs, query values, record/reference/customer identifiers,
request or response bodies, previews, document contents, or attachment text.

This policy does not automatically sanitize infrastructure, reverse-proxy, shell, or
browser logs. In particular, the demo's personal URL key can leak through URL
handling. Production hosting requires an end-to-end logging and secret-redaction
review.

## Attachments

In local and authenticated single-user use, the current attachment tool:

- validates redirect targets, pins the validated public DNS address through the TLS
  connection, and does not forward the Moneybird bearer token to the signed storage
  host;
- enforces a 20 MiB download limit, PDF content-type/magic validation, 100-page
  limit, and 40,000-character output limit;
- holds current downloads in memory and does not retain an attachment file.

Parsing runs in a disposable spawned worker process with a hard wall-clock timeout
and best-effort process-memory containment on supported Unix and Windows platforms.
Hosted mode still disables the operation entirely: per-document process isolation is
not a hosted queue, global/per-tenant capacity controller, abuse policy, or operational
lifecycle.

Older releases wrote attachment files to the data directory. The current server
does not automatically adopt or delete those legacy files. Review, quarantine, or
delete them according to the operator's retention requirements; never infer tenant
ownership from a filename.

## Hosted production requirements

Before a hosted product can store financial data:

- authenticate a durable principal and active grant/administration membership before
  every financial operation or artifact read;
- encrypt provider tokens and retained financial artifacts with separately managed
  keys;
- define token rotation and membership-revocation exposure;
- enforce tenant-plus-administration ownership for all artifacts;
- provide account export, revocation, deletion, retention jobs, backup, and tested
  restoration;
- isolate untrusted document processing and bound resources;
- document any legal basis for retaining financial execution/audit history.

Binding the current local server publicly, or placing the demo behind TLS, does not
satisfy these requirements.

## Incident containment

On suspected cross-tenant access, credential disclosure, duplicate write, or false
success:

1. disable writes and public access;
2. revoke or rotate affected credentials;
3. preserve execution/access evidence without copying customer content
   unnecessarily;
4. reconcile ambiguous Moneybird state;
5. notify affected operators under applicable obligations;
6. forward-fix and verify containment before re-enabling the affected surface.
