# Agent instructions

This repository is the independently released public `moneybird-mcp` core, source-available under
MIT + Commons Clause. Do not publish proprietary advanced/hosted source, secrets or customer data.
Work in an isolated branch/worktree from remote main; preserve concurrent work; open a focused PR.
No merge, package publication, live Moneybird write or credential/runtime change without explicit authorization.

Load [docs index](docs/README.md), the relevant scoped task in [roadmap](docs/roadmap.md), then
[architecture boundaries](docs/architecture.md) and affected code/tests. Hosted plans/state live
in their private repository, not here. Historical private monolith refs are never a development/push base.

Keep local approval/idempotency/reconciliation, request-context credential confinement, read-only
policy, privacy and distribution hygiene intact. Public core never depends on advanced or hosted.

For prose/checker changes run `python scripts/check_docs.py` and checker tests. For behavior run
Ruff on changed files and focused positive/adversarial tests; broaden to the full suite when shared
safety/client/lifecycle impact cannot be bounded. Dependency/API-bound changes need minimum-bound
checks; releases retain every [release gate](docs/releasing.md). Existing required CI remains mandatory.
Workflow metadata changes require `python scripts/render_workflow_catalogue.py --check`.

Update only the fact's authority; relevant versions must agree mechanically. Keep durable knowledge in the
operator-configured shared project memory store; do not commit local paths or personal details.
