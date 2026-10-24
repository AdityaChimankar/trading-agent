from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading_agent.ingest.gap_healer import GapHealer
from trading_agent.ingest.fetch_historical import HistoricalFetcher
from trading_agent.storage.parquet_store import ParquetStore


def test_read_latest_accepts_mixed_iso_timestamp_formats(tmp_path):
    path = tmp_path / "candles" / "symbol=TEST" / "interval=minute"
    path.mkdir(parents=True)
    frame = pd.DataFrame(
        {
            "timestamp": [
                "2026-10-12T03:44:00Z",
                "2026-10-12 03:45:00.000000Z",
            ],
            "open": [1.0, 2.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
            "volume": [10, 20],
        }
    )
    pq.write_table(pa.Table.from_pandas(frame), path / "legacy.parquet")

    latest = ParquetStore(root_path=tmp_path).read_latest("TEST", "minute")

    assert latest.loc[0, "timestamp"] == pd.Timestamp("2026-10-12T03:45:00Z")


@pytest.mark.asyncio
async def test_heal_to_current_skips_symbols_missing_from_current_instruments():
    healer = object.__new__(GapHealer)
    healer.symbols = ["VALID", "DELISTED"]
    healer.instruments = Mock()
    healer.instruments.get_token.side_effect = lambda symbol: 123 if symbol == "VALID" else None
    healer._heal_symbol = AsyncMock(return_value=7)

    results = await healer.heal_to_current()

    assert results == {"VALID": 7, "DELISTED": 0}
    healer._heal_symbol.assert_awaited_once()


def test_fetch_symbol_converts_pandas_timestamps_to_native_datetimes():
    fetcher = object.__new__(HistoricalFetcher)
    fetcher.instruments = Mock()
    fetcher.instruments.get_token.return_value = 123
    fetcher._fetch_chunk = Mock(return_value=[])

    fetcher.fetch_symbol(
        "TEST",
        interval="minute",
        from_date=pd.Timestamp("2026-10-16T10:00:00Z"),
        to_date=pd.Timestamp("2026-10-16T11:00:00Z"),
        store=False,
    )

    _, _, from_date, to_date = fetcher._fetch_chunk.call_args.args
    assert type(from_date) is datetime
    assert type(to_date) is datetime
    assert from_date.tzinfo is timezone.utc


def test_missing_ranges_cover_interior_holes_and_partial_days_but_skip_holidays():
    from datetime import date

    def ist(day, clock):
        return pd.Timestamp(f"{day} {clock}", tz="Asia/Kolkata").to_pydatetime()

    # Stored calendar: Oct 1-16 and 19 (Oct 2 a holiday); the symbol has Oct 1-5 complete,
    # nothing for Oct 6-16 and only a partial live-ticker morning on Oct 19.
    sessions = {date(2026, 10, d) for d in (1, 5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 19)}
    complete = {date(2026, 10, 1), date(2026, 10, 5)}
    ranges = HistoricalFetcher.missing_ranges(complete, sessions, ist("2026-10-01", "00:00"),
                                              ist("2026-10-21", "12:00"))
    assert ranges == [(ist("2026-10-06", "09:15"), ist("2026-10-21", "12:00"))]  # Oct 19 partial, 20-21 unseen
    # Weekdays before anything was stored are fetched too.
    head = HistoricalFetcher.missing_ranges(complete, sessions, ist("2026-09-28", "00:00"), ist("2026-10-05", "16:00"))
    assert head == [(ist("2026-09-28", "09:15"), ist("2026-09-30", "15:30"))]
