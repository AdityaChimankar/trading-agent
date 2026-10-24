"""Regression tests for features restored on unified-platform (Phase 1)."""
import asyncio
import sqlite3
import threading
import time
from datetime import UTC, datetime

import httpx
import numpy as np
import pandas as pd
import pytest

from trading_agent.core import quant_indicators as qi
from trading_agent.core.exceptions import ModelError
from trading_agent.core.llm_client import LLMRequest, OpenAICompatibleClient, _RateLimiter
from trading_agent.core.pattern_detection import detect_doji
from trading_agent.research import walk_forward_optimizer as wf


# --- storage ---------------------------------------------------------------

def test_sqlite_connections_use_wal(tmp_path, monkeypatch):
    from trading_agent.storage import db

    path = tmp_path / "wal.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    db.init_db(str(path))
    conn = db.get_connection()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_sqlalchemy_engine_uses_wal(tmp_path):
    from trading_agent.storage.db import Database

    database = Database(f"sqlite:///{tmp_path / 'engine.db'}")
    with database.sync_engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
    database.sync_engine.dispose()


# --- live ticker -----------------------------------------------------------

def test_background_writer_runs_handler_off_the_calling_thread():
    from trading_agent.ingest.live_ticker import BackgroundCandleWriter, CandleData

    seen, release = [], threading.Event()

    def slow_handler(candle):
        release.wait(5)
        seen.append((candle.symbol, threading.current_thread().name))

    writer = BackgroundCandleWriter(slow_handler)
    candle = CandleData("INFY", "minute", datetime.now(UTC), 1, 1, 1, 1, 10)
    started = time.perf_counter()
    writer(candle)
    # The reactor thread must not wait for the blocked write.
    assert time.perf_counter() - started < 0.5
    release.set()
    writer.close()
    assert seen == [("INFY", "candle-writer")]


def test_background_writer_survives_handler_errors():
    from trading_agent.ingest.live_ticker import BackgroundCandleWriter, CandleData

    seen = []

    def flaky(candle):
        if candle.symbol == "BAD":
            raise OSError("disk full")
        seen.append(candle.symbol)

    writer = BackgroundCandleWriter(flaky)
    for symbol in ("BAD", "OK"):
        writer(CandleData(symbol, "minute", datetime.now(UTC), 1, 1, 1, 1, 1))
    writer.close()
    assert seen == ["OK"]


# --- LLM throttle / retry ----------------------------------------------------

def _client(handler, **kwargs):
    client = OpenAICompatibleClient("https://llm.test", "key", "model", **kwargs)
    client._client = httpx.AsyncClient(base_url="https://llm.test",
                                       transport=httpx.MockTransport(handler))
    return client


def _ok(content="hi"):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "model": "m"})


def test_llm_retries_429_honouring_retry_after(monkeypatch):
    import trading_agent.core.llm_client as llm

    monkeypatch.setattr(llm, "_rate_limiter", _RateLimiter())
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "0"}) if len(calls) < 3 else _ok("done")

    reply = asyncio.run(_client(handler, max_retries=4).complete(LLMRequest("s", "u")))
    assert reply.content == "done" and len(calls) == 3


def test_llm_gives_up_after_max_retries(monkeypatch):
    import trading_agent.core.llm_client as llm

    monkeypatch.setattr(llm, "_rate_limiter", _RateLimiter())
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "0"})

    with pytest.raises(ModelError, match="429"):
        asyncio.run(_client(handler, max_retries=2).complete(LLMRequest("s", "u")))
    assert len(calls) == 3


def test_llm_does_not_retry_client_errors(monkeypatch):
    import trading_agent.core.llm_client as llm

    monkeypatch.setattr(llm, "_rate_limiter", _RateLimiter())
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, text="bad key")

    with pytest.raises(ModelError, match="401"):
        asyncio.run(_client(handler).complete(LLMRequest("s", "u")))
    assert len(calls) == 1


