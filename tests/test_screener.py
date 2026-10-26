"""Market screener: daily bars, technicals, presets, sector breadth, sector map and API."""

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trading_agent.analysis import screener
from trading_agent.analysis.screener import (
    daily_bars,
    filter_rows,
    mark_stale,
    screen_counts,
    screen_row,
    sector_breadth,
)
from trading_agent.analysis.screener_jobs import technicals_check
from trading_agent.core.quant_indicators import rsi
from trading_agent.ingest.sector_map import build_sector_map, load_sector_map, parse_index_csv, save_sector_map

IST = timezone(timedelta(hours=5, minutes=30))


def minute_frame(closes_per_session: list[list[float]], start=date(2026, 9, 1)) -> pd.DataFrame:
    """One bar per minute from 09:15 IST, one list of closes per weekday session."""
    rows, day = [], start
    for closes in closes_per_session:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        open_at = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
        for i, close in enumerate(closes):
            rows.append({"timestamp": (open_at + timedelta(minutes=i)).astimezone(timezone.utc),
                         "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 100})
        day += timedelta(days=1)
    return pd.DataFrame(rows)


def test_daily_bars_group_by_ist_session():
    frame = minute_frame([[10, 11, 12], [20, 19]])
    days = daily_bars(frame)
    assert list(days["close"]) == [12, 19]
    assert list(days["high"]) == [13, 21]
    assert list(days["volume"]) == [300, 200]


def test_screen_row_pivots_breakout_and_changes():
    # Previous session H/L/C = 101/89/100 -> pivot 96.67, R1 104.33, S1 92.33.
    frame = minute_frame([[90, 100], [100] * 60 + [102, 103, 104, 105, 106, 110]])
    row = screen_row("TEST", frame, {"industry": "Power", "indices": ["NIFTY50"]})
    assert row["prev_close"] == 100 and row["ltp"] == 110
    assert row["change_pct"] == pytest.approx(10.0)
    assert row["r1"] == pytest.approx(104.33, abs=0.01) and row["r1_breakout"] is True
    assert row["change_5m_pct"] == pytest.approx((110 / 102 - 1) * 100, abs=0.001)
    assert row["change_1h_pct"] == pytest.approx(10.0)
    assert row["industry"] == "Power" and row["indices"] == ["NIFTY50"]
    assert row["rsi"] is None and row["macd_hist"] is None  # two sessions is too little history


def test_rsi_uses_completed_sessions_and_live_value_is_separate():
    sessions = [[100 + i + (i % 3)] for i in range(40)] + [[60]]  # crash on the live bar
    row = screen_row("TEST", minute_frame(sessions))
    closes = np.array([100 + i + (i % 3) for i in range(40)], dtype=float)
    assert row["rsi"] == pytest.approx(rsi(closes, 14)[-1], abs=0.01)
    assert row["rsi_live"] < row["rsi"]
    assert row["macd_hist"] is not None


def test_quote_becomes_today_when_stored_bars_end_yesterday():
    frame = minute_frame([[90, 100], [100, 101, 99]])  # last stored session ends at 99
    last_day = pd.Timestamp(frame["timestamp"].iloc[-1]).tz_convert("Asia/Kolkata").date()
    nxt = last_day + timedelta(days=3 if last_day.weekday() == 4 else 1)
    quote = {"ltp": 110.0, "open": 100.0, "high": 112.0, "low": 98.0, "prev_close": 100.0, "volume": 5000,
             "time": datetime(nxt.year, nxt.month, nxt.day, 14, 0, tzinfo=IST)}
    row = screen_row("TEST", frame, quote=quote)
    assert row["session"] == nxt.isoformat() and row["price_source"] == "quote"
    assert row["ltp"] == 110 and row["prev_close"] == 100  # official close replaces the stored 99
    assert row["change_pct"] == pytest.approx(10.0) and row["volume"] == 5000
    assert row["change_5m_pct"] is None  # no minute bars for the quoted session
    assert row["year_high"] == 102 and row["new_52w_high"] is True  # completed sessions only


def test_week_average_uses_previous_seven_calendar_days():
    frame = minute_frame([[100]] * 12)
    frame["volume"] = np.arange(1, len(frame) + 1) * 100  # session n has volume 100 n
    row = screen_row("TEST", frame)
    days = daily_bars(frame)
    session = days.index[-1]
    window = [v for d, v in days["volume"].iloc[:-1].items() if d >= session - timedelta(days=7)]
    assert row["volume_week_avg"] == int(np.mean(window)) and len(window) == 5


def test_parse_quote_and_batched_fetch(monkeypatch):
    from trading_agent.ingest import live_quotes

    raw = {"last_price": 12.5, "volume": 900, "ohlc": {"open": 12, "high": 13, "low": 11.9, "close": 12.1},
           "timestamp": datetime(2026, 10, 26, 15, 0)}
    parsed = live_quotes.parse_quote(raw, datetime(2026, 10, 26, 9, 30, tzinfo=timezone.utc))
    assert parsed["time"].utcoffset() == timedelta(hours=5, minutes=30) and parsed["prev_close"] == 12.1
    assert live_quotes.parse_quote({"last_price": 0}, datetime.now(timezone.utc)) is None

    calls = []

    class FakeKite:
        def quote(self, keys):
            calls.append(len(keys))
            return {key: raw for key in keys}

    monkeypatch.setattr(live_quotes.time, "sleep", lambda seconds: None)
    quotes = live_quotes.fetch_quotes([f"S{i}" for i in range(1201)], kite=FakeKite())
    assert calls == [500, 500, 201] and len(quotes) == 1201 and quotes["S7"]["ltp"] == 12.5


def test_mark_stale_flags_older_sessions_and_ticker_gaps():
    rows = [{"session": "2026-10-26", "as_of": "2026-10-26T09:27:00+00:00"},
            {"session": "2026-10-26", "as_of": "2026-10-26T03:48:00+00:00"},
            {"session": "2026-10-23", "as_of": "2026-10-23T09:59:00+00:00"}]
    assert mark_stale(rows) == "2026-10-26"
    assert [r["stale"] for r in rows] == [False, True, True]
    after_close = [{"session": "2026-10-26", "as_of": "2026-10-26T15:48:00+05:30"},
                   {"session": "2026-10-26", "as_of": "2026-10-26T15:31:00+05:30"},
                   {"session": "2026-10-26", "as_of": "2026-10-26T15:05:00+05:30"}]
    mark_stale(after_close)  # closing-minute prices are final; lag counts to 15:30
    assert [r["stale"] for r in after_close] == [False, False, True]
    check = technicals_check([{**r, "rsi": 50, "macd_hist": 1, "full_year": True, "industry": "x"} for r in rows])
    assert (check["current"], check["stale"], check["lagging"]) == (1, 2, 1)


ROWS = [
    {"symbol": "A", "industry": "Power", "indices": ["NIFTY50"], "change_pct": 2.0, "turnover": 5e7, "rsi": 75,
     "macd_hist": 1.0, "r1_breakout": True, "stale": False},
    {"symbol": "B", "industry": "Power", "indices": [], "change_pct": -2.0, "turnover": 9e7, "rsi": 25,
     "macd_hist": -1.0, "r1_breakout": False, "stale": False},
    {"symbol": "C", "industry": "Banks", "indices": ["NIFTY50"], "change_pct": 0.5, "turnover": 1e7, "rsi": None,
     "macd_hist": None, "r1_breakout": False, "stale": False},
    {"symbol": "D", "industry": "Banks", "indices": [], "change_pct": 9.0, "turnover": 1e9, "rsi": 80,
     "macd_hist": 2.0, "r1_breakout": True, "stale": True},
]


def test_filter_rows_presets_skip_stale_and_missing_fields():
    assert [r["symbol"] for r in filter_rows(ROWS)] == ["B", "A", "C"]  # turnover desc, stale D excluded
    assert [r["symbol"] for r in filter_rows(ROWS, [("rsi", "ge", 70)])] == ["A"]
    assert [r["symbol"] for r in filter_rows(ROWS, [("indices", "has", "NIFTY50")], sort="change_pct")] == ["A", "C"]
    counts = {s["id"]: s["count"] for s in screen_counts(ROWS)}
    assert counts["resistance-breakout"] == 1 and counts["rsi-oversold"] == 1 and counts["macd-above-signal"] == 1
    with pytest.raises(ValueError):
        filter_rows(ROWS, sort="nope")


def test_sector_breadth_counts_and_equal_weighted_mean():
    breadth = sector_breadth(ROWS + [{"symbol": "E", "industry": None, "change_pct": 1.0, "turnover": 1, "stale": False}])
    by_name = {s["sector"]: s for s in breadth["sectors"]}
    assert by_name["Power"]["advances"] == 1 and by_name["Power"]["declines"] == 1
    assert by_name["Power"]["avg_change_pct"] == pytest.approx(0.0)
    assert by_name["Banks"]["stocks"] == 1  # stale D left out
    assert breadth["unclassified_stocks"] == 1
    assert [s["sector"] for s in breadth["sectors"]] == ["Banks", "Power"]


def test_sector_map_parse_merge_round_trip(tmp_path):
    csv_text = "Company Name,Industry,Symbol,Series,ISIN Code\nReliance Industries Ltd.,Oil Gas & Consumable Fuels,RELIANCE,EQ,X\n"
    rows = parse_index_csv(csv_text)
    merged = build_sector_map({"NIFTY50": rows, "NIFTY100": rows})
    path = save_sector_map(merged, tmp_path / "sector_map.csv")
    loaded = load_sector_map(path)
    assert loaded["RELIANCE"]["industry"] == "Oil Gas & Consumable Fuels"
    assert loaded["RELIANCE"]["indices"] == ["NIFTY100", "NIFTY50"]
    assert load_sector_map(tmp_path / "missing.csv") == {}


class FakeJobs:
    def __init__(self, rows):
        self._rows = rows

    def rows(self):
        return [dict(r) for r in self._rows]

    def snapshot(self, rows=True):
        return {"status": "completed", "processed": 4, "total": 4, "latest_session": "2026-10-26",
                "skipped": 0, "errors": [], "check": None}


def test_screener_api_filters_and_validation(monkeypatch):
    from trading_agent.api.routers import screener as router_module

    monkeypatch.setattr(router_module, "get_screener_jobs", lambda: FakeJobs(ROWS))
    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/v1/screener")
    client = TestClient(app)

    body = client.get("/api/v1/screener/stocks", params={"screen": "resistance-breakout"}).json()
    assert [r["symbol"] for r in body["rows"]] == ["A"] and body["status"] == "completed"
    body = client.get("/api/v1/screener/stocks", params={"sector": "Power", "rsi_max": 30}).json()
    assert [r["symbol"] for r in body["rows"]] == ["B"]
    assert client.get("/api/v1/screener/stocks", params={"screen": "nope"}).status_code == 404
    assert client.get("/api/v1/screener/stocks", params={"sort": "nope"}).status_code == 422
    assert client.get("/api/v1/screener/stocks", params={"rsi_min": 101}).status_code == 422
    sectors = client.get("/api/v1/screener/sectors").json()
    assert [s["sector"] for s in sectors["sectors"]] == ["Banks", "Power"]
    assert len(client.get("/api/v1/screener/screens").json()) == len(screener.PRESETS)
    assert client.get("/api/v1/screener/symbols/..%2Fx").status_code in {404, 422}
