"""SQLite FTS5 layer over the synced search index.

The JSON sync index (see :mod:`moneybird_mcp.sync`) stays the durable store — its
versioned incremental sync against Moneybird is the hard part and unchanged.
This module derives a full-text index from it, so ``search`` gets token-based
matching (multi-word, any order, prefixes, bm25 ranking) instead of a single
substring test. The FTS file is a disposable cache: it is rebuilt whenever the
sync index's ``updated_at`` changes, and every function degrades gracefully
(returning ``None``/no results) when FTS5 is unavailable, so callers can always
fall back to the substring scan.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from .config import data_dir, harden_private_file

FTS_INDEX_BASENAME = ".moneybird_search_fts"

# Bump whenever the ``records`` table gains or loses a column. The file is a
# rebuildable cache, so a mismatch drops and repopulates it.
FTS_SCHEMA_VERSION = 2

SEARCH_BUCKETS = (
    "contacts",
    "sales_invoices",
    "purchase_invoices",
    "receipts",
    "general_journal_documents",
    "financial_mutations",
)


def fts_index_path(administration_id: str | None) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(administration_id or "default"))
    return data_dir() / f"{FTS_INDEX_BASENAME}_{safe}.sqlite3"


def _connect(administration_id: str | None) -> sqlite3.Connection | None:
    """A connection with the schema in place, or None when FTS5 is unavailable.

    The whole file is a disposable cache, so a schema change simply drops and
    recreates the table rather than migrating it; the next refresh repopulates
    from the durable JSON index.
    """
    path = fts_index_path(administration_id)
    connection = sqlite3.connect(path)
    harden_private_file(path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None or row[0] != str(FTS_SCHEMA_VERSION):
            with connection:
                connection.execute("DROP TABLE IF EXISTS records")
                # Force a repopulate: the freshness marker belongs to the rows
                # that were just dropped.
                connection.execute(
                    "DELETE FROM meta WHERE key = 'sync_index_updated_at'"
                )
                connection.execute(
                    "INSERT OR REPLACE INTO meta (key, value) "
                    "VALUES ('schema_version', ?)",
                    (str(FTS_SCHEMA_VERSION),),
                )
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS records USING fts5("
            "search_text, title, record_id UNINDEXED, url UNINDEXED, "
            "bucket UNINDEXED, contact_id UNINDEXED, date UNINDEXED, "
            "amount UNINDEXED, state UNINDEXED, "
            "tokenize='unicode61 remove_diacritics 2')"
        )
    except sqlite3.OperationalError:  # FTS5 not compiled into this sqlite
        connection.close()
        return None
    return connection


def refresh_fts_index(index: dict[str, Any], administration_id: str | None) -> bool:
    """Bring the FTS cache in line with the sync index. Returns False when FTS is unusable."""
    connection = _connect(administration_id)
    if connection is None:
        return False
    try:
        # A no-change sync refreshes ``updated_at`` to record freshness, but
        # leaves ``content_updated_at`` stable. Avoid rebuilding the complete
        # FTS database when no searchable record changed.
        updated_at = str(
            index.get("content_updated_at")
            or index.get("updated_at")
            or ""
        )
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'sync_index_updated_at'"
        ).fetchone()
        if row is not None and row[0] == updated_at and updated_at:
            return True  # cache already matches this sync snapshot

        with connection:  # single transaction: never leaves a half-built index
            connection.execute("DELETE FROM records")
            for bucket in SEARCH_BUCKETS:
                records = (index.get(bucket) or {}).get("records") or {}
                connection.executemany(
                    "INSERT INTO records (search_text, title, record_id, url, "
                    "bucket, contact_id, date, amount, state) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        (
                            str(record.get("search_text") or ""),
                            str(record.get("title") or ""),
                            str(record.get("id") or ""),
                            str(record.get("url") or ""),
                            bucket,
                            str(record.get("contact_id") or ""),
                            str(record.get("date") or ""),
                            str(record.get("amount") or ""),
                            str(record.get("state") or ""),
                        )
                        for record in records.values()
                    ),
                )
            connection.execute(
                "INSERT OR REPLACE INTO meta (key, value) "
                "VALUES ('sync_index_updated_at', ?)",
                (updated_at,),
            )
        return True
    finally:
        connection.close()


def _match_expressions(query: str) -> list[str]:
    """FTS5 MATCH expressions to try in order: all words first, then any word.

    Every token matches as a prefix. The AND form keeps multi-word queries
    precise; the OR form is the recall fallback so e.g. 'vitens water' still
    surfaces Vitens records when no record contains both words.
    """
    tokens = re.findall(r"[^\s\"'()*:^]+", query)
    if not tokens:
        return []
    prefixed = [f'"{token}"*' for token in tokens]
    expressions = [" AND ".join(prefixed)]
    if len(prefixed) > 1:
        expressions.append(" OR ".join(prefixed))
    return expressions


def search_fts(
    administration_id: str | None,
    query: str,
    limit: int,
) -> list[dict[str, Any]] | None:
    """bm25-ranked matches, [] for no hits, or None when FTS cannot answer."""
    expressions = _match_expressions(query)
    if not expressions:
        return None
    connection = _connect(administration_id)
    if connection is None:
        return None
    try:
        for expression in expressions:
            rows = connection.execute(
                "SELECT record_id, title, url, contact_id, date, amount, state "
                "FROM records WHERE records MATCH ? "
                "ORDER BY bm25(records) LIMIT ?",
                (expression, max(1, limit)),
            ).fetchall()
            if rows:
                return [_hit(row) for row in rows]
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()
    return []


def _hit(row: tuple[Any, ...]) -> dict[str, Any]:
    """One search result, carrying the facets that avoid a follow-up fetch."""
    hit = {"id": row[0], "title": row[1], "url": row[2]}
    for key, value in zip(("contact_id", "date", "amount", "state"), row[3:]):
        if value:
            hit[key] = value
    return hit
