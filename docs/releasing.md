# Releasing `moneybird-mcp`

Releases are manual and fail closed. A push or merge never publishes. After the
version commit has passed review and landed on the default branch, a maintainer
must explicitly dispatch `release.yml` with the exact package version and full
40-character commit SHA. The workflow requires those inputs to match the selected
default-branch commit, tests that source, publishes its exact artifacts through
Trusted Publishing, verifies them after download from PyPI, and creates the GitHub
Release once from those verified files.

The platform-specific `.mcpb` bundle remains a separate local build and manual
GitHub-release upload.

## 1. Prepare and review the version commit

- Bump `version` in both `pyproject.toml` and `mcpb/manifest.json`.
- Update `CHANGELOG.md`.
- Open a pull request into the default branch and require the applicable CI and
  security checks before merge.
- Check PyPI and existing `vX.Y.Z` tag/release state before dispatching.

PyPI versions are immutable. Do not reuse a published version or assume a partially
published version can be repaired by uploading a replacement file.

## 2. Verify locally

```text
ruff check moneybird_mcp scripts tests moneybird_mcp_server.py
python -m compileall moneybird_mcp scripts
python -m pytest -q
python -m pytest --cov=moneybird_mcp --cov-report=term-missing --cov-fail-under=70
bandit --quiet --recursive moneybird_mcp scripts moneybird_mcp_server.py --severity-level medium --confidence-level medium
python -m pip_audit -r requirements.txt
python -m pip install -c requirements-minimum.txt -r requirements.txt pytest
python -m pytest -q
python scripts/assert_release_version.py X.Y.Z
python -m pip install build twine cyclonedx-bom==7.3.1
python scripts/check_reproducible_build.py --output-dir dist
python -m twine check dist/moneybird_mcp-X.Y.Z*
python scripts/check_dist_hygiene.py
python scripts/smoke_dist_install.py --expected-version X.Y.Z
python scripts/build_sbom.py --expected-version X.Y.Z
gitleaks git --redact --verbose .
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

## 3. Dispatch the exact default-branch commit

From the GitHub Actions UI, select **Release**, choose **Run workflow** on the
default branch, and enter both inputs. The equivalent authenticated CLI command is:

```console
gh workflow run release.yml --ref main \
  -f version=X.Y.Z \
  -f commit_sha=<full-40-character-main-commit-sha>
```

Repository-wide release concurrency prevents two release state machines from
running at once. The workflow then:

1. requires a manual dispatch from the repository's default branch;
2. requires the version input to equal both package and manifest metadata, and
   the commit input to be the exact full SHA selected by the dispatch;
3. refuses to continue if the PyPI version, Git tag, or GitHub Release already
   exists;
4. runs the full test matrix, lowest-supported-direct-dependency lane, and
   dependency audit;
5. builds one wheel and one sdist twice from fixed inputs and requires identical
   SHA-256 digests, then checks metadata/hygiene, smoke-tests the candidate, and
   generates a reproducible CycloneDX SBOM;
6. passes those exact tested artifacts between jobs and tests the wheel across
   the supported Python matrix;
7. creates `vX.Y.Z` once at the exact tested source SHA and re-verifies it;
8. publishes through the `pypi` environment using OIDC Trusted Publishing and
   uploads PEP 740 attestations for both distributions;
9. re-verifies the tag inside the publish job immediately after any environment
    approval and before upload; after publication it verifies the tag again,
   downloads the exact version back from PyPI, compares filenames and SHA-256
   digests with the tested candidate, cryptographically verifies each artifact's
   provenance against this repository, checks hygiene, and clean-installs both
   published artifacts;
10. re-verifies the tag immediately before release mutation, creates the GitHub
    Release once from those re-downloaded PyPI artifacts plus an SBOM regenerated
    from the exact published wheel, and finally checks the exact tag, filenames,
    and SHA-256 digests again.

The tag is checked again after creation. A release cannot silently tag a different
commit from the source that produced the tested artifacts within that workflow run.
These YAML checks—including final tag and release-asset verification—are defense in
depth; repository settings must prevent later tag movement and restrict who can
approve publication.

## Failure and recovery rules

- **A pre-publish job fails:** fix the cause on a new reviewed commit and dispatch
  that exact commit only while the version and tag remain unused.
- **The tag or GitHub Release appears after the initial check:** later mutation
  steps fail rather than moving, editing, deleting, or overwriting it.
- **Any PyPI file is uploaded:** stop. The version is immutable and this workflow
  will refuse every attempt to reuse it. Record exactly what exists, investigate,
  and choose the next legal version. Never construct a hybrid release.
- **PyPI succeeds but later verification or GitHub Release creation fails:** stop
  and use an explicit reviewed recovery procedure based only on the exact original
  workflow artifacts and PyPI files. The normal workflow intentionally refuses an
  already published version.

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

For this solo-maintainer beta, the `pypi` environment must accept deployments only
from protected `main`, and a `v*` tag ruleset must prevent tag update and deletion
while allowing the release workflow to create a new tag. Protected `main`, the
manual version/SHA dispatch, the restricted environment, exact-artifact handoff,
and the deliberate pre-publication checkpoint form the release brake.

There is no independent human deployment reviewer in the solo-maintainer setup.
That is a documented residual beta limitation, not a claim of dual control. Add an
independent required reviewer and prevent self-review if a suitable maintainer
becomes available without making releases impossible.

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
matching GitHub release.