def test_llm_throttle_spaces_calls(monkeypatch):
    import trading_agent.core.llm_client as llm

    monkeypatch.setattr(llm, "_rate_limiter", _RateLimiter())
    stamps = []

    def handler(request):
        stamps.append(time.monotonic())
        return _ok()

    async def two_calls():
        client = _client(handler, min_interval_seconds=0.3)
        await client.complete(LLMRequest("s", "u"))
        await client.complete(LLMRequest("s", "u"))

    asyncio.run(two_calls())
    assert stamps[1] - stamps[0] >= 0.28


def test_openrouter_provider_uses_openrouter_key(monkeypatch):
    from trading_agent.core import config, llm_client

    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.openai.com")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    config.get_settings.cache_clear()
    try:
        client = llm_client.create_llm_client()
        assert isinstance(client, OpenAICompatibleClient)
        assert client.base_url == llm_client.OPENROUTER_BASE_URL
        assert client.api_key == "or-key"
        assert client.min_interval_seconds == 4.5
    finally:
        config.get_settings.cache_clear()


# --- indicators, patterns, technical summary --------------------------------

def _ohlc(close):
    close = np.asarray(close, dtype=float)
    return close + 1, close - 1, close


def test_williams_r_and_cci_ranges():
    h, lo, c = _ohlc(np.linspace(100, 200, 60))
    wr = qi.williams_r(h, lo, c)
    assert np.nanmin(wr) >= -100 and np.nanmax(wr) <= 0
    assert wr[-1] > -20  # closing at the top of the range = overbought
    assert qi.cci(h, lo, c)[-1] > 100  # steady uptrend = strong momentum


def test_directional_indicators_follow_trend():
    h, lo, c = _ohlc(np.linspace(100, 200, 60))
    plus_di, minus_di = qi.directional_indicators(h, lo, c)
    assert plus_di[-1] > minus_di[-1]


def test_doji_detection():
    open_ = np.array([100.0, 100.0])
    close = np.array([100.05, 103.0])
    high = np.array([101.0, 103.5])
    low = np.array([99.0, 99.5])
    assert detect_doji(open_, high, low, close).tolist() == [True, False]


def test_technical_votes_counts_and_verdict():
    from trading_agent.analysis.technical_summary import technical_votes, verdict_label

    n = 120
    close = np.linspace(100, 160, n) + np.random.default_rng(1).normal(0, 0.8, n)
    df = pd.DataFrame({"timestamp": pd.date_range("2025-01-01", periods=n, freq="min"),
                       "open": close - 0.2, "high": close + 1, "low": close - 1, "close": close,
                       "volume": 1000})
    votes = technical_votes(df)
    assert votes["total"] == votes["bullish_count"] + votes["bearish_count"] + votes["neutral_count"]
    assert {r["name"] for r in votes["readings"]} >= {"RSI (14)", "Williams %R", "CCI (20)", "ADX/DI (14)"}
    assert votes["verdict"] == verdict_label(votes["bullish_count"] - votes["bearish_count"])
    assert technical_votes(df.head(10)) is None
    assert verdict_label(6) == "Strongly bullish" and verdict_label(-6) == "Strongly bearish"


# --- instruments -------------------------------------------------------------

def test_instrument_resolve_splits_known_and_unknown(tmp_path):
    from trading_agent.ingest.instrument_lookup import InstrumentLookup

    lookup = InstrumentLookup(tmp_path / "i.csv.gz")
    lookup._symbol_to_token = {"INFY": 408065}
    assert lookup.resolve(["INFY", "GONE"]) == ({"INFY": 408065}, ["GONE"])


