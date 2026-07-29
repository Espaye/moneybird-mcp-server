# Releasing moneybird-mcp

Releases are **version-driven**: the version in `pyproject.toml` is the trigger.
A commit landing on `main` that bumps it makes
[`.github/workflows/release.yml`](../.github/workflows/release.yml) test, build,
publish to PyPI, tag the commit, and create the GitHub release. Pushes that don't
change the version skip all of that, so main stays green.

The one thing that stays manual is the `.mcpb` bundle for Claude Desktop: it vendors
its dependencies and is therefore platform-specific, so it is built on the dev
machine and attached to the release afterwards.

> **The bump is the release.** PyPI versions are immutable — once `0.3.0` is up you
> can yank it but never replace it. Treat the version bump as the point of no
> return, and keep it in its own commit so it is easy to hold back. If you want an
> explicit brake, add required reviewers to the `pypi` environment in the repo
> settings; the publish job then waits for your approval.

## 1. Version

- Bump `version` in **both** `pyproject.toml` and `mcpb/manifest.json`
  (`tests/test_server_entry.py::PackagingVersionSyncTests` fails if they differ, and
  the release workflow runs the suite before publishing, so a mismatch cannot ship).
- Use semver-ish judgement: new tools or packaging changes → minor bump;
  fixes only → patch.
- Check what is already published — the workflow compares against PyPI and simply
  does nothing if the version exists: `pip index versions moneybird-mcp` or
  <https://pypi.org/project/moneybird-mcp/#history>.

## 2. Verify

```
python -m pytest -q                 # full suite must pass
python scripts/healthcheck_readonly.py   # optional live read-only sanity sweep
```

## 3. Build (local check)

The workflow builds and publishes the wheel/sdist itself, so this step is just a
local dry run before you push the bump — plus the `.mcpb`, which only happens here:

```
python -m build                     # dist/moneybird_mcp-X.Y.Z-py3-none-any.whl + .tar.gz
python -m twine check dist/moneybird_mcp-X.Y.Z*
python scripts/check_dist_hygiene.py     # no .env / tokens / sqlite state / audit logs
python scripts/build_mcpb.py        # dist/moneybird-mcp-X.Y.Z-<platform>.mcpb
```

The `.mcpb` **is** still a manual build: it is platform-specific (dependencies are
vendored into it), so build it on the platform you're shipping for. The wheel/sdist
are pure Python and universal.

`twine check` must PASS for both artifacts, and `check_dist_hygiene.py` must exit 0
— it asserts the wheel holds nothing but the `moneybird` package and that neither
artifact carries a `.env`, OAuth tokens, the approvals DB, a sync cache, or an
audit log. Note it wants exactly one wheel and one sdist in `dist/`, so clear out
stale versions first.

## 4. Publish: push the bump to main

```
git push origin main
```

That's it. The workflow then, in order:

1. **check** — reads `pyproject.toml` and asks PyPI which versions exist. Version
   already published → the rest is skipped and the run ends green.
2. **build** — installs deps, runs `pytest -q`, `python -m build`, `twine check`,
   and `scripts/check_dist_hygiene.py`. Any failure here stops the release.
3. **publish-pypi** — uploads via Trusted Publishing in the `pypi` environment.
4. **tag-and-release** — creates tag `vX.Y.Z` at that commit and a GitHub release
   with the wheel + sdist attached.

Publishing happens under the pseudonymous identity — no real name anywhere in
package metadata (`authors` in `pyproject.toml` is the source of truth).

Watch a run with `gh run watch` or `gh run list --workflow=release.yml`.

### One-time setup on pypi.org — already configured

Trusted Publishing means there is no PyPI API token stored in the repo; PyPI mints
a short-lived token from the workflow's OIDC identity. This publisher was added on
2026-07-29 under *Manage project → Publishing → Add a new publisher* (GitHub):

| Field | Value |
| --- | --- |
| Owner | `Espaye` |
| Repository | `moneybird-mcp-server` |
| Workflow name | `release.yml` |
| Environment | `pypi` |

All four must match or PyPI rejects the upload. Falling back to a manual upload
(`python -m twine upload dist/moneybird_mcp-X.Y.Z*` with username `__token__`)
still works if the workflow is unavailable.

## 5. After publishing

- Smoke-test the published artifact in a clean environment:
  `pipx run moneybird-mcp --help` (or `uvx moneybird-mcp`) — it should start the
  stdio server and complain only about missing credentials.
- Attach the locally built `.mcpb` to the GitHub release the workflow created, for
  Desktop users (only useful once the repo is public):
  `gh release upload vX.Y.Z dist/moneybird-mcp-X.Y.Z-<platform>.mcpb`.
- Update the README install instructions if the install story changed.
