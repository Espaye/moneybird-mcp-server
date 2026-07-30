# Releasing `moneybird-mcp`

Releases are version-driven and fail closed. A commit on `main` whose version is not
fully present on PyPI starts the release workflow. The workflow tests the source,
publishes exact artifacts with Trusted Publishing, verifies those artifacts after
download from PyPI, and creates or repairs the GitHub release from the verified PyPI
files.

The platform-specific `.mcpb` bundle remains a separate local build and manual
GitHub-release upload.

## 1. Prepare the version commit

- Bump `version` in both `pyproject.toml` and `mcpb/manifest.json`.
- Update `CHANGELOG.md`.
- Keep the version bump on `main`; the workflow refuses other refs.
- Check PyPI and existing `vX.Y.Z` tag/release state before pushing.

PyPI versions are immutable. Do not reuse a published version or assume a partially
published version can be repaired by uploading a replacement file.

## 2. Verify locally

```text
python -m pytest -q
python -m pip install -c requirements-minimum.txt -r requirements.txt pytest
python -m pytest -q
python scripts/assert_release_version.py X.Y.Z
python -m pip install build twine cyclonedx-bom==7.3.1
python scripts/check_reproducible_build.py --output-dir dist
python -m twine check dist/moneybird_mcp-X.Y.Z*
python scripts/check_dist_hygiene.py
python scripts/smoke_dist_install.py --expected-version X.Y.Z
python scripts/build_sbom.py --expected-version X.Y.Z
```

`scripts/check_dist_hygiene.py` expects exactly one wheel and one sdist in `dist/`.
Use a clean build directory so an older artifact cannot be selected accidentally.
The smoke test installs the built distributions into clean environments, checks
imports and version metadata, and invokes the CLI help path. It does not exercise
live Moneybird credentials.

An optional live read-only check is:

```text
python scripts/healthcheck_readonly.py
```

## 3. Push the bump to `main`

```text
git push origin main
```

Repository-wide release concurrency prevents two release state machines from running
at once. The workflow then:

1. asserts the source and manifest versions match;
2. inspects PyPI for the exact version and requires either no artifacts or exactly
   one non-yanked wheel plus one non-yanked sdist;
3. inspects any existing tag and GitHub release;
4. requires an existing tag to peel to a commit on `main`;
5. runs the full test matrix, lowest-supported-direct-dependency lane, and
   dependency audit;
6. builds one wheel and one sdist twice from fixed inputs and requires identical
   SHA-256 digests, then checks metadata/hygiene, smoke-tests the candidate, and
   generates a reproducible CycloneDX SBOM;
7. tests the exact wheel artifact across the supported Python matrix;
8. creates or verifies `vX.Y.Z` at the tested source SHA;
9. publishes through the `pypi` environment using OIDC Trusted Publishing and
   uploads PEP 740 attestations for both distributions;
10. re-verifies the tag inside the publish job immediately after any environment
    approval and before upload; after publication it verifies the tag again,
    downloads the exact version back from PyPI, compares its filenames and SHA-256
    digests with the tested candidate when one was built, cryptographically verifies
    each artifact's PyPI provenance against this GitHub repository, checks hygiene,
    and clean-installs both published artifacts;
11. re-verifies the tag immediately before release mutation, creates or repairs the
    GitHub release from those re-downloaded PyPI artifacts plus an SBOM regenerated
    from the exact published wheel, removes stale package/SBOM assets, clears
    draft/prerelease state, and finally checks the exact filenames and SHA-256
    digests again.

The tag is checked again after creation. A release cannot silently tag a different
commit from the source that produced the tested artifacts within that workflow run.
These YAML checks—including final tag and release-asset verification—are defense in
depth; repository settings must prevent later tag movement and restrict who can
approve publication.

## Recovery rules

- **Exactly one valid wheel and one valid sdist already exist on PyPI:** publishing
  is skipped, but published-artifact verification and GitHub-release repair still
  run.
- **Only one artifact exists, an artifact is yanked, or the file set is
  unexpected:** the workflow fails. Never rebuild an already published version.
  Repair a missing file only from the exact original tested workflow artifact under
  an explicit recovery procedure; if that artifact is unavailable, investigate and
  choose a new version. Do not construct a hybrid release.
- **A tag exists at a different SHA than the current trigger:** do not move it.
  Resume or rerun the original failed workflow for the tagged source.
- **PyPI is complete but the GitHub release is absent or incomplete:** rerun the
  workflow. It rebuilds the package-asset set from verified PyPI downloads and
  regenerates the SBOM from the exact published wheel, then deletes differently
  named stale wheel/sdist/SBOM assets.
- **A historical tag predates the current verification helpers:** recovery uses
  helpers from the guarded workflow commit while package bytes still come only from
  the original candidate or PyPI. If that helper provenance cannot be reproduced
  and reviewed, stop and perform an explicit manual recovery; never substitute a
  rebuild of the historical package source.
- **A pre-publish job fails:** fix the cause and rerun before any immutable upload.

Do not use a manual `twine upload` as the normal fallback. It bypasses the workflow's
source, artifact, and recovery checks.

## Trusted Publishing

The PyPI publisher must match:

| Field | Value |
| --- | --- |
| Owner | `Espaye` |
| Repository | `moneybird-mcp-server` |
| Workflow | `release.yml` |
| Environment | `pypi` |

Production release readiness requires both:

- a `pypi` environment deployment-branch policy restricted to `main`, with at least
  one required reviewer who is independent of the triggering workflow; and
- a repository ruleset protecting `v*` tags from creation, update, or deletion
  outside the intended release authority.

As of 2026-07-30, those environment and tag protections were not configured. The
workflow's ref/source/tag checks reduce accidental release drift
but cannot make an unprotected GitHub tag immutable or add an external publication
approval. Configure and verify both controls before treating the release path as
production-ready.

No long-lived PyPI API token is needed in the repository.

The separate `security.yml` workflow runs pinned Bandit and a full-history Gitleaks
scan on pushes, pull requests, and a weekly schedule. CodeQL also runs when the
repository is public, or when GitHub Code Security is enabled and the repository
variable `ENABLE_CODEQL` is explicitly set to `true`. All third-party Actions are
pinned to commit SHAs. Treat those workflow results, the dependency audit,
minimum-version lane, reproducibility check, SBOM, and provenance verification as
release signals; do not waive them by publishing manually.

## `.mcpb` bundle

Build the bundle on each platform being shipped:

```text
python scripts/build_mcpb.py
```

After the automated release is complete, upload that platform's bundle to the
matching GitHub release. The gateway demo is intentionally not included in the
wheel, sdist, or `.mcpb`.