def test_instrument_lookup_falls_back_to_stale_cache(tmp_path, monkeypatch):
    import os

    from trading_agent.core.exceptions import DataError
    from trading_agent.ingest.instrument_lookup import InstrumentLookup

    cache = tmp_path / "i.csv.gz"
    pd.DataFrame([{"tradingsymbol": "INFY", "instrument_token": 408065, "segment": "NSE",
                   "instrument_type": "EQ"}]).to_csv(cache, index=False, compression="gzip")
    old = time.time() - 3 * 86400
    os.utime(cache, (old, old))
    lookup = InstrumentLookup(cache)
    monkeypatch.setattr(lookup, "_download", lambda: (_ for _ in ()).throw(DataError("offline")))
    lookup.load()
    assert lookup.get_token("INFY") == 408065


# --- backtest selftest -------------------------------------------------------

def _synthetic_prepared():
    return wf._prepared_from_frame("SYN", wf.synthetic_frame(n=3000))


def test_selftest_compares_real_trades():
    prepared, codes = _synthetic_prepared()
    full = (0, prepared["length"])
    loose = {"pattern_confirm_buy_rsi": 50, "pattern_confirm_sell_rsi": 50, "adx_threshold": 15}
    # A selftest that compares two empty lists proves nothing.
    assert len(wf.loop_trades(prepared, codes, loose, full)) > 5
    assert all(wf.verify_vectorized_matches_loop(prepared, codes, full, p)
               for p in [None, *wf._grid_combinations()])


def test_selftest_detects_a_vectorization_bug(monkeypatch):
    prepared, codes = _synthetic_prepared()
    loose = {"pattern_confirm_buy_rsi": 50, "pattern_confirm_sell_rsi": 50, "adx_threshold": 15}
    original = wf.vectorized_evaluate

    def off_by_one_threshold(rsi, adx, pattern_codes, params):
        return original(rsi, adx, pattern_codes, {**(params or {}), "adx_threshold": params["adx_threshold"] + 5})

    monkeypatch.setattr(wf, "vectorized_evaluate", off_by_one_threshold)
    assert not wf.verify_vectorized_matches_loop(prepared, codes, (0, prepared["length"]), loose)


def test_walk_forward_multi_passes_param_grid_to_workers(monkeypatch):
    seen = []
    monkeypatch.setattr(wf, "run_walk_forward",
                        lambda symbol, verbose=True, **kw: seen.append(kw["param_grid"]) or {})
    wf.run_walk_forward_multi(["A", "B"], parallel=False, param_grid={"adx_threshold": [25]})
    assert seen == [{"adx_threshold": [25]}] * 2


def test_simulate_trades_accepts_default_and_override_params():
    from trading_agent.core.signals import prepare_symbol_data, simulate_trades

    prepared = prepare_symbol_data(wf.synthetic_frame(n=800))
    simulate_trades(prepared)  # used to crash: SignalParams is not subscriptable
    strict = simulate_trades(prepared, {"adx_threshold": 1000})
    assert strict == []


# --- lookahead check ---------------------------------------------------------

def _feature_input(n=1200):
    from trading_agent.core.signals import prepare_symbol_data

    frame = wf.synthetic_frame(n=n)
    return prepare_symbol_data(frame)


def test_session_features_are_causal():
    from trading_agent.strategy import session_features as sf

    ok, mismatched = sf.verify_causal(_feature_input())
    assert ok, mismatched
    assert sf.verify_sanity(_feature_input()) == []


def test_verify_causal_catches_lookahead(monkeypatch):
    from trading_agent.strategy import session_features as sf

    def leaky_cumsum(values, groups):
        # Full-series total at every index: peeks at future candles.
        return np.full(len(values), np.nansum(values))

    monkeypatch.setattr(sf, "_group_cumsum", leaky_cumsum)
    ok, mismatched = sf.verify_causal(_feature_input())
    assert not ok and "vwap" in mismatched


def test_candle_timestamp_normalizes_forms():
    from trading_agent.core.freshness import candle_timestamp

    aware = datetime(2025, 9, 29, 3, 45, 30, tzinfo=UTC)
    assert candle_timestamp(aware) == "2025-09-29T09:15:00"
    assert candle_timestamp("2025-09-29T09:15:42+05:30") == "2025-09-29T09:15:00"
