"""Write-safety machinery: durable approval state and the JSONL audit export.

Approvals are persisted in a small SQLite database inside :func:`~moneybird_mcp.config.data_dir`
so a prepared write survives a server restart and works when the server runs with more
than one worker process (prepare and execute may land on different processes). Approval
claims and outcomes remain in SQLite instead of deleting the row before an upstream write.
The audit log stays JSONL for backward-compatible, greppable export.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import sqlite3
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import (
    APPROVAL_TTL_MINUTES,
    MoneybirdError,
    MoneybirdHTTPError,
    data_dir,
    harden_private_file,
)
from .credentials import (
    CREDENTIAL_MODE_HOSTED_REQUEST_ONLY,
    get_active_administration_id,
    get_credential_mode,
)
from .formatting import iso_now

# The audit log is per administration so tenants never share a write history.
AUDIT_LOG_BASENAME = ".moneybird_audit_log"
LEGACY_AUDIT_LOG_PATH = Path(f"{AUDIT_LOG_BASENAME}.jsonl")
# Backward-compatible alias for the legacy single-file path (re-exported by the entrypoint).
AUDIT_LOG_PATH = LEGACY_AUDIT_LOG_PATH

APPROVALS_DB_BASENAME = "moneybird_approvals.sqlite3"
APPROVALS_SCHEMA_VERSION = 3

PENDING_APPROVAL_STATE = "pending"
CLAIMED_APPROVAL_STATE = "claimed"
SUCCESS_APPROVAL_STATE = "succeeded"
UNRESOLVED_APPROVAL_STATES = {
    CLAIMED_APPROVAL_STATE,
    "partial_failure",
    "verification_failed",
    "ambiguous",
}
APPROVAL_OUTCOME_STATES = {
    "success": SUCCESS_APPROVAL_STATE,
    "failed": "failed",
    "failed_pre_write": "failed_pre_write",
    "partial_failure": "partial_failure",
    "verification_failed": "verification_failed",
    "ambiguous": "ambiguous",
    "duplicate_suppressed": "duplicate_suppressed",
}


class _ClosingSQLiteConnection(sqlite3.Connection):
    """Make ``with _approvals_connection()`` close, not only commit, the handle."""

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def audit_log_path(administration_id: str | None = None) -> Path:
    administration_id = administration_id or get_active_administration_id()
    if not administration_id:
        return data_dir() / LEGACY_AUDIT_LOG_PATH.name
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(administration_id))
    return data_dir() / f"{AUDIT_LOG_BASENAME}_{safe}.jsonl"


def approvals_db_path() -> Path:
    return data_dir() / APPROVALS_DB_BASENAME


def _approvals_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(
        approvals_db_path(),
        timeout=30,
        factory=_ClosingSQLiteConnection,
    )
    harden_private_file(approvals_db_path())
    connection.execute("PRAGMA busy_timeout = 30000")
    (schema_version,) = connection.execute("PRAGMA user_version").fetchone()
    if int(schema_version) > APPROVALS_SCHEMA_VERSION:
        connection.close()
        raise MoneybirdError(
            "The approvals database was created by a newer Moneybird MCP "
            f"schema ({schema_version}); this build supports up to "
            f"{APPROVALS_SCHEMA_VERSION} and will not downgrade it."
        )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS approvals (
            approval_id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            payload TEXT NOT NULL,
            summary TEXT NOT NULL,
            administration_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            fingerprint TEXT NOT NULL DEFAULT '',
            claim_id TEXT,
            claimed_at TEXT,
            claim_owner TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            phase TEXT NOT NULL DEFAULT 'pending',
            dispatch_started_at TEXT,
            completed_at TEXT,
            outcome TEXT,
            error TEXT,
            reconciled_at TEXT,
            reconciled_by TEXT,
            reconciliation_evidence TEXT
        )
        """
    )
    connection.commit()

    # Existing installations have the original seven-column table. Serialize the
    # additive migration so concurrent workers cannot both try to add a column.
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(approvals)").fetchall()
    }
    additions = {
        "state": "TEXT NOT NULL DEFAULT 'pending'",
        "fingerprint": "TEXT NOT NULL DEFAULT ''",
        "claim_id": "TEXT",
        "claimed_at": "TEXT",
        "claim_owner": "TEXT",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "phase": "TEXT NOT NULL DEFAULT 'pending'",
        "dispatch_started_at": "TEXT",
        "completed_at": "TEXT",
        "outcome": "TEXT",
        "error": "TEXT",
        "reconciled_at": "TEXT",
        "reconciled_by": "TEXT",
        "reconciliation_evidence": "TEXT",
    }
    if not additions.keys() <= columns:
        connection.execute("BEGIN IMMEDIATE")
        try:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(approvals)").fetchall()
            }
            for name, declaration in additions.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE approvals ADD COLUMN {name} {declaration}"
                    )
            connection.execute(f"PRAGMA user_version = {APPROVALS_SCHEMA_VERSION}")
            connection.commit()
        except Exception:
            connection.rollback()
            connection.close()
            raise
    else:
        connection.execute(f"PRAGMA user_version = {APPROVALS_SCHEMA_VERSION}")

    # Populate the indexed fingerprint for rows created by the legacy schema.
    legacy_rows = connection.execute(
        "SELECT approval_id, payload FROM approvals WHERE fingerprint = ''"
    ).fetchall()
    for approval_id, payload_json in legacy_rows:
        try:
            fingerprint = str(json.loads(payload_json).get("fingerprint") or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            fingerprint = ""
        if fingerprint:
            connection.execute(
                "UPDATE approvals SET fingerprint = ? WHERE approval_id = ?",
                (fingerprint, approval_id),
            )

    # A partially migrated database may contain several already-claimed or
    # unresolved rows for the same semantic write. Retain the oldest one and
    # durably discard later duplicates before adding the uniqueness invariant.
    unresolved_sql = ", ".join(
        f"'{state}'" for state in sorted(UNRESOLVED_APPROVAL_STATES)
    )
    duplicate_groups = connection.execute(
        # Only values from the module-owned UNRESOLVED_APPROVAL_STATES set
        # are interpolated into this migration-only query.
        "SELECT administration_id, action, fingerprint "
        "FROM approvals WHERE fingerprint <> '' "
        f"AND state IN ({unresolved_sql}) "  # nosec B608
        "GROUP BY administration_id, action, fingerprint HAVING COUNT(*) > 1"
    ).fetchall()
    for administration_id, action, fingerprint in duplicate_groups:
        duplicate_rows = connection.execute(
            # The dynamic state list has the same fixed internal provenance.
            "SELECT approval_id FROM approvals "
            "WHERE administration_id = ? AND action = ? AND fingerprint = ? "
            f"AND state IN ({unresolved_sql}) "  # nosec B608
            "ORDER BY created_at, approval_id",
            (administration_id, action, fingerprint),
        ).fetchall()
        for (approval_id,) in duplicate_rows[1:]:
            connection.execute(
                "UPDATE approvals SET state = 'discarded', outcome = 'discarded', "
                "completed_at = ? WHERE approval_id = ?",
                (datetime.now(UTC).isoformat(), approval_id),
            )

    # Only one execution with an exact fingerprint may be active or unresolved.
    # Successful rows leave this index so a later explicit ``invalidated`` audit
    # event can reopen the fingerprint; audit_log_contains_success still reads
    # durable succeeded rows and ordered invalidations.
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS approvals_one_unresolved_fingerprint "
        "ON approvals (administration_id, action, fingerprint) "
        f"WHERE fingerprint <> '' AND state IN ({unresolved_sql})"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS approvals_pending_state "
        "ON approvals (state, expires_at)"
    )
    connection.commit()
    return connection


