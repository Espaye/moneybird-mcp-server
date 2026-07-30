# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and semantic
versioning while allowing pre-1.0 breaking changes.

## Unreleased

## 0.4.0 — 2026-07-30

### Security

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
  builds, a CycloneDX SBOM, and verified PyPI publish provenance.
- Made numeric write inputs reject trailing junk, ambiguous separators, non-finite
  values, non-positive payments, and zero-value explicit bank links.
- Applied best-effort owner-only modes to explicitly configured data directories and
  current approval, audit, OAuth, sync, and FTS state files.
- Hardened the loopback gateway demo's identifiers and single-process JSON writes while
  documenting its plaintext, URL-key, and production no-go limitations.
- Added vulnerability reporting, supported-deployment guidance, and reconciled threat/data
  boundaries.

### Changed

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
  artifacts. Partial or yanked PyPI state now fails closed; legacy repair requires
  reproducible, reviewed helper provenance.
- Added a lowest-supported-dependency test lane, pinned the isolated build backend,
  required two-build hash equality, emitted a reproducible CycloneDX SBOM, and
  required cryptographic verification of Trusted Publishing attestations.
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
