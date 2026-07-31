# Independent release validation: 0.4.0

Validation date: 2026-07-31

Verdict: **NO-GO**

> Historical pre-refactor snapshot. This report records the independent
> validation state before the dedicated `release/0.4.0` preparation branch
> replaced push-triggered publication and before live repository controls were
> configured. Its local artifact hashes are evidence for that snapshot, not the
> final release artifacts or current control status.

The local read-only candidate passed its source, safety, packaging, secret-scan,
and artifact checks after the corrections recorded below. The source-disclosure
gate therefore passes: no credential or private-data blocker prevents making the
repository public. It is not safe to merge, push, or manually rerun for
publication yet: the live `pypi` GitHub environment has no deployment-branch
restriction or required reviewer, allows administrator bypass, and the release
workflow publishes automatically on a successful push to `main`. Repository
rulesets, branch protection, and CodeQL are also unavailable or not enabled in
the current private-repository plan/configuration.

This is a release-control NO-GO, not a source-test failure.

## Scope and supported profile

The validated product profile is:

- local stdio transport;
- local credential handling;
- mechanically read-only by default;
- experimental local writes only after explicit process configuration;
- localhost gateway demonstration only, not a production hosted service.

No hosted product or web interface was built.

## Exact source state

The validation started on branch `main` at commit
`f1b5cd9a73c0d6ae7c4a695ddf19bf1ca5a62d29`. The repository is a full clone,
not a shallow clone.

The initial working tree contained 51 modified tracked files, no added or deleted
tracked files, and these three untracked candidate files:

- `docs/release_workflow_refactor_plan.md`
- `docs/security_remediation_status_2026-07-31.md`
- `tests/test_env_file_boundary.py`

The initial tracked diff was 51 files, 530 insertions, and 248 deletions. All
changes were reviewed as candidate work; no unrelated user change was identified.
Nothing was discarded or overwritten.

Validation found genuine defects and added minimal local corrections to already
modified files plus `docs/releasing.md` and `scripts/smoke_dist_install.py`. This
report is new validation evidence. The exact candidate is the complete unstaged
diff from the commit above together with the untracked candidate files named by
`git status --short`; nothing was staged. The final status and diff summary were
captured after writing this report.

Initial state commands:

```console
git status --short
git diff --stat
git diff --check
git diff
git log -5 --oneline
git branch --show-current
git rev-parse HEAD
git rev-parse --is-shallow-repository
```

Initial history:

```text
f1b5cd9 Bump version to 0.4.0
f9cfc98 Ignore local virtualenvs including verification venvs
f288e0b Fix security workflow checks
524593a Harden financial writes and release security
89c7962 Clarify sandboxed GitHub auth checks
```

Final working-tree snapshot after validation cleanup:

```text
 M .env.example
 M .github/workflows/ci.yml
 M .gitignore
 M CHANGELOG.md
 M CLAUDE.md
 M CONTRIBUTING.md
 M README.md
 M docs/data_handling.md
 M docs/hosted_gateway_design.md
 M docs/releasing.md
 M moneybird/client.py
 M moneybird/config.py
 M moneybird/formatting.py
 M moneybird/invoicing.py
 M moneybird/oauth.py
 M moneybird/purchase_reconcile.py
 M moneybird/purchase_review.py
 M moneybird/server.py
 M moneybird/sync.py
 M moneybird/tools/__init__.py
 M moneybird/tools/_writes.py
 M moneybird/tools/approvals.py
 M moneybird/tools/bank.py
 M moneybird/tools/contacts.py
 M moneybird/tools/core.py
 M moneybird/tools/ledger.py
 M moneybird/tools/payments.py
 M moneybird/tools/purchases.py
 M moneybird/tools/reference.py
 M moneybird/tools/reports.py
 M moneybird/tools/sales.py
 M moneybird/tools/sales_batches.py
 M moneybird/tools/workflows.py
 M moneybird_mcp_server.py
 M pyproject.toml
 M scripts/build_sbom.py
 M scripts/check_reproducible_build.py
 M scripts/healthcheck_readonly.py
 M scripts/oauth_login.py
 M scripts/reclassify_remaining_uncategorized_2025_2026.py
 M scripts/reclassify_uncategorized_expenses_2026.py
 M scripts/smoke_dist_install.py
 M tests/test_attachments.py
 M tests/test_cache_authorization.py
 M tests/test_capabilities.py
 M tests/test_moneybird_helpers.py
 M tests/test_path_confinement.py
 M tests/test_purchase_reconcile.py
 M tests/test_purchase_review.py
 M tests/test_server_entry.py
 M tests/test_state_permissions.py
 M tests/test_workflows.py
 M tests/test_write_contract_regressions.py
?? docs/release_validation_0.4.0.md
?? docs/release_workflow_refactor_plan.md
?? docs/security_remediation_status_2026-07-31.md
?? tests/test_env_file_boundary.py
```

