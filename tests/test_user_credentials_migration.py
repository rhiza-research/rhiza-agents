"""Tests for the stale-user_credentials migration in Database._init_db.

Pre-credentials-feature deployments may have a ``user_credentials`` table
left over from an earlier prototype with a completely different schema
(``type`` / ``fields_json`` / ``payload_ciphertext`` / ``key_version``).
``CREATE TABLE IF NOT EXISTS`` skips it, so INSERTs blow up at runtime.
The migration in ``Database._init_db`` detects that case and either drops
(if empty) or refuses to start (if non-empty — won't silently lose
encrypted credentials).

These tests exercise the three branches against a temp SQLite file.
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path

import pytest

from rhiza_agents.db.sqlite import Database

# Schema as it existed in the pre-credentials prototype.
_STALE_SCHEMA = """
CREATE TABLE user_credentials (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    payload_ciphertext BLOB NOT NULL,
    key_version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _seed_db(seed_sql: str | None = None) -> Path:
    """Create a temp SQLite file. Optionally seed it with custom SQL."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    path = Path(f.name)
    if seed_sql:
        con = sqlite3.connect(path)
        con.executescript(seed_sql)
        con.commit()
        con.close()
    return path


def _columns(path: Path, table: str) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def _row_count(path: Path, table: str) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def _connect(path: Path) -> Database:
    db = Database(f"sqlite:///{path}")
    asyncio.run(db.connect())
    return db


def _disconnect(db: Database) -> None:
    asyncio.run(db.disconnect())


def test_init_db_creates_current_schema_on_fresh_db():
    """Most common case: no table exists → CREATE builds the new shape."""
    path = _seed_db()
    try:
        db = _connect(path)
        try:
            cols = _columns(path, "user_credentials")
            assert "value_ciphertext" in cols
            assert "payload_ciphertext" not in cols
        finally:
            _disconnect(db)
    finally:
        path.unlink()


def test_init_db_no_op_when_current_schema_already_present():
    """Re-running connect on a current-schema DB must be idempotent."""
    path = _seed_db()
    try:
        db = _connect(path)
        _disconnect(db)
        cols_before = _columns(path, "user_credentials")
        # Reconnect; should be a no-op for user_credentials.
        db2 = _connect(path)
        try:
            cols_after = _columns(path, "user_credentials")
            assert cols_before == cols_after
        finally:
            _disconnect(db2)
    finally:
        path.unlink()


def test_init_db_drops_empty_stale_table_and_recreates_current_shape():
    """The dev-DB scenario: stale table with no rows → safe to drop + recreate."""
    path = _seed_db(_STALE_SCHEMA)
    try:
        # Sanity check the seed was the stale shape.
        assert "payload_ciphertext" in _columns(path, "user_credentials")
        assert "value_ciphertext" not in _columns(path, "user_credentials")
        assert _row_count(path, "user_credentials") == 0

        db = _connect(path)
        try:
            cols = _columns(path, "user_credentials")
            assert "value_ciphertext" in cols
            assert "payload_ciphertext" not in cols
            assert "fields_json" not in cols
        finally:
            _disconnect(db)
    finally:
        path.unlink()


def test_init_db_refuses_to_drop_non_empty_stale_table():
    """If somehow the prod DB has stale schema WITH data, refuse loudly.

    The encrypted-payload format is encryption-version-specific so there's
    no automatic remap. Better to fail fast than to silently destroy
    encrypted credentials the operator stored under the prototype.
    """
    path = _seed_db(
        _STALE_SCHEMA
        + "\nINSERT INTO user_credentials (id, user_id, name, type, fields_json, payload_ciphertext) "
        + "VALUES ('1', 'u', 'n', 'env_var', '{}', X'00');"
    )
    try:
        with pytest.raises(RuntimeError, match="stale prototype schema"):
            db = _connect(path)
            _disconnect(db)
        # The stale row + table must still be there — we did NOT drop.
        assert _row_count(path, "user_credentials") == 1
        assert "payload_ciphertext" in _columns(path, "user_credentials")
    finally:
        path.unlink()
