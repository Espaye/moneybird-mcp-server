# Public core architecture boundaries

The public package is standalone. Optional extensions register through installed
`moneybird_mcp.tools` entry points and consume the public `moneybird_mcp.api` interface.
The package must work with no extension installed. This describes existing boundaries,
not a newly accepted design.

| Change | Read |
|---|---|
| Client/provider behavior | `client.py`, `credentials.py`; [OAuth](oauth.md) and API snapshot/scopes |
| Public tools | `tools/`, `write_contracts.py`; [tool reference](tool-reference.md) and generated workflow catalogue |
| Guarded write | `safety.py`, executor/preflight/verifier; [deployment safety](deployment-and-safety.md), [threat model](threat_model.md) |
| Extension seam | `api.py`, `_extensions.py`; seam/provenance tests. API_VERSION declares compatibility, package minimum bounds cover added names |
| Local state / attachments | [data handling](data_handling.md), [lifecycle](data-lifecycle.md), [PDF boundary](reading_pdf_attachments.md) |

Runtime starts read-only. Writes require explicit opt-in and trusted supervised approval; uncertainty
never permits blind resend or fake verified success. Request-context mode never falls back to local
operator credentials and does not reuse the local approval kernel. Details and evidence live in the
linked boundary documents/tests, not a duplicated architecture inventory.