This is 53 modified tracked files, no added or deleted tracked files, and four
untracked files including this report. The tracked diff is 53 files, 569
insertions, and 261 deletions. The validation environments were removed and do not
appear in the final working tree.

`git diff --check` found no whitespace error. Git emitted only working-copy
LF-to-CRLF conversion notices.

## Corrections made during validation

The following local corrections were required:

1. `moneybird/config.py` now parses and validates an explicitly selected env file
   completely before changing `os.environ`. An invalid later variable therefore
   cannot leave a partially applied security configuration. Parent-process values
   still win through `setdefault`.
2. `scripts/oauth_login.py` now uses the same default state directory as the stdio
   server (`~/.moneybird-mcp`) when no nonblank data-directory environment value
   is supplied.
3. The two reclassification scripts now import `get_client` from
   `moneybird.client`, instead of the legacy entry module that no longer exports
   it.
4. `tests/test_env_file_boundary.py` gained hostile-working-directory,
   exact-explicit-path, no-partial-mutation, parent-token precedence, and OAuth
   state-default coverage using synthetic values only.
5. `scripts/smoke_dist_install.py` now proves that an installed artifact starts
   with local credentials, stdio transport, and read-only capability.
6. Release documentation was narrowed to match actual provenance recovery,
   artifact-type validation, and helper-review behavior.
7. `pyproject.toml` excludes release-validation reports from the source
   distribution. This avoids a report/artifact-hash self-reference while keeping
   the report in the repository.

All affected checks and both complete test environments were rerun after these
changes.

## Commands run

The commands below are written without private machine paths or temporary
directory names. `python` and installed command names refer to the active
dedicated validation environment. `<clean-output-dir>` was a newly created empty
directory and was not the repository's existing `dist/`.

### Normal/current dependency environment

```console
python -m venv <current-validation-venv>
python -m pip install --upgrade pip
python -m pip install ruff==0.16.1 pytest pytest-cov==7.1.0 bandit==1.9.4 pip-audit build twine cyclonedx-bom==7.3.1
python -m pip install -e ".[pdf]"
ruff check moneybird gateway scripts tests moneybird_mcp_server.py
python -m compileall moneybird gateway scripts
python -m pytest -q
python -m pytest --cov=moneybird --cov=gateway --cov-report=term-missing --cov-fail-under=70
bandit --quiet --recursive moneybird gateway scripts moneybird_mcp_server.py --severity-level medium --confidence-level medium
python -m pip_audit -r requirements.txt
python scripts/assert_release_version.py 0.4.0
git diff --check
```

### Env-file and boundary tests

```console
python -m pytest -q tests/test_env_file_boundary.py tests/test_server_entry.py tests/test_capabilities.py tests/test_credential_modes.py
```

The hostile subprocess fixture contained exactly:

```env
MONEYBIRD_CAPABILITY_MODE=write_enabled
MONEYBIRD_ADMINISTRATION_ID=999
MCP_TRANSPORT=http
MCP_HOST=0.0.0.0
MCP_PORT=9999
MCP_AUTH_TOKEN=attacker-secret
MCP_TRUSTED_TLS_PROXY=true
MONEYBIRD_CREDENTIAL_MODE=hosted_request_only
MONEYBIRD_ACCESS_TOKEN=attacker-token
MONEYBIRD_MCP_DATA_DIR=attacker-directory
MCP_TOOL_DISCOVERY=full
```

The file had no effect when merely present in the subprocess working directory.
The same file took effect only when its exact regular-file path was passed through
`--env-file`. A synthetic parent token was not replaced by the selected file.

### Minimum dependency environment

The minimum suite ran in a separate Python 3.11.15 environment:

```console
python -m venv <minimum-validation-venv>
python -m pip install --upgrade pip
python -m pip install -c requirements-minimum.txt -r requirements.txt pytest
python -m pytest -q
```

Resolved minimum constraints included:

| Package | Installed version |
| --- | ---: |
| Python | 3.11.15 |
| fastmcp | 3.4.0 |
| pydantic | 2.11.7 |
| httpx | 0.28.1 |
| uvicorn | 0.35.0 |
| chardet | 5.0.0 |
| pypdf | 5.0.0 |

An attempted minimum install under Python 3.14 was not treated as the minimum
result: the pinned `pydantic-core` did not provide a compatible wheel and the
machine lacked the native compiler needed for a source build. Running the declared
lowest supported Python, 3.11, proved that this was an interpreter/toolchain
combination issue rather than a project test failure.

### Attachment compatibility

The complete suites above exercised attachments under Python 3.11 and 3.14.
Focused attachment suites filled the intermediate-version compatibility coverage:

```console
python3.12 -m pytest -q tests/test_attachments.py
python3.13 -m pytest -q tests/test_attachments.py
```

### Artifact build and inspection

The scripts' `--help` output was inspected before using their supported path
arguments.

```console
python scripts/check_reproducible_build.py --help
python scripts/check_dist_hygiene.py --help
python scripts/smoke_dist_install.py --help
python scripts/build_sbom.py --help
python scripts/check_reproducible_build.py --output-dir <clean-output-dir>
python -m twine check <clean-output-dir>/*
python scripts/check_dist_hygiene.py <clean-output-dir>
python scripts/smoke_dist_install.py --dist-dir <clean-output-dir> --expected-version 0.4.0
python scripts/build_sbom.py --dist-dir <clean-output-dir> --output-dir <clean-output-dir> --expected-version 0.4.0
```

Archive member lists, counts, metadata, SHA-256 hashes, and the SBOM JSON were
then inspected directly. The clean directory contained exactly one wheel and one
source distribution before SBOM generation, and those two artifacts plus one SBOM
afterward.

### Full-history secret scan

The official Gitleaks 8.30.1 Windows archive was checked against its published
SHA-256 checksum before use:

```console
gitleaks version
gitleaks git --redact --verbose .
git rev-list --all --count
git tag --list
git ls-files
```

### Dependabot and live GitHub read-only checks

```console
git status --short .github
git log --all -- .github/dependabot.yml
git diff -- .github/dependabot.yml
git check-ignore -v .github/dependabot.yml
gh auth status
gh api repos/Espaye/moneybird-mcp-server
gh api repos/Espaye/moneybird-mcp-server/environments/pypi
gh api repos/Espaye/moneybird-mcp-server/rulesets
gh api repos/Espaye/moneybird-mcp-server/branches/main/protection
gh api repos/Espaye/moneybird-mcp-server/actions/permissions
gh api repos/Espaye/moneybird-mcp-server/actions/permissions/workflow
gh api repos/Espaye/moneybird-mcp-server/private-vulnerability-reporting
gh api repos/Espaye/moneybird-mcp-server/vulnerability-alerts
gh api repos/Espaye/moneybird-mcp-server/automated-security-fixes
gh api repos/Espaye/moneybird-mcp-server/dependabot/alerts
gh api repos/Espaye/moneybird-mcp-server/code-scanning/alerts
gh api repos/Espaye/moneybird-mcp-server/actions/variables/ENABLE_CODEQL
gh run list --repo Espaye/moneybird-mcp-server
gh run view 30584312107 --repo Espaye/moneybird-mcp-server
gh run view 30584312139 --repo Espaye/moneybird-mcp-server
gh run view 30584312153 --repo Espaye/moneybird-mcp-server
```

Trusted Publishing was not changed or inferred from repository files. Its PyPI
administrative configuration was not observable through the available read-only
GitHub interface.

## Check results

| Check | Status | Result |
| --- | --- | --- |
| Ruff 0.16.1 | PASS | All checks passed |
| Compileall | PASS | `moneybird`, `gateway`, and `scripts` compiled |
| Current full suite | PASS | 279 passed, 142 subtests, 0 skipped, 2 warnings |
| Coverage suite | PASS | 72.48%; threshold 70%; 279 passed, 142 subtests, 0 skipped, 2 warnings |
| Bandit 1.9.4 | PASS | Exit 0; no medium/high issue |
| pip-audit | PASS | No known vulnerabilities in `requirements.txt` |
| Release version | PASS | Package/release metadata agrees on 0.4.0 |
| Diff check | PASS | No whitespace errors |
| Minimum full suite | PASS | 279 passed, 142 subtests, 0 skipped, 2 warnings |
| Python 3.12 attachments | PASS | 26 passed, 17 subtests |
| Python 3.13 attachments | PASS | 26 passed, 17 subtests |
| Reproducible build | PASS | Two independent builds had identical hashes |
| Twine metadata | PASS | Wheel and sdist passed |
| Distribution hygiene | PASS | No credentials, runtime state, caches, attachments, audit logs, or `.env` |
| Clean installed smoke | PASS | Version, CLI help, PDF extra, local/stdio/read-only defaults passed |
| SBOM | PASS | CycloneDX 1.6, 72 components, generated from the validated wheel |
| Full-history Gitleaks | PASS | 33 commits and all reachable history scanned; no leak |
| Current tracked filenames | PASS | No committed credential/runtime-state file found |
| Configuration boundary | PASS | No implicit `.env`; explicit regular file only; parent wins; atomic invalid-file failure |
| Write-safety invariants | PASS | All required capability, approval, membership, and result-state invariants preserved |
| Documentation consistency | PASS | Local beta/gateway/PDF/migration/security claims aligned after corrections |
| GitHub publication controls | FAIL | The `pypi` environment has no trustworthy approval boundary |
| GitHub repository protections | FAIL/UNAVAILABLE | Rulesets/branch protection unavailable on the current private plan |
| CodeQL | FAIL | Security run skipped CodeQL; repository code scanning is not enabled |
| Trusted Publishing | UNAVAILABLE | PyPI-side configuration could not be observed |

The two test warnings were an upstream Starlette/httpx test-client deprecation and
a local pytest cache warning during concurrent isolated validators. Neither
affected assertions or artifacts. Bandit also printed informational notices for
intentional `nosec` annotations around reviewed SQL construction. Git printed
LF-to-CRLF working-copy notices; `git diff --check` remained clean.

## Security and semantic review

Three independent reviews were reconciled with the primary inspection.

### Configuration and credentials

- Importing `moneybird` does not search for or load `.env`.
- Only explicit `--env-file PATH` loading exists, and the resolved target must be
  a regular file.
- The selected file is completely parsed and validated before mutation.
- Parent-process values take precedence.
- Security-sensitive configuration is established before dependent modules are
  imported by the entrypoints.
- Stdio and OAuth default state both resolve to `~/.moneybird-mcp`.
- OAuth token-source precedence and missing-secret errors are fail-closed.
- Telemetry does not expose credentials or Moneybird identifiers.

### Write safety and Ruff drift

AST comparison against `HEAD` showed that Ruff-only edits in formatting,
invoicing, reconciliation, review, sync, and all changed tool modules were import
or formatting changes. `moneybird.tools` exported the same 202 names with no
identity difference. Financial endpoint behavior in `moneybird.client` was
unchanged apart from attachment hardening.

Mechanical invariants were verified:

- read-only is the clean-process default;
- hosted mode refuses all writes;
- experimental local writes require explicit process configuration;
- the capability check precedes approval claiming;
- approval claiming remains atomic across processes;
- unresolved duplicate fingerprints remain blocked;
- failed, partial, ambiguous, or unverified work never records success;
- live administration membership is checked before cache access;
- administration and filesystem paths remain confined;
- an approval identifier is not represented as independent human consent.

### Attachments

- TCP connects to a validated numeric address while TLS/SNI verifies the original
  hostname.
- Moneybird bearer authorization is sent only to the initial Moneybird request,
  never to the signed storage URL.
- Every DNS result must be globally routable; a mixed public/private result fails
  closed.
- Private, loopback, link-local, multicast, reserved, site-local, malformed, and
  otherwise non-global addresses are rejected.
- Only HTTPS, no credentials, no fragment, and default/443 ports are accepted.
- Redirect processing is manual and a second redirect is rejected.
- Content length and streaming reads are bounded.
- Attachment size, PDF page count, extracted text, worker time, and worker memory
  are bounded.
- Worker termination, join, and kill cleanup prevents a failed worker from
  hanging the server.
- Hosted-request credential mode refuses attachment processing before obtaining a
  client.
- Tests used synthetic responses and controlled sockets; no arbitrary public host
  was contacted.

## Dependabot discrepancy

`.github/dependabot.yml` is tracked at `HEAD`, was introduced by commit `524593a`,
is not ignored, has no working-tree diff, and was never deleted in this candidate.
No restoration or replacement was appropriate.

The file schedules weekly updates for both Python dependencies and GitHub Actions
at repository root, with a dependency label and an open-request limit of five.
That version-update configuration is satisfactory. It is distinct from live
Dependabot security services: GitHub reports vulnerability alerts and Dependabot
alerts disabled, and automated security fixes/security updates disabled.

## Artifact evidence

These hashes identify the local validation artifacts. They are not promises of the
future published hashes: the reproducible build uses the source commit timestamp,
so a future merge commit can legitimately produce different digests.

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| `moneybird_mcp-0.4.0-py3-none-any.whl` | 172900 | `65383314cd0dc25a4705bbb2e47c9f6f6b3a989c7c90f5719f2814181a072f3b` |
| `moneybird_mcp-0.4.0.tar.gz` | 1068548 | `c0672421877fc5e5c9a07fd1c714e714b6a4274fed9585b2b93a52ea9a34c171` |
| `moneybird-mcp-0.4.0.cdx.json` | 94129 | `bde5950b94960d1ec8d6e7ffeb64e9b50a9f52e1d0fc4143320816782f7dc7b3` |

Both artifact builds used `SOURCE_DATE_EPOCH=1785447589` and produced the hashes
above. The SBOM is CycloneDX 1.6 with 72 dependency components and was selected
from the exact clean-output wheel. The validation report is intentionally absent
from the sdist to avoid self-referential artifact hashes.

## Full-history secret scan

The repository is a full clone with 33 reachable commits and one reachable tag.
Gitleaks 8.30.1 scanned approximately 1.44 MB of reachable Git history using
redaction and found no leak. The downloaded scanner archive matched the official
SHA-256:

```text
d29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e
```

Tracked filenames were also inspected for local env, credential, approval,
attachment, cache, database, and audit-state patterns. Matches were only expected
examples, source, documentation, and tests; no committed local state was found.

## Live GitHub controls

Read-only authenticated inspection produced:

| Control | Status | Evidence |
| --- | --- | --- |
| `pypi` environment exists | Verified but misconfigured | No protection rules; no branch policy; administrators can bypass |
| `main`-only deployment | Missing | `deployment_branch_policy` is null |
| Independent deployment reviewer | Missing | `protection_rules` is empty |
| Prevent self-review | Missing/unverified | No reviewer rule exists |
| Administrator bypass disabled | Missing | `can_admins_bypass` is true |
| Protected `v*` tags | Unavailable/missing | Rulesets API requires GitHub Pro or a public repository |
| Branch protection/required checks | Unavailable/missing | Protection API requires GitHub Pro or a public repository |
| Private vulnerability reporting | Missing/unavailable | Endpoint unavailable while repository is private |
| Dependabot config | Verified and satisfactory | Tracked weekly pip and Actions configuration |
| Vulnerability alerts | Missing | Disabled |
| Dependabot security updates | Missing | Disabled |
| Dependabot alerts | Missing | API explicitly reports alerts disabled |
| Latest CI for `f1b5cd9` | Verified and satisfactory | Run 30584312107 succeeded |
| Latest Security for `f1b5cd9` | Partial | Bandit and full-history Gitleaks succeeded; CodeQL skipped |
| Code scanning/CodeQL | Missing | Code scanning is disabled and `ENABLE_CODEQL` is absent |
| Actions default workflow permissions | Verified and satisfactory | Read-only default; workflows cannot approve pull-request reviews |
| Actions allow-list/pin enforcement | Weak | All actions allowed; SHA pinning not enforced by settings |
| Workflow action references | Verified and satisfactory | Candidate workflows pin action references |
| Trusted Publishing | Inaccessible/unverified | PyPI-side owner/repository/workflow/environment binding not observable |

