#!/usr/bin/env python3
"""
Legacy candle timestamp migration.

Converts legacy Kite tz-aware timestamps (with +05:30 suffix) to the
canonical naive IST format used by the current pipeline. This ensures
all timestamps have a single identity for proper deduplication and
gap detection.

Run once after upgrading from a pre-migration database:
    python -m scripts.migrate_legacy_timestamps
"""
import sqlite3
from datetime import datetime
from paths import DB_PATH

from core.freshness import candle_timestamp
from core.logging_config import get_logger, setup_logging


logger = get_logger(__name__)


def migrate_legacy_timestamps() -> dict:
    """Migrate legacy candle timestamps to canonical format.

    Returns:
        Dict with migration statistics
    """
    stats = {
        "total_rows": 0,
        "legacy_rows": 0,
        "updated_rows": 0,
        "duplicate_rows": 0,
        "errors": 0,
    }

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    try:
        # Get all distinct timestamps that have the legacy format
        cursor = conn.execute("""
            SELECT DISTINCT timestamp FROM candles
            WHERE timestamp LIKE '%+05:30'
        """)
        legacy_timestamps = [row["timestamp"] for row in cursor.fetchall()]

        stats["legacy_rows"] = len(legacy_timestamps)
        logger.info(f"Found {stats['legacy_rows']} legacy timestamps to migrate")

        if not legacy_timestamps:
            return stats

        # Count total rows
        cursor = conn.execute("SELECT COUNT(*) as cnt FROM candles")
        stats["total_rows"] = cursor.fetchone()["cnt"]

        # Process each legacy timestamp
        for legacy_ts in legacy_timestamps:
            try:
                # Convert to canonical format
                canonical_ts = candle_timestamp(legacy_ts)

                if legacy_ts == canonical_ts:
                    continue  # Already canonical

                # Check if canonical timestamp already exists for same symbol
                cursor = conn.execute("""
                    SELECT symbol, timestamp FROM candles
                    WHERE timestamp = ?
                """, (legacy_ts,))

                rows_to_update = cursor.fetchall()

                for row in rows_to_update:
                    symbol = row["symbol"]

                    # Check if canonical version already exists
                    cursor = conn.execute("""
                        SELECT 1 FROM candles
                        WHERE symbol = ? AND timestamp = ?
                    """, (symbol, canonical_ts))

                    if cursor.fetchone():
                        # Canonical version exists, delete legacy row
                        conn.execute("""
                            DELETE FROM candles WHERE symbol = ? AND timestamp = ?
                        """, (symbol, legacy_ts))
                        stats["duplicate_rows"] += 1
                    else:
                        # Update legacy row to canonical timestamp
                        conn.execute("""
                            UPDATE candles SET timestamp = ?
                            WHERE symbol = ? AND timestamp = ?
                        """, (canonical_ts, symbol, legacy_ts))
                        stats["updated_rows"] += 1

                conn.commit()

            except Exception as e:
                logger.error(f"Error migrating timestamp {legacy_ts}", extra={"error": str(e)})
                stats["errors"] += 1
                conn.rollback()

        logger.info(
            "Legacy timestamp migration completed",
            extra=stats
        )

    finally:
        conn.close()

    return stats


def verify_migration() -> dict:
    """Verify migration was successful - no legacy timestamps remain."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    try:
        # Check for any remaining legacy timestamps
        cursor = conn.execute("""
            SELECT COUNT(*) as cnt FROM candles
            WHERE timestamp LIKE '%+05:30'
        """)
        legacy_count = cursor.fetchone()["cnt"]

        # Check for any duplicate (symbol, timestamp) pairs
        cursor = conn.execute("""
            SELECT symbol, timestamp, COUNT(*) as cnt
            FROM candles
            GROUP BY symbol, timestamp
            HAVING COUNT(*) > 1
        """)
        duplicates = cursor.fetchall()

        return {
            "legacy_timestamps_remaining": legacy_count,
            "duplicate_keys": len(duplicates),
            "duplicates": [{"symbol": d["symbol"], "timestamp": d["timestamp"], "count": d["cnt"]} for d in duplicates],
        }

    finally:
        conn.close()


def main():
    setup_logging()
    logger.info("Starting legacy candle timestamp migration")

    # Run migration
    stats = migrate_legacy_timestamps()

    # Verify
    verification = verify_migration()

    logger.info("Migration verification", extra=verification)

    if verification["legacy_timestamps_remaining"] > 0:
        logger.warning("Some legacy timestamps remain after migration")
        return 1

    if verification["duplicate_keys"] > 0:
        logger.warning("Duplicate (symbol, timestamp) keys found after migration")
        return 1

    logger.info("Migration completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())