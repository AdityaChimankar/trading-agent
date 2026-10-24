"""Opening-range breakout + Nifty gap regime, U-shape volume forecast, cross-sectional ranking."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from trading_agent.core.config import get_settings

IST = "Asia/Kolkata"


def _minutes(day: str, closes: list[float], start: str = "09:15") -> pd.DataFrame:
    ts = pd.date_range(f"{day} {start}", periods=len(closes), freq="min", tz=IST).tz_convert("UTC")
    c = np.array(closes, dtype=float)
    return pd.DataFrame({"timestamp": ts, "open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 100.0})


# --- opening-range breakout --------------------------------------------------------

def test_opening_range_breakout_is_causal_and_fires_once_per_side():
    from trading_agent.research.strategy_lab import opening_range_actions

    # 15 flat range bars (100 +/- 0.1), then a breakout up, more highs, then a breakdown.
    closes = [100.0] * 15 + [100.5, 101.0, 100.0, 99.5, 99.0]
    frame = _minutes("2026-10-19", closes)
    actions = opening_range_actions(frame)
    assert actions[:15].tolist() == [0] * 15          # no signal inside the window
    assert actions[15] == 1 and actions[16] == 0      # first close above 100.1 only
    assert actions[18] == 2 and actions[19] == 0      # first close below 99.9 only

    # A session without its 09:15 bar has no opening range.
    late = _minutes("2026-10-20", closes, start="09:20")
    assert not opening_range_actions(late).any()

    # A spike after the window must not change earlier signals.
    spiked = frame.copy()
    spiked.loc[19, ["high", "close"]] = [130.0, 129.9]
    assert opening_range_actions(spiked)[:19].tolist() == actions[:19].tolist()


def test_gap_regime_filters_sides():
    from trading_agent.research.strategy_lab import index_gap_regime, opening_range_actions

    index = pd.concat([_minutes("2026-10-16", [100.0] * 5), _minutes("2026-10-19", [101.0] * 5),
                       _minutes("2026-10-20", [100.9] * 5)], ignore_index=True)
    regime = index_gap_regime(index)
    assert regime == {date(2026, 10, 19): "GAP_UP", date(2026, 10, 20): "FLAT"}

    closes = [100.0] * 15 + [100.5, 99.0]
    gap_up_day = _minutes("2026-10-19", closes)
    actions = opening_range_actions(gap_up_day, regime=regime)
    assert 1 not in actions and actions[16] == 2      # gap up: shorts only
    flat_day = _minutes("2026-10-20", closes)
    assert opening_range_actions(flat_day, regime=regime)[15] == 1
    unknown = _minutes("2026-10-21", closes)
    assert not opening_range_actions(unknown, regime=regime).any()


def test_strategy_lab_reports_orb_and_skips_gap_profile_without_index():
    from trading_agent.research.strategy_lab import compare_profiles

    rng = np.random.default_rng(1)
    days = pd.bdate_range("2026-09-01", periods=6)
    frames = []
    for d in days:
        closes = 100 + rng.normal(0, 0.2, 375).cumsum()
        frames.append(_minutes(d.strftime("%Y-%m-%d"), list(closes)))
    raw = pd.concat(frames, ignore_index=True)
    raw["high"], raw["low"] = raw[["open", "close"]].max(axis=1) + 0.05, raw[["open", "close"]].min(axis=1) - 0.05
    result = compare_profiles(raw, "TEST", "minute")
    assert result["status"] == "completed"
    assert "orb" in {p["id"] for p in result["profiles"]}
    assert result["skipped_profiles"][0]["id"] == "orb_gap"

    index = raw.assign(open=raw.open * 2, close=raw.close * 2, high=raw.high * 2, low=raw.low * 2)
    with_index = compare_profiles(raw, "TEST", "minute", index_frame=index)
    assert "orb_gap" in {p["id"] for p in with_index["profiles"]} and not with_index["skipped_profiles"]


def test_index_symbols_resolve_without_instrument_dump():
    from trading_agent.ingest.instrument_lookup import INDEX_TOKENS, InstrumentLookup

    lookup = InstrumentLookup.__new__(InstrumentLookup)
    lookup._symbol_to_token, lookup._token_to_symbol = {"X": 1}, {1: "X"}
    assert lookup.get_token("NIFTY50") == INDEX_TOKENS["NIFTY50"] == 256265
    assert lookup.get_symbol(260105) == "BANKNIFTY"


# --- volume forecast ------------------------------------------------------------------

def _u_shape_sessions(n=30, seed=4):
    rng = np.random.default_rng(seed)
    minutes = np.arange(375)
    shape = 1 + 3 * np.exp(-minutes / 30) + 2 * np.exp(-(374 - minutes) / 30)
    frames = []
    for d in pd.bdate_range("2026-08-03", periods=n):
        level = rng.lognormal(0, 0.4)
        volume = level * shape * rng.lognormal(0, 0.2, 375) * 1000
        ts = pd.date_range(f"{d.date()} 09:15", periods=375, freq="min", tz=IST).tz_convert("UTC")
        frames.append(pd.DataFrame({"timestamp": ts, "volume": volume}))
    return pd.concat(frames, ignore_index=True)


def test_forecast_blends_profile_and_pace():
    from trading_agent.analysis.volume_insights import forecast_day_volume

    sessions = pd.DataFrame({"session": np.repeat(np.arange(6), 2), "minute": [600, 900] * 6,
                             "volume": [300.0, 700.0] * 6})
    out = forecast_day_volume(sessions, today_cum=600.0, now_minute=600)
    # 30% of a 1,000-share day is normally done by 10:00; today's pace implies 2,000.
    assert out["expected_share_traded"] == pytest.approx(0.3)
    weight = 0.3 ** 0.5
    blended_total = weight * 2000 + (1 - weight) * 1000
    assert out["forecast_remaining_volume"] == pytest.approx(0.7 * blended_total)
    assert forecast_day_volume(sessions[sessions.session < 4], 600.0, 600) is None  # too few sessions


def test_volume_forecast_beats_baselines_out_of_sample():
    from trading_agent.analysis.volume_insights import evaluate_volume_forecast

    report = evaluate_volume_forecast(_u_shape_sessions())
    assert report["status"] == "completed" and report["sessions_evaluated"] >= 20
    assert report["beats_baselines"] is True
    assert evaluate_volume_forecast(_u_shape_sessions(n=4))["status"] == "insufficient_history"


def test_router_caps_size_at_forecast_remaining_volume(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from trading_agent.api.main import create_app
    from trading_agent.storage import db as db_module

    path = tmp_path / "ledger.db"
    monkeypatch.setenv("DB_URL", f"sqlite:///{path.as_posix()}")
    monkeypatch.setenv("API_AUTH_TOKEN", "")
    monkeypatch.setattr(db_module, "DB_PATH", str(path))
    profile = {"liquidity": "high", "reason": "", "max_position_value": 1e9, "participation_pct": 1.0,
               "max_quantity": 10**6, "max_quantity_remaining": 5, "remaining_participation_pct": 5.0}
    monkeypatch.setattr("trading_agent.analysis.volume_insights.symbol_volume", lambda symbol, interval="minute": profile)
    get_settings.cache_clear()
    db_module.reset_database()
    entry = {"symbol": "TEST", "side": "BUY", "quantity": 10, "reference_price": 100.0, "stop_loss": 95.0}
    try:
        with TestClient(create_app()) as client:
            response = client.post("/api/v1/execution/enter", json=entry)
            assert response.status_code == 409 and "forecast remaining volume" in response.json()["detail"]
            assert client.post("/api/v1/execution/enter", json={**entry, "quantity": 5}).status_code == 200
    finally:
        db_module.reset_database()
        get_settings.cache_clear()


# --- cross-sectional ranking -------------------------------------------------------------

def _panel(n_symbols=40, n_days=260, reversal=0.0, seed=7):
    """Daily opens/closes; ``reversal`` < 0 plants next-day mean reversion of intraday returns."""
    rng = np.random.default_rng(seed)
    sessions = [d.date() for d in pd.bdate_range("2025-06-02", periods=n_days)]
    ret = np.zeros((n_days, n_symbols))
    for t in range(n_days):
        noise = rng.normal(0, 0.01, n_symbols)
        ret[t] = noise + (reversal * ret[t - 1] if t else 0)
    gaps = rng.normal(0, 0.003, (n_days, n_symbols))
    opens, closes = np.zeros_like(ret), np.zeros_like(ret)
    prev = np.full(n_symbols, 100.0)
    for t in range(n_days):
        opens[t] = prev * (1 + gaps[t])
        closes[t] = opens[t] * (1 + ret[t])
        prev = closes[t]
    cols = [f"S{i:02d}" for i in range(n_symbols)]
    return pd.DataFrame(opens, index=sessions, columns=cols), pd.DataFrame(closes, index=sessions, columns=cols)


FAST = dict(max_lag=5, train_days=60, retrain_days=20, top_k=5, cost_bps=5.0, n_estimators=60,
            min_test_days=60, random_draws=20)


def test_daily_bars_need_open_and_closing_bars():
    from trading_agent.research.cross_sectional import daily_bars

    full = _minutes("2026-10-19", list(np.linspace(100, 101, 375)))
    partial = _minutes("2026-10-20", [100.0] * 30)          # ends 09:44: open only
    late = _minutes("2026-10-21", [100.0] * 30, start="15:00")  # no 09:15 bar: close only
    bars = daily_bars(pd.concat([full, partial, late], ignore_index=True))
    assert bars.loc[date(2026, 10, 19), ["open", "close"]].tolist() == pytest.approx([100.0, 101.0])
    # MIS exit: close of the 15:14 bar (index 359 of 375), not the 15:29 close.
    assert bars.loc[date(2026, 10, 19), "exit"] == pytest.approx(full.close.iloc[359])
    assert np.isnan(bars.loc[date(2026, 10, 20), "exit"])
    assert np.isnan(bars.loc[date(2026, 10, 20), "close"]) and bars.loc[date(2026, 10, 20), "open"] == 100.0
    assert np.isnan(bars.loc[date(2026, 10, 21), "open"])


def test_features_only_use_information_known_at_the_open():
    from trading_agent.research.cross_sectional import build_features

    opens, closes = _panel(n_symbols=4, n_days=30)
    base = build_features(opens, closes, (1, 2, 5))
    changed_closes = closes.copy()
    changed_closes.iloc[20:] *= 1.5  # alter closes from session 20 on
    changed = build_features(opens, changed_closes, (1, 2, 5))
    target = opens.index[20]
    features = [c for c in base.columns if c[:6] in ("IntraR", "CloseR", "OverNR")]
    pd.testing.assert_frame_equal(base.xs(target, level="session")[features],
                                  changed.xs(target, level="session")[features])


def test_planted_reversal_passes_gate_and_publishes_ranking():
    from trading_agent.research.cross_sectional import RankConfig, evaluate

    opens, closes = _panel(reversal=-0.6)
    report = evaluate(opens, closes, RankConfig(**FAST))
    assert report["status"] == "completed" and report["promotion_eligible"] is False
    assert report["model"]["mean_daily_net_bps"] > report["random_baseline"]["mean_daily_net_bps"]
    assert report["research_gate"]["passed"] is True
    ranking = report["latest_ranking"]
    assert len(ranking["long"]) == len(ranking["short"]) == 5
    assert not {r["symbol"] for r in ranking["long"]} & {r["symbol"] for r in ranking["short"]}


def test_pure_noise_fails_gate_and_withholds_ranking():
    from trading_agent.research.cross_sectional import RankConfig, evaluate

    opens, closes = _panel(reversal=0.0, seed=11)
    report = evaluate(opens, closes, RankConfig(**{**FAST, "cost_bps": 15.0}))
    assert report["status"] == "completed"
    assert report["research_gate"]["passed"] is False and report["latest_ranking"] is None


def test_short_history_is_reported_not_evaluated():
    from trading_agent.research.cross_sectional import RankConfig, evaluate

    opens, closes = _panel(n_days=80)
    report = evaluate(opens, closes, RankConfig(**FAST))
    assert report["status"] == "insufficient_history" and "usable sessions" in report["reason"]


def test_cross_sectional_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from trading_agent.api.main import create_app
    from trading_agent.research import cross_sectional

    monkeypatch.setenv("DB_URL", f"sqlite:///{(tmp_path / 'ledger.db').as_posix()}")
    monkeypatch.setenv("API_AUTH_TOKEN", "")
    get_settings.cache_clear()
    calls = []
    monkeypatch.setattr(cross_sectional, "run", lambda symbols, interval, config: calls.append(config))
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/analytics/cross-sectional").json()["report"] is None
        assert client.post("/api/v1/analytics/cross-sectional/run", json={"top_k": 3}).status_code == 202
        for _ in range(50):
            if not cross_sectional.get_job().state["running"]:
                break
            import time
            time.sleep(0.05)
        assert calls and calls[0].top_k == 3
    get_settings.cache_clear()


def test_liquidity_filter_and_exit_return():
    from trading_agent.research.cross_sectional import build_features

    opens, closes = _panel(n_symbols=4, n_days=40)
    exits = opens * 1.01
    values = pd.DataFrame(5e8, index=opens.index, columns=opens.columns)
    values["S00"] = 1e7  # INR 1 crore a day: below a 10 crore threshold
    table = build_features(opens, closes, (1, 2), exits=exits, values=values, min_avg_value=1e8)
    assert "S00" not in table.index.get_level_values("symbol")
    assert table["ret"].to_numpy() == pytest.approx(0.01)