def _purge_expired(connection: sqlite3.Connection) -> None:
    connection.execute(
        "UPDATE approvals SET state = 'expired', outcome = 'expired', "
        "completed_at = COALESCE(completed_at, ?) "
        "WHERE state = 'pending' AND expires_at < ?",
        (
            datetime.now(UTC).isoformat(),
            datetime.now(UTC).isoformat(),
        ),
    )


def clear_pending_approvals() -> None:
    """Explicitly erase approval history for tests or a deliberate manual reset.

    Normal execution never deletes approval rows. This reset retains the legacy
    helper's clean-slate semantics so unresolved fingerprints from one isolated
    run cannot contaminate the next.
    """
    with _approvals_connection() as connection:
        connection.execute("DELETE FROM approvals")


def pending_approval_count() -> int:
    with _approvals_connection() as connection:
        _purge_expired(connection)
        (count,) = connection.execute(
            "SELECT COUNT(*) FROM approvals WHERE state = 'pending'"
        ).fetchone()
    return int(count)


def make_approval(action: str, payload: dict[str, Any], summary: str) -> dict[str, Any]:
    if get_credential_mode() == CREDENTIAL_MODE_HOSTED_REQUEST_ONLY:
        raise MoneybirdError(
            "Write preparation is disabled in hosted_request_only mode because "
            "approval state is not yet bound to an independently authenticated "
            "principal, session, and grant."
        )
    approval_id = secrets.token_urlsafe(18)
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=APPROVAL_TTL_MINUTES)
    administration_id = get_active_administration_id()
    if not administration_id:
        raise MoneybirdError(
            "Cannot prepare a write without an active Moneybird administration."
        )
    with _approvals_connection() as connection:
        _purge_expired(connection)
        # Read collisions before inserting, so this approval never matches itself.
        collisions = _pending_collisions(
            connection, _approval_target_ids(payload, action), action
        )
        try:
            connection.execute(
                "INSERT INTO approvals ("
                "approval_id, action, payload, summary, administration_id, created_at, "
                "expires_at, state, fingerprint"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    approval_id,
                    action,
                    # default=str keeps rare non-JSON scalars (Decimal, dates) storable;
                    # write payloads must be JSON-shaped anyway to be sendable to Moneybird.
                    json.dumps(payload, ensure_ascii=True, default=str),
                    summary,
                    str(administration_id),
                    now.isoformat(),
                    expires_at.isoformat(),
                    str(payload.get("fingerprint") or ""),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise MoneybirdError(
                "The approval could not be persisted because its identifier or "
                "unresolved write fingerprint conflicted with durable state."
            ) from exc
    prepared = {
        "approval_id": approval_id,
        "action": action,
        "summary": summary,
        "administration_id": administration_id,
        "expires_at": expires_at.isoformat(),
        "warning": (
            "This action is not executed yet. Ask the user for explicit confirmation "
            "before calling execute_approved_action (or the matching "
            "*_from_approval tool)."
        ),
    }
    if collisions:
        prepared["collides_with"] = collisions
        prepared["collision_warning"] = (
            "Another pending approval targets the same record. Each preview pins the "
            "record's current version, so executing either one makes the other stale "
            "and it will abort rather than apply. Execute them one at a time, "
            "re-preparing the next after each, and re-check its preview."
        )
    return prepared


# Payload keys naming the record an action changes. A booking_id is included
# only for invoice/document bookings: for a LedgerAccount booking it names the
# category, which two unrelated mutations legitimately share.
_TARGET_ID_KEYS = frozenset(
    {"document_id", "financial_mutation_id", "sales_invoice_id"}
)


def _approval_target_ids(payload: Any, action: str = "") -> set[str]:
    """Collect the ids of records a prepared payload would change.

    Only records the action *writes* count. A ``contact_id`` is usually a
    foreign key — creating an invoice names a contact without pinning or
    changing it — so it counts only for the actions whose subject is the
    contact itself. Treating every occurrence as a target would report a
    collision between a contact edit and any invoice that merely refers to that
    contact, and tell the user to discard two approvals that can both apply.
    """
    keys = set(_TARGET_ID_KEYS)
    if action.endswith("_contact") or "contacts" in action:
        keys.add("contact_id")
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key in keys and isinstance(value, (str, int)):
                text = str(value).strip()
                if text:
                    found.add(text)
            elif isinstance(value, (dict, list)):
                walk(value)
        booking_type = str(node.get("booking_type") or "")
        booking_id = str(node.get("booking_id") or "").strip()
        if booking_id and booking_type in {"Document", "SalesInvoice"}:
            found.add(booking_id)

    walk(payload)
    return found


def _pending_collisions(
    connection: sqlite3.Connection,
    targets: set[str],
    action: str = "",
) -> list[dict[str, Any]]:
    """Return pending approvals that would touch any of ``targets``."""
    if not targets:
        return []
    rows = connection.execute(
        "SELECT approval_id, action, summary, payload FROM approvals "
        "WHERE state = ? ORDER BY created_at",
        (PENDING_APPROVAL_STATE,),
    ).fetchall()
    collisions: list[dict[str, Any]] = []
    for pending_id, pending_action, pending_summary, payload_json in rows:
        try:
            other = json.loads(payload_json)
        except (TypeError, ValueError):
            continue
        shared = sorted(targets & _approval_target_ids(other, pending_action))
        if shared:
            collisions.append(
                {
                    "approval_id": pending_id,
                    "action": pending_action,
                    "summary": pending_summary,
                    "shared_targets": shared,
                }
            )
    return collisions


def pop_approval(
    approval_id: str,
    expected_action: str,
    *,
    administration_id: str | None = None,
) -> dict[str, Any]:
    connection = _approvals_connection()
    try:
        # BEGIN IMMEDIATE makes validation + compare-and-set one write transaction
        # across threads and processes. No network call occurs while it is held.
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT action, payload, summary, administration_id, expires_at, state, "
            "fingerprint "
            "FROM approvals WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        if not row:
            raise MoneybirdError(
                "Unknown approval_id. Prepare the action again before executing it."
            )
        (
            action,
            payload_json,
            summary,
            prepared_administration_id,
            expires_at,
            state,
            fingerprint,
        ) = row

        if action != expected_action:
            raise MoneybirdError(
                f"approval_id is for {action}, not {expected_action}."
            )

        if (
            prepared_administration_id
            and str(prepared_administration_id) != str(administration_id or "")
        ):
            raise MoneybirdError(
                "approval_id belongs to a different Moneybird administration. "
                "Prepare the action again for the active administration."
            )

        if state != PENDING_APPROVAL_STATE:
            raise MoneybirdError(
                f"approval_id is already {state} and cannot be executed again."
            )

        if datetime.now(UTC) > datetime.fromisoformat(expires_at):
            connection.execute(
                "UPDATE approvals SET state = 'expired', outcome = 'expired', "
                "completed_at = ? WHERE approval_id = ? AND state = 'pending'",
                (datetime.now(UTC).isoformat(), approval_id),
            )
            connection.commit()
            raise MoneybirdError("approval_id expired. Prepare the action again.")

        claim_id = secrets.token_urlsafe(18)
        claimed_at = datetime.now(UTC).isoformat()
        claim_owner = f"{socket.gethostname()}:{os.getpid()}"
        try:
            cursor = connection.execute(
                "UPDATE approvals SET state = 'claimed', claim_id = ?, claimed_at = ?, "
                "claim_owner = ?, attempt_count = attempt_count + 1, phase = 'preflight', "
                "dispatch_started_at = NULL "
                "WHERE approval_id = ? AND state = 'pending'",
                (claim_id, claimed_at, claim_owner, approval_id),
            )
        except sqlite3.IntegrityError as exc:
            raise MoneybirdError(
                "An execution with this exact fingerprint is already active or "
                "requires reconciliation."
            ) from exc
        if cursor.rowcount != 1:
            raise MoneybirdError(
                "approval_id was claimed concurrently and cannot be executed again."
            )
        connection.commit()
        # Every guarded write claims its approval here, so this is the one place
        # that can start the applied-write ledger for all of them.
        reset_applied_writes()
        return {
            "approval_id": approval_id,
            "claim_id": claim_id,
            "claim_owner": claim_owner,
            "action": action,
            "payload": json.loads(payload_json),
            "summary": summary,
            "administration_id": prepared_administration_id,
            "expires_at": expires_at,
            "fingerprint": fingerprint,
        }
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def peek_approval(
    approval_id: str,
    *,
    administration_id: str | None = None,
) -> dict[str, Any]:
    """Inspect a pending approval without consuming it.

    Used by the generic approval dispatcher to select the already-bound action.
    The action-specific executor still calls :func:`pop_approval`, so single-use,
    expiry, and tenant checks remain centralized and unchanged.
    """
    with _approvals_connection() as connection:
        row = connection.execute(
            "SELECT action, payload, summary, administration_id, expires_at, state "
            "FROM approvals WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
    if not row:
        raise MoneybirdError(
            "Unknown approval_id. Prepare the action again before executing it."
        )
    action, payload_json, summary, prepared_administration_id, expires_at, state = row
    if (
        prepared_administration_id
        and str(prepared_administration_id) != str(administration_id or "")
    ):
        raise MoneybirdError(
            "approval_id belongs to a different Moneybird administration. "
            "Prepare the action again for the active administration."
        )
    if state != PENDING_APPROVAL_STATE:
        raise MoneybirdError(
            f"approval_id is already {state} and cannot be executed again."
        )
    if datetime.now(UTC) > datetime.fromisoformat(expires_at):
        with _approvals_connection() as connection:
            connection.execute(
                "UPDATE approvals SET state = 'expired', outcome = 'expired', "
                "completed_at = ? WHERE approval_id = ? AND state = 'pending'",
                (datetime.now(UTC).isoformat(), approval_id),
            )
        raise MoneybirdError("approval_id expired. Prepare the action again.")
    return {
        "action": action,
        "payload": json.loads(payload_json),
        "summary": summary,
        "administration_id": prepared_administration_id,
        "expires_at": expires_at,
    }


def discard_approval(
    approval_id: str,
    *,
    administration_id: str | None = None,
) -> bool:
    """Durably discard an unexecuted approval owned by the active administration."""
    with _approvals_connection() as connection:
        cursor = connection.execute(
            "UPDATE approvals SET state = 'discarded', outcome = 'discarded', "
            "completed_at = ? WHERE approval_id = ? AND administration_id = ? "
            "AND state = 'pending'",
            (
                datetime.now(UTC).isoformat(),
                approval_id,
                str(administration_id or ""),
            ),
        )
    return cursor.rowcount > 0


APPROVAL_EXECUTION_PHASES = {
    "preflight",
    "dispatching",
    "verifying",
}


def record_approval_phase(
    approval_id: str,
    phase: str,
    *,
    administration_id: str | None = None,
) -> None:
    """Advance a claimed write through its durable dispatch boundary."""

    if phase not in APPROVAL_EXECUTION_PHASES:
        raise MoneybirdError(
            f"Unsupported execution phase {phase!r}; expected one of "
            f"{', '.join(sorted(APPROVAL_EXECUTION_PHASES))}."
        )
    administration_id = administration_id or get_active_administration_id()
    dispatch_started_at = (
        datetime.now(UTC).isoformat() if phase == "dispatching" else None
    )
    with _approvals_connection() as connection:
        cursor = connection.execute(
            "UPDATE approvals SET phase = ?, "
            "dispatch_started_at = COALESCE(dispatch_started_at, ?) "
            "WHERE approval_id = ? AND administration_id = ? AND state = 'claimed'",
            (
                phase,
                dispatch_started_at,
                approval_id,
                str(administration_id or ""),
            ),
        )
        if cursor.rowcount != 1:
            raise MoneybirdError(
                "Cannot advance execution phase because this approval is not claimed."
            )


def approval_execution_state(
    approval_id: str,
    *,
    administration_id: str | None = None,
) -> dict[str, Any]:
    """Read durable execution metadata for diagnostics and reconciliation."""

    administration_id = administration_id or get_active_administration_id()
    with _approvals_connection() as connection:
        row = connection.execute(
            "SELECT approval_id, action, summary, administration_id, state, outcome, "
            "fingerprint, claim_id, claimed_at, claim_owner, attempt_count, phase, "
            "dispatch_started_at, completed_at, error, reconciled_at, reconciled_by, "
            "reconciliation_evidence FROM approvals "
            "WHERE approval_id = ? AND administration_id = ?",
            (approval_id, str(administration_id or "")),
        ).fetchone()
    if row is None:
        raise MoneybirdError("Unknown approval_id for this administration.")
    keys = (
        "approval_id",
        "action",
        "summary",
        "administration_id",
        "state",
        "outcome",
        "fingerprint",
        "claim_id",
        "claimed_at",
        "claim_owner",
        "attempt_count",
        "phase",
        "dispatch_started_at",
        "completed_at",
        "error",
        "reconciled_at",
        "reconciled_by",
        "reconciliation_evidence",
    )
    return dict(zip(keys, row))


def list_unresolved_approval_executions(
    *,
    administration_id: str | None = None,
) -> list[dict[str, Any]]:
    administration_id = administration_id or get_active_administration_id()
    unresolved = tuple(sorted(UNRESOLVED_APPROVAL_STATES))
    placeholders = ", ".join("?" for _item in unresolved)
    with _approvals_connection() as connection:
        rows = connection.execute(
            # Dynamic text is a fixed-length list of SQLite parameter markers.
            "SELECT approval_id FROM approvals WHERE administration_id = ? "
            f"AND state IN ({placeholders}) ORDER BY claimed_at, approval_id",  # nosec B608
            (str(administration_id or ""), *unresolved),
        ).fetchall()
    return [
        approval_execution_state(
            str(row[0]),
            administration_id=administration_id,
        )
        for row in rows
    ]


def reconcile_approval_execution(
    approval_id: str,
    resolution: str,
    *,
    evidence: str,
    reconciled_by: str,
    administration_id: str | None = None,
) -> dict[str, Any]:
    """Resolve an unresolved execution after an operator has inspected Moneybird."""

    resolution = str(resolution).strip()
    evidence = str(evidence).strip()
    reconciled_by = str(reconciled_by).strip()
    if resolution not in {"proven_absent", "succeeded_verified", "manual_review"}:
        raise MoneybirdError(
            "resolution must be proven_absent, succeeded_verified, or manual_review."
        )
    if not evidence or not reconciled_by:
        raise MoneybirdError(
            "Reconciliation requires non-empty evidence and reconciled_by."
        )
    administration_id = administration_id or get_active_administration_id()
    now = datetime.now(UTC).isoformat()
    unresolved = tuple(sorted(UNRESOLVED_APPROVAL_STATES))
    placeholders = ", ".join("?" for _item in unresolved)
    with _approvals_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT state, outcome, phase FROM approvals "
            "WHERE approval_id = ? AND administration_id = ?",
            (approval_id, str(administration_id or "")),
        ).fetchone()
        if row is None:
            raise MoneybirdError("Unknown approval_id for this administration.")
        state, existing_outcome, phase = (
            str(row[0]),
            str(row[1] or ""),
            str(row[2] or ""),
        )
        if state not in UNRESOLVED_APPROVAL_STATES:
            raise MoneybirdError(
                f"approval_id is {state}, not an unresolved execution."
            )
        if resolution == "manual_review":
            new_state = state
            new_outcome = existing_outcome or state
            new_phase = phase
            completed_at = None
        elif resolution == "proven_absent":
            # A dispatched call that was later proven absent is materially
            # different from one that failed before dispatch. Keep that
            # distinction durable while releasing the unresolved fingerprint.
            new_state = "reconciled_absent"
            new_outcome = "reconciled_absent"
            new_phase = "reconciled"
            completed_at = now
        else:
            new_state = SUCCESS_APPROVAL_STATE
            new_outcome = "success"
            new_phase = "reconciled"
            completed_at = now
        cursor = connection.execute(
            # Dynamic text is a fixed-length list of SQLite parameter markers.
            "UPDATE approvals SET state = ?, outcome = ?, phase = ?, "
            "completed_at = COALESCE(?, completed_at), reconciled_at = ?, "
            "reconciled_by = ?, reconciliation_evidence = ? "
            "WHERE approval_id = ? AND administration_id = ? "
            f"AND state IN ({placeholders})",  # nosec B608
            (
                new_state,
                new_outcome,
                new_phase,
                completed_at,
                now,
                reconciled_by,
                evidence,
                approval_id,
                str(administration_id or ""),
                *unresolved,
            ),
        )
        if cursor.rowcount != 1:
            raise MoneybirdError("The execution changed while it was reconciled.")
    append_audit_log(
        {
            "action": "reconcile_approval_execution",
            "approval_id": approval_id,
            "result": resolution,
            "prior_phase": phase,
            "evidence": evidence,
            "reconciled_by": reconciled_by,
        },
        administration_id=administration_id,
    )
    return approval_execution_state(
        approval_id,
        administration_id=administration_id,
    )


def record_approval_outcome(
    approval_id: str,
    outcome: str,
    *,
    administration_id: str | None = None,
    error: str = "",
) -> None:
    """Persist the terminal or unresolved result of one claimed approval.

    ``success`` is intentionally explicit. Partial, verification-failed, and
    ambiguous outcomes remain distinct and never become successful duplicate
    evidence.
    """
    state = APPROVAL_OUTCOME_STATES.get(str(outcome))
    if state is None:
        supported = ", ".join(sorted(APPROVAL_OUTCOME_STATES))
        raise MoneybirdError(
            f"Unsupported approval outcome {outcome!r}; use one of: {supported}."
        )
    administration_id = administration_id or get_active_administration_id()
    with _approvals_connection() as connection:
        cursor = connection.execute(
            "UPDATE approvals SET state = ?, outcome = ?, completed_at = ?, error = ?, "
            "phase = CASE WHEN ? IN "
            "('partial_failure', 'verification_failed', 'ambiguous') "
            "THEN phase ELSE 'completed' END "
            "WHERE approval_id = ? AND administration_id = ? AND state = 'claimed'",
            (
                state,
                outcome,
                datetime.now(UTC).isoformat(),
                str(error or ""),
                outcome,
                approval_id,
                str(administration_id or ""),
            ),
        )
        if cursor.rowcount != 1:
            row = connection.execute(
                "SELECT state FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            current = str(row[0]) if row else "unknown"
            raise MoneybirdError(
                f"Cannot record outcome for approval_id in state {current}."
            )


def classify_write_exception(exc: BaseException) -> str:
    """Conservatively recognize current client errors that mean result unknown."""
    message = str(exc).lower()
    ambiguous_markers = (
        "ambiguous",
        "may already have been processed",
        "reconcile moneybird before retrying",
        "reconcile the record before retrying",
    )
    return (
        "ambiguous"
        if any(marker in message for marker in ambiguous_markers)
        else "failed"
    )


# How many mutating Moneybird requests have succeeded during the write currently
# being executed. Reset when an approval is claimed, incremented by the HTTP
# client. It answers the only question that separates a closed failure from an
# unresolved one: could anything have been applied before this error?
_APPLIED_WRITES: ContextVar[int] = ContextVar("moneybird_applied_writes", default=0)


def reset_applied_writes() -> None:
    _APPLIED_WRITES.set(0)


def record_applied_write() -> None:
    """Count one mutating request that Moneybird accepted."""
    _APPLIED_WRITES.set(_APPLIED_WRITES.get() + 1)


def applied_write_count() -> int:
    return _APPLIED_WRITES.get()


def classify_failed_write(exc: BaseException, *, phase: str) -> str:
    """Decide what a failed execution actually proved.

    ``ambiguous`` exists for the cases where the write may or may not have
    landed — a timeout, a 5xx, a dropped connection. It is expensive: it leaves
    an unresolved entry in the durable audit trail that a human has to close.

    Treating every post-dispatch error as ambiguous spends that cost on errors
    that prove the opposite. When Moneybird answers 422 because an email domain
    is unreachable, the request it rejected changed nothing, and nothing has been
    applied yet in this execution — that is a closed failure. Recording it as
    unresolved teaches people to ignore the state, which is what makes it useless
    for the timeouts where it is real.
    """
    definitive = (
        isinstance(exc, MoneybirdHTTPError)
        and exc.is_definitive_rejection
        # A rejection only proves *this* request applied nothing. If an earlier
        # request in the same execution already succeeded, the action as a whole
        # is not clean and stays for reconciliation.
        and applied_write_count() == 0
    )
    if phase == "preflight" and classify_write_exception(exc) != "ambiguous":
        return "failed_pre_write"
    if definitive:
        return "failed"
    return "ambiguous"


def append_audit_log(entry: dict[str, Any], administration_id: str | None = None) -> None:
    administration_id = administration_id or get_active_administration_id()
    log_entry = {"timestamp": iso_now(), **entry, "administration_id": administration_id}
    path = audit_log_path(administration_id)
    with path.open("a", encoding="utf-8") as handle:
        harden_private_file(path)
        handle.write(json.dumps(log_entry, ensure_ascii=True) + "\n")


def _audit_log_candidates(administration_id: str | None) -> list[Path]:
    """Current path plus pre-data-dir/pre-multitenant locations, for back-compat reads."""
    candidates = [audit_log_path(administration_id)]
    if administration_id:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(administration_id))
        candidates.append(Path(f"{AUDIT_LOG_BASENAME}_{safe}.jsonl"))
    candidates.append(LEGACY_AUDIT_LOG_PATH)
    unique: list[Path] = []
    for path in candidates:
        if path.resolve() not in {existing.resolve() for existing in unique}:
            unique.append(path)
    return unique


def audit_log_contains_success(
    action: str,
    fingerprint: str,
    administration_id: str | None = None,
) -> bool:
    administration_id = administration_id or get_active_administration_id()
    expected_administration_id = str(administration_id or "")
    latest_timestamp = ""
    latest_result: str | None = None

    # SQLite is now the durable source for newly executed approvals. Merge it
    # with JSONL so a later explicit invalidation can still reopen a fingerprint.
    with _approvals_connection() as connection:
        rows = connection.execute(
            "SELECT completed_at FROM approvals "
            "WHERE administration_id = ? AND action = ? AND fingerprint = ? "
            "AND state = 'succeeded'",
            (str(administration_id or ""), action, fingerprint),
        ).fetchall()
    for (completed_at,) in rows:
        timestamp = str(completed_at or "")
        if timestamp >= latest_timestamp:
            latest_timestamp = timestamp
            latest_result = "success"

    for path in _audit_log_candidates(administration_id):
        if not path.exists():
            continue
        for raw_line in path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines():
            if not raw_line.strip():
                continue
            try:
                entry = json.loads(raw_line)
            except (TypeError, ValueError, json.JSONDecodeError):
                # JSONL appends can be interrupted mid-line. A corrupt export
                # record must never crash execution or become success evidence.
                continue
            if not isinstance(entry, dict):
                continue
            entry_administration_id = entry.get("administration_id")
            if (
                entry_administration_id is None
                or str(entry_administration_id) != expected_administration_id
            ):
                # Legacy unscoped entries and foreign-tenant entries are not
                # authoritative duplicate-suppression evidence.
                continue
            if (
                entry.get("action") == action
                and entry.get("fingerprint") == fingerprint
            ):
                result = str(entry.get("result") or "")
                # Failed and partial attempts never erase a previously verified
                # success. Only an explicit invalidation can reopen the exact
                # fingerprint; a later success can close it again.
                if result not in {"success", "invalidated"}:
                    continue
                timestamp = str(entry.get("timestamp") or "")
                if timestamp >= latest_timestamp:
                    latest_timestamp = timestamp
                    latest_result = result
    # An append-only ``invalidated`` entry can correct a false-positive success
    # after live verification proves the expected state is no longer present.
    return latest_result == "success"


def append_failed_audit_log(
    action: str,
    *,
    fingerprint: str = "",
    error: str,
    partial: dict[str, Any] | None = None,
    administration_id: str | None = None,
    result: str = "failed",
) -> None:
    if result not in {
        "failed",
        "failed_pre_write",
        "partial_failure",
        "verification_failed",
        "ambiguous",
    }:
        raise ValueError("Failure audit result cannot be successful.")
    append_audit_log(
        {
            "action": action,
            "fingerprint": fingerprint,
            "result": result,
            "error": error,
            "partial": partial or {},
        },
        administration_id=administration_id,
    )
