# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and semantic
versioning while allowing pre-1.0 breaking changes.

## Unreleased

### Changed

- **Relicensed from MIT to MIT with the "Commons Clause" License Condition v1.0.**
  The project is now **source-available, not OSI-approved open source**. Inspecting,
  downloading, modifying, personal use, and internal use within your own organisation
  remain permitted free of charge. Selling the Software — including offering it as a paid
  or commercial hosted service, providing a managed service whose value derives entirely or
  substantially from its functionality, or repackaging it commercially as a competing
  product — now requires a separate commercial licence from the Licensor (`Espaye`).
  Enquiries go to the repository owner via a GitHub issue.
- Package metadata now declares the PEP 639 custom expression
  `LicenseRef-MIT-Commons-Clause-1.0` in `pyproject.toml` and `mcpb/manifest.json` instead
  of plain `MIT`. The combination has no standard SPDX identifier; the `LicenseRef-` form is
  deliberate and must not be replaced with a well-known id to satisfy tooling.
  `tests/test_licensing.py` pins the licence text and both metadata declarations so the
  project cannot silently revert to plain MIT.
- Added a licensing section to `README.md` and a contribution-licensing statement to
  `CONTRIBUTING.md`.

This change applies to this and all future versions. It does not retroactively withdraw the
MIT terms from copies already obtained under earlier releases.

Version `0.4.0` and earlier were published under plain MIT. On 2026-07-31 those artifacts were
withdrawn from normal public distribution: PyPI releases `0.1.0` through `0.4.0` were deleted,
as were the GitHub releases for `v0.3.0` and `v0.4.0` and their attached assets. The `v0.3.0`
and `v0.4.0` tags and the project's Git history were deliberately left intact. That withdrawal
is a distribution decision, not a revocation of rights already granted; copies may persist in
third-party mirrors and caches outside this project's control. Third-party dependencies keep
their own licences.

## 0.4.0 — 2026-07-31

### Security

- Removed import-time and working-directory `.env` discovery. Configuration files
  are now loaded only through an explicit `--env-file PATH`, and cannot override
  values supplied by the parent process.
- Defaulted the server and Desktop bundle to `read_only`; experimental writes now require
  an explicit local or authenticated single-user capability opt-in.
- Forced hosted request mode to live reads only: all writes, durable search sync/cache
  access, and attachment download/parsing are refused without credential fallback.
- Required bearer authentication for every network transport and an explicit trusted
  TLS-proxy acknowledgement for non-loopback binds.
- Bounded PDF downloads and extraction, removed current attachment-file retention, and
  hardened signed-storage redirect handling with validated DNS-to-TCP address pinning.
- Moved local PDF parsing into a disposable spawned process with a hard wall-clock
  timeout and Unix/Windows process-memory containment.
- Added capability-aware CodeQL, pinned Bandit scanning for private repositories, and
  scheduled full-history Gitleaks scanning; release artifacts now require reproducible
  builds and a CycloneDX SBOM, while newly published PyPI artifacts require verified
  publish provenance.
- Made numeric write inputs reject trailing junk, ambiguous separators, non-finite
  values, non-positive payments, and zero-value explicit bank links.
- Applied best-effort owner-only modes to explicitly configured data directories and
  current approval, audit, OAuth, sync, and FTS state files.
- Hardened the loopback gateway demo's identifiers and single-process JSON writes while
  documenting its plaintext, URL-key, and production no-go limitations.
- Added vulnerability reporting, supported-deployment guidance, and reconciled threat/data
  boundaries.
- Added direct transport tests for numeric-address TCP pinning with original-hostname
  TLS verification, closed mixed/non-public DNS answers, and fixed Python 3.14
  compatibility in the pinned HTTPS handler.

### Changed

- Added a scoped Ruff correctness/import gate and a 70% CI coverage regression floor.
- Replaced push-triggered publication with a default-branch-only manual release
  dispatch that requires the exact version and full commit SHA, refuses any
  existing PyPI version/tag/release, and never overwrites release assets.
- Added durable atomic write claims, typed outcomes, and action-specific postcondition
  checks where defined; partial or ambiguous execution is no longer presented as verified
  success, without claiming independent bookkeeping correctness.
- Added fallback semantic fingerprints to every staged action, captured live pre-state
  for repeatable contact/invoice workflow changes, and kept every executor exception
  unresolved when a dispatch may have occurred. Contact and several create/workflow
  actions now use independent post-write reads.
- Added a versioned WriteSpec registry for all approval executors, exact
  caller-controlled header/line comparison, complete batch preflights, and
  occurrence-aware payment, credit, link, and unlink verification.
- Persisted claim owner, attempt, execution phase, dispatch timing, and reconciliation
  evidence. Added a local-only operator reconciliation CLI; unresolved dispatched
  work remains duplicate-blocking until explicitly resolved.
- Bound repeatable bank link/unlink identities to the mutation occurrence so a
  verified unlink can be followed by a later legitimate relink/unlink cycle.
- Bound bank-link approvals to the exact target occurrence, require the
  independent post-read to match both booking id and type, expose only the three
  link types whose target can be proven, and preserve invoice-currency amounts
  instead of translating them as ledger base amounts.
- Made hosted search use live Moneybird reads with membership revalidation instead of local
  JSON/FTS state.
- Hardened release automation with a `main` ref guard, source/tag verification, dependency
  audit, exact artifact matrix tests, late tag re-verification, tested-candidate/PyPI
  digest comparison, and final GitHub tag/asset repair and verification from published
  artifacts. Partial or yanked PyPI state now fails closed; legacy repair uses helpers
  from the guarded workflow commit, whose review trust still depends on repository
  controls.
- Added a lowest-supported-dependency test lane, pinned the isolated build backend,
  required two-build hash equality, emitted a reproducible CycloneDX SBOM, and
  required cryptographic verification of Trusted Publishing attestations for new
  publications.
- Made the reproducibility check emit the exact compared artifacts, so CI and the
  release publisher cannot accidentally use a separately built, non-deterministic
  wheel or source distribution.
- Documented that production release readiness still requires a `main`-restricted,
  independently reviewed `pypi` environment and a protected `v*` tag ruleset.
- Declared Pydantic as a direct runtime dependency and aligned release/package metadata.

## 0.3.0

- Published the current compact-discovery Moneybird MCP beta with 77 tools, OAuth helpers,
  durable approvals, audit logging, incremental sync, attachment extraction, and CI/release
  automation.

Earlier changes predate this maintained changelog; consult Git history for details.
