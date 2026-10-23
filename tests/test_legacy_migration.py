"""Upgrade the previously deployed schema without losing ledger or metadata."""

from datetime import datetime
import os
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import Boolean, DateTime, Float, Integer, create_engine

from trading_agent.storage.db import Base

RENAMES = {
    "open_positions": "position_metadata", "signals": "signal_metadata",
    "opportunities": "opportunity_metadata", "system_events": "event_metadata",
}


def alembic(path, *args):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        env=os.environ | {"DB_URL": f"sqlite:///{path.as_posix()}"},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def legacy_database(path):
    alembic(path, "upgrade", "001")
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            row = {}
            for column in table.columns:
                if isinstance(column.type, DateTime):
                    value = datetime(2026, 10, 1, 10)
                elif isinstance(column.type, Boolean):
                    value = True
                elif isinstance(column.type, (Float, Integer)):
                    value = 1
                else:
                    value = '{"preserve":"original"}' if column.name.endswith("metadata") else "TEST"
                row[column.name] = value
            connection.execute(table.insert().values(**row))
    engine.dispose()
    with sqlite3.connect(path) as connection:
        for table, canonical in RENAMES.items():
            connection.execute(f'ALTER TABLE "{table}" RENAME COLUMN "{canonical}" TO metadata')
        connection.execute('ALTER TABLE trades DROP COLUMN updated_at')


def snapshot(path):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return {table.name: [dict(row) for row in connection.execute(f'SELECT * FROM "{table.name}" ORDER BY id')]
                for table in Base.metadata.sorted_tables}


def test_legacy_upgrade_preserves_every_record_and_metadata(tmp_path):
    path = tmp_path / "legacy.db"
    legacy_database(path)
    before = snapshot(path)
    alembic(path, "upgrade", "head")
    for table, canonical in RENAMES.items():
        for row in before[table]:
            row[canonical] = row.pop("metadata")
    for row in before["trades"]:
        row["updated_at"] = row["created_at"]
    assert snapshot(path) == before
    alembic(path, "check")
    alembic(path, "upgrade", "head")
    assert snapshot(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("002",)


@pytest.mark.parametrize("ambiguous", [True, False])
def test_unknown_metadata_schema_is_rejected_before_any_changes(tmp_path, ambiguous):
    path = tmp_path / "unknown.db"
    legacy_database(path)
    with sqlite3.connect(path) as connection:
        if ambiguous:
            connection.execute('ALTER TABLE signals ADD COLUMN signal_metadata TEXT')
        else:
            connection.execute('ALTER TABLE signals DROP COLUMN metadata')
    before = snapshot(path)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=os.environ | {"DB_URL": f"sqlite:///{path.as_posix()}"},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0
    assert "Ambiguous or missing metadata" in result.stderr
    assert snapshot(path) == before
