# Local data lifecycle

Moneybird MCP stores local operational state. This page describes the current behaviour; dedicated `moneybird-mcp data ...` subcommands are not implemented yet.

## Default location

For the installed stdio command, the default directory is:

```text
~/.moneybird-mcp
```

On Windows this normally resolves to:

```text
%USERPROFILE%\.moneybird-mcp
```

Override it with:

```text
MONEYBIRD_MCP_DATA_DIR=/absolute/private/path
```

The legacy clone entrypoint may retain working-directory behaviour for compatibility. Prefer an explicit private data directory.

## What may be stored

Depending on the enabled features, the directory can contain:

| Data | Purpose | Sensitivity |
|---|---|---|
| `moneybird_approvals.sqlite3` | Prepared writes, execution claims, outcomes, and reconciliation evidence | High |
| `.moneybird_audit_log_<administration>.jsonl` | Per-administration write audit export | High |
| OAuth token store | Moneybird access and refresh tokens | Critical |
| Sync JSON and SQLite FTS files | Local searchable copies of bookkeeping records | High |
| Telemetry state | Bounded local performance and error aggregates | Moderate |

Exact cache filenames may evolve. Treat the whole directory as sensitive financial data.

The project applies best-effort private POSIX modes, but Windows and some mounted filesystems require the operator to configure directory ACLs. Files are not encrypted by Moneybird MCP.

## Retention

There is no automatic hosted retention or deletion service.

- Pending approvals normally expire after 15 minutes.
- Claimed, partial, ambiguous, and verification-failed write outcomes remain durable for reconciliation.
- Audit logs and local indexes remain until the operator removes them.
- OAuth tokens remain until removed or revoked.

## Back up

Stop the MCP server before copying the directory so the SQLite approvals database and search files are not changing during the backup.

A backup can contain live credentials and customer bookkeeping data. Encrypt it, restrict access, and apply an intentional retention period.

## Reset a read-only installation

When the installation has never used `write_enabled`, a complete local reset is generally:

1. stop every Moneybird MCP process;
2. revoke the Moneybird token if it is being retired;
3. back up anything that must be retained;
4. remove the configured data directory;
5. restart the client and reconfigure credentials as needed.

Deleting the directory removes local indexes and OAuth state. It does not delete data from Moneybird.

## Reset an installation that has used writes

Do not delete the approvals database or audit logs while an action is claimed, partial, ambiguous, or awaiting verification.

First inspect unresolved executions:

```bash
python scripts/reconcile_execution.py --administration-id <id> list
```

Use the script's help for the exact supported actions:

```bash
python scripts/reconcile_execution.py --help
```

Resolve each case using independent evidence from Moneybird. The reconciliation CLI deliberately requires explicit evidence-bearing decisions.

After unresolved work has been handled and retained records have been exported as required, stop the server and remove the data directory.

## Remove only search state

Stop the server before deleting cache or FTS files. Keep the approvals database, audit logs, and OAuth store unless you intentionally want to remove them too.

Because exact cache filenames are implementation details, inspect the selected data directory and confirm each file against the current version before deletion. A later sync rebuilds removed search state from Moneybird.

## Remove OAuth credentials

Stop the server, remove the local OAuth token file from the data directory, and revoke the application grant in Moneybird when appropriate.

If `MONEYBIRD_ACCESS_TOKEN` is set in the process environment or an explicit environment file, removing the OAuth store does not remove that separate credential.

## Uninstalling the package

```bash
python -m pip uninstall moneybird-mcp
```

Package removal does not remove the data directory. Delete local state separately after applying the checks above.

## Planned CLI direction

A future lifecycle CLI could safely provide commands such as:

```text
moneybird-mcp data status
moneybird-mcp data export
moneybird-mcp data purge-search
moneybird-mcp approvals list
moneybird-mcp approvals reconcile
```

Those names are design targets, not current commands. Any implementation should refuse destructive cleanup while unresolved writes exist and should never print credentials or raw bookkeeping data by default.