No GitHub or PyPI setting was changed.

## Release-trigger analysis

The exact existing `f1b5cd9` push already started the release workflow. Its
candidate build job failed on one multiprocessing timing test
(`SafetyKernelTests.test_same_approval_is_claimed_once_across_processes`): 1
failed, 264 passed, 1 skipped, and 125 subtests. The build, tag, publish, and
release jobs were skipped. The normal CI workflow on the same commit passed,
which means a manual rerun of this old release could advance farther. It must not
be rerun while publication is unprotected.

A commit of the corrected tree pushed or merged to `main` would:

1. start CI, Security, and Release independently and concurrently;
2. see version 0.4.0 absent from PyPI and `v0.4.0` absent from GitHub;
3. run the Release workflow's own test matrix, audit, minimum-dependency, clean
   build, smoke, hygiene, reproducibility, and SBOM checks;
4. create the lightweight `v0.4.0` tag at the tested commit;
5. enter the `pypi` environment and publish via OIDC;
6. verify published hashes/provenance and then create the GitHub release.

The Release workflow uses a clean runner and passes the exact tested artifact
between jobs; historical repository `dist/` files cannot be selected. Its
permissions are narrowly scoped by job. However, it does not wait for the
concurrent CI/Security workflows and it does not repeat Ruff, coverage, Bandit,
Gitleaks, or CodeQL inside the release workflow. With the current unprotected
environment, publication can occur without independent manual review and before
those concurrent checks finish.

A future tag-creation ruleset must not accidentally block the intended release
identity. If tag creation is restricted, grant a narrowly scoped bypass to the
dedicated release identity and test it. A broad GitHub Actions App bypass would
also let changes to the workflow weaken tag protection unless the workflow and
`main` are themselves protected.

## Required controls before merge or rerun

1. In **Settings > Environments > pypi**:
   - restrict deployment branches/tags to `main`;
   - add an independent required reviewer;
   - prevent self-review;
   - disable administrator bypass.
2. Upgrade the private repository to a plan supporting rulesets, or make it public
   before configuring protections. Add an active tag ruleset for `v*` that blocks
   tag update and deletion. If creation is restricted, authorize only the intended
   release identity.
3. Protect `main`: require pull requests and require the CI and Security checks
   before merge, with no administrator bypass. Include Ruff, coverage, tests,
   Bandit, Gitleaks, and enabled CodeQL checks.
4. Under **Settings > Code security and analysis**, enable vulnerability alerts,
   Dependabot security updates, Dependabot alerts, and code scanning/CodeQL. After
   publication, enable private vulnerability reporting and rerun the Security
   workflow with CodeQL required to succeed.
5. Under **Settings > Actions > General**, retain read-only default workflow
   permissions and narrow allowed actions to GitHub/verified or an explicit list.
   Enable required full-SHA pinning if supported.
6. In PyPI Trusted Publishing, independently verify the exact binding:
   owner `Espaye`, repository `moneybird-mcp-server`, workflow
   `release.yml`, environment `pypi`.
7. Do not manually rerun release run 30584312139. After the controls above exist,
   review and approve only a release run for the corrected, intended commit SHA.

## Final conclusion

The corrected local source is technically suitable for the stated local
read-only beta profile and passed independent source, safety, packaging,
compatibility, artifact, and full-history secret validation. It is ready for
public source disclosure. Making the repository public can also unlock the
free-plan security controls that are currently unavailable; configure them
immediately after the visibility change and before any push, merge, or release
rerun. The repository is not yet safe to merge as a publishing source because the
live publication approval boundary and required repository security controls are
missing.

No real Moneybird data or credentials were used. All security-boundary tests used
synthetic values. Nothing was committed, pushed, merged, tagged, published,
released, uploaded, or changed in GitHub/PyPI settings during this validation.
