# Support

Moneybird MCP is an unofficial community project. It is not developed, endorsed, supported, or audited by Moneybird B.V. Moneybird support is not responsible for this repository.

## Before opening an issue

Read:

- [Getting started](docs/getting-started.md)
- [Deployment and safety](docs/deployment-and-safety.md)
- [Local data lifecycle](docs/data-lifecycle.md)
- [Security policy](SECURITY.md)

Use [GitHub Issues](https://github.com/Espaye/moneybird-mcp-server/issues) for reproducible bugs and feature requests that contain no secrets or customer data.

Include:

- `moneybird-mcp` version;
- Python version and operating system;
- MCP client;
- transport;
- credential mode;
- capability mode;
- the failing command or tool;
- a minimal reproduction using synthetic or redacted data;
- a sanitized traceback or error response.

## Never post

Do not include:

- Moneybird access or refresh tokens;
- OAuth client secrets;
- MCP bearer tokens;
- raw invoices, contact records, bank data, audit logs, caches, or attachments;
- complete environment files;
- screenshots containing customer or financial data.

Revoke any credential that may have been exposed.

## Security vulnerabilities

Do not open a public issue for a vulnerability. Use the private process in [SECURITY.md](SECURITY.md).

## Service level

This is a pre-1.0, solo-maintained community project. There is no guaranteed response time, uptime, data recovery, bookkeeping correctness, backward compatibility, or tax/accounting advice.
