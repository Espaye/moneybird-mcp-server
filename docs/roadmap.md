# Public-core roadmap

This queue covers only the independently released public core. Source version is in
[pyproject.toml](../pyproject.toml); [release history](../CHANGELOG.md) separates Unreleased work
from published releases. The supported product remains local stdio, read-first with supervised
experimental writes. Hosted launch, model, billing and deployment decisions are owned privately;
M2/M3 hosted milestone statements formerly here were stale and are removed.

## Active tasks

| Task | Scope and acceptance |
|---|---|
| OAuth logout guidance / revocation follow-up | Documentation now acknowledges Moneybird's [revocation API](https://developer.moneybird.com/authentication/). `auth logout` still only deletes local credentials. Its runtime message and `REVOCATION_SUPPORTED=False` tests encode the old claim. Any automatic revoke-before-delete/failed-revoke lifecycle is a separate behavior change requiring owner direction and deterministic failure/idempotency/privacy tests. Do not flip the flag alone. |
| Compatibility regressions | Verify `general_document` attachment support and journal_entries/assets monthly report series against current dispatch; add failing-safe regressions before changing behavior. These are review leads, not proven fixes. |
| Payment follow-up | Current source includes the replacement `/payments` endpoint and balance/credit-settlement changes under **Unreleased**; do not describe them as present in the published 0.8.3 wheel. A release needs the existing release gates and explicit dispatch. |

No release, merge or live test is authorized by this queue. For a task, load its relevant
[architecture boundary](architecture.md), affected implementation and tests. General improvements
must preserve approval, confinement, read-only and ambiguous-write reconciliation invariants.
