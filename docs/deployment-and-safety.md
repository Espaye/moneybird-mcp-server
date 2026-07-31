# Deployment and safety

Moneybird MCP has deliberately different boundaries for local, single-user network, and hosted operation.

## Supported posture

| Mode | Transport | Credential source | Writes | Status |
|---|---|---|---|---|
| `local` | stdio | request context, environment, or local OAuth store | Read-only by default; explicit experimental opt-in | Supported default |
| `network_single_user` | authenticated HTTP/SSE | environment or local OAuth store | Read-only by default; explicit experimental opt-in | Experimental |
| `hosted_request_only` | authenticated HTTP/SSE behind a trusted gateway | one gateway-injected request token and administration | Refused | Containment mode only |

The repository does not implement production multi-user identity, sessions, per-user OAuth grants, encrypted credential storage, tenant-isolated approvals, or hosted reconciliation.

## Read-only default

The server defaults to:

```text
MONEYBIRD_CAPABILITY_MODE=read_only
```

Write execution is mechanically refused unless that value is explicitly set to `write_enabled`. To pin the restriction in a launch script, service definition, or MCP-client configuration, set the variable explicitly rather than relying on the default:

```bash
export MONEYBIRD_CAPABILITY_MODE="read_only"
```

## Experimental writes

Writes are enabled only when all of the following are true:

- credential mode is `local` or `network_single_user`;
- `MONEYBIRD_CAPABILITY_MODE=write_enabled` is set;
- a prepare tool has produced a durable preview and approval ID;
- the matching executor or `execute_approved_action` is called;
- the action-specific checks accept the result.

This is not an independent human-confirmation authority. The same model-visible channel can receive an approval ID and call an executor. A trusted client UI may add a separate confirmation boundary, but the repository does not mint or verify that receipt.

Moneybird does not provide cross-object transactions for several workflows. Partial, ambiguous, or verification-failed outcomes are recorded as unresolved rather than reported as success.

## Network authentication

Every HTTP or SSE listener requires `MCP_AUTH_TOKEN`, including loopback.

Example single-user local HTTP server:

```bash
export MCP_AUTH_TOKEN="a-long-random-secret"
export MONEYBIRD_CREDENTIAL_MODE="network_single_user"
export MCP_TRANSPORT="http"
moneybird-mcp
```

The streamable HTTP endpoint is:

```text
http://localhost:8000/mcp
```

Legacy SSE is available at `/sse` when explicitly selected.

A shared secret is a coarse server gate. It is not user identity, tenant membership, role-based authorisation, OAuth grant isolation, or a safe public multi-user boundary.

## TLS and non-loopback listeners

The server defaults to `127.0.0.1`. It refuses a non-loopback plaintext bind unless:

```text
MCP_TRUSTED_TLS_PROXY=true
```

Set that only when a trusted reverse proxy actually terminates TLS before the application listener. The gateway or proxy must strip client-supplied Moneybird credential headers and inject trusted context itself.

## ChatGPT and other remote clients

Some MCP clients connect only to network-accessible HTTPS endpoints. For local testing, a trusted tunnel can terminate TLS while Moneybird MCP remains bound to loopback.

A tunnel does not convert the static-secret single-user server into a production hosted service. Confirm the client's current authentication and destructive-tool confirmation behaviour before exposing an endpoint.

Relevant documentation:

- [OpenAI MCP documentation](https://developers.openai.com/api/docs/mcp)
- [OpenAI developer mode](https://platform.openai.com/docs/guides/developer-mode)

## Tool discovery

The default is compact Tool Search:

```text
MCP_TOOL_DISCOVERY=search
```

This exposes the core tools plus `search_tools` and `call_tool`, allowing the model to discover schemas on demand.

For an older client that cannot use Tool Search:

```bash
moneybird-mcp --tool-discovery full
```

The full mode exposes the complete native catalogue at connection time.

## Credential resolution

### Personal API token

Set:

```text
MONEYBIRD_ACCESS_TOKEN=...
```

When it is present, it takes precedence over the local OAuth store.

### Administration selection

Set `MONEYBIRD_ADMINISTRATION_ID` when the credential can access more than one administration. The server revalidates administration access before using administration-scoped local search state.

### Local OAuth store

When a personal token is absent, local and single-user modes can use the OAuth store created by `scripts/oauth_login.py`.

The token file contains secrets and is not encrypted by this project. Protect the data directory with an appropriate filesystem ACL.

### Hosted request credentials

`hosted_request_only` accepts only one nonblank gateway-injected token and administration per request. It does not fall back to environment or local OAuth credentials. It also refuses writes, durable sync/FTS access, and attachment parsing.

This mode is containment for a future gateway, not a production-hosting claim.

## Security documents

- [Security policy](../SECURITY.md)
- [Threat model](threat_model.md)
- [Data handling](data_handling.md)
- [Local data lifecycle](data-lifecycle.md)
- [Contribution safety invariants](../CONTRIBUTING.md)
