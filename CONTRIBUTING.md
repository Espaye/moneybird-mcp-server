# Contributing

Thank you for helping improve Moneybird MCP.

## Licensing of contributions

This project is source-available, not OSI-approved open source: it is distributed under the
MIT License with the "Commons Clause" License Condition v1.0. See [`LICENSE`](LICENSE) and the
licensing section of [`README.md`](README.md#8-licensing).

By opening a pull request you confirm that:

1. your contribution is submitted under the project's current licence, and you agree it may be
   distributed by the project under that licence and under any later licence the project adopts
   for subsequent releases;
2. you have the right to submit it — it is your own work, or you have the necessary permission
   from the rights holder (for example your employer), and it does not knowingly include
   third-party code under incompatible terms;
3. any third-party code or dependency you add is identified with its own licence in the pull
   request.

Contributors keep the copyright in their own contributions. Enquiries about a separate
commercial licence go to the repository owner via an issue.

## Development setup

Use Python 3.11 or newer in a fresh virtual environment:

```powershell
python -m pip install -r requirements.txt
python -m pip install ruff==0.16.1 pytest-cov==7.1.0
ruff check moneybird gateway scripts tests moneybird_mcp_server.py
python -m pytest -q
python -m pytest --cov=moneybird --cov=gateway --cov-report=term-missing --cov-fail-under=70
python -m pip install -c requirements-minimum.txt -r requirements.txt pytest
python -m pytest -q
```

No real Moneybird credential is needed for the test suite. Keep `.env`, OAuth stores,
approvals, audit logs, sync indexes, FTS databases, and downloaded attachments out of commits
and test fixtures.

When workflow metadata changes, regenerate and verify the catalogue:

```bash
python scripts/render_workflow_catalogue.py
python scripts/render_workflow_catalogue.py --check
```

The initial coverage gate is a 70% regression floor, slightly below the measured
baseline so platform-specific branches do not make the gate flaky. Raise it after
merging focused behavioural tests for high-risk modules; never add assertion-free
tests solely to increase the percentage. Ruff intentionally starts with Pyflakes,
import ordering, and core syntax/error rules. Broader style and complexity families
remain outside the gate until they can be adopted in small reviewed changes without
weakening deliberate fail-closed handling.

## Change expectations

1. Open a focused issue or pull request with the user-visible behavior and risk.
2. Add deterministic tests, including negative/adversarial cases.
3. Preserve tenant and administration confinement at every boundary.
4. For writes, define the action precondition, immutable preview/payload representation,
   verifier, idempotency key, partial-failure behavior, and ambiguous-result reconciliation.
5. Run the full suite and distribution-hygiene check.
6. Update README, threat/data documentation, and changelog when behavior or durable state
   changes.

## Financial-safety invariants

A prompt is never sufficient enforcement. Changes must not:

- permit a model to manufacture trusted human confirmation;
- allow one approval or semantic idempotency key to reach Moneybird twice concurrently;
- record failed, partial, unverified, or ambiguous work as verified success;
- retry a write after dispatch may have started without reconciliation;
- use caller-controlled paths, administration IDs, cache ownership, or telemetry labels;
- fall back to operator credentials in a hosted request;
- expose secrets or bookkeeping content in logs, URLs, fixtures, or packages.

Use `apply_patch`-sized, reviewable migrations. A migration that has observed new writes must
roll forward or enter read-only reconciliation mode; it must not restore legacy write state.

## Pull-request checklist

- [ ] Focused tests pass.
- [ ] Full `python -m pytest -q` passes.
- [ ] Minimum-dependency tests pass when dependency bounds or used APIs change.
- [ ] `scripts/check_reproducible_build.py` passes for release/build changes.
- [ ] New dependency is direct, bounded, and justified.
- [ ] No credential or local state was added.
- [ ] Migration and rollback behavior are documented.
- [ ] Security claims are no stronger than mechanical enforcement.
