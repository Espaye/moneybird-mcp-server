# Releasing moneybird-mcp

Checklist for cutting a release: a wheel + sdist for PyPI and a `.mcpb` bundle for
Claude Desktop. Everything is runnable from the repo root on the dev machine.

## 1. Version

- Bump `version` in **both** `pyproject.toml` and `mcpb/manifest.json`
  (`tests/test_server_entry.py::PackagingVersionSyncTests` fails if they differ).
- Use semver-ish judgement: new tools or packaging changes → minor bump;
  fixes only → patch.

## 2. Verify

```
python -m pytest -q                 # full suite must pass
python scripts/healthcheck_readonly.py   # optional live read-only sanity sweep
```

## 3. Build

```
python -m build                     # dist/moneybird_mcp-X.Y.Z-py3-none-any.whl + .tar.gz
python -m twine check dist/moneybird_mcp-X.Y.Z*
python scripts/build_mcpb.py        # dist/moneybird-mcp-X.Y.Z-<platform>.mcpb
```

The `.mcpb` is platform-specific (dependencies are vendored into it), so build it on
the platform you're shipping for. The wheel/sdist are pure Python and universal.

Sanity: the wheel must contain no `.env`, tokens, sqlite state, or audit logs —
only the `moneybird` package. `twine check` must PASS for both artifacts.

## 4. Publish to PyPI

Publish under the pseudonymous identity (no real name anywhere in package
metadata — `authors` in `pyproject.toml` is the source of truth).

```
python -m twine upload dist/moneybird_mcp-X.Y.Z*
```

Authenticate with a PyPI API token (username `__token__`, password `pypi-...`).
First release claims the `moneybird-mcp` name; later uploads need the same account
or a maintainer invite.

## 5. After publishing

- Smoke-test the published artifact in a clean environment:
  `pipx run moneybird-mcp --help` (or `uvx moneybird-mcp`) — it should start the
  stdio server and complain only about missing credentials.
- Tag the commit: `git tag vX.Y.Z && git push origin vX.Y.Z`.
- Attach the `.mcpb` to a GitHub release for Desktop users (only useful once the
  repo is public).
- Update the README install instructions if the install story changed.
