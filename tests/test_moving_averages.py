"""Moving-average variants: optional filter on the rule, EMA crossover, chart lines."""
import numpy as np
import pandas as pd

from trading_agent.core.signals import apply_ma_filter, ma_trend

IST = "Asia/Kolkata"


def _frame(closes, day="2026-10-19", volume=1000.0):
    ts = pd.date_range(f"{day} 09:15", periods=len(closes), freq="min", tz=IST).tz_convert("UTC")
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"timestamp": ts, "open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": volume})


def test_ma_filter_keeps_only_trend_aligned_signals():
    trend = ma_trend(np.array([101.0, 99.0, 100.0, 101.0]), np.array([100.0, 100.0, 100.0, 100.0]))
    assert trend.tolist() == [1, -1, 0, 1]
    actions = np.array([1, 1, 2, 2], dtype=np.int8)
    assert apply_ma_filter(actions, trend).tolist() == [1, 0, 0, 0]
    assert actions.tolist() == [1, 1, 2, 2]  # the rule output itself is not modified


def test_ema_and_vwap_are_causal_and_vwap_resets_each_session():
    from trading_agent.strategy.research_features import prepare_research_frame

    rng = np.random.default_rng(5)
    day1 = _frame(100 + rng.normal(0, 0.2, 120).cumsum())
    day2 = _frame(100 + rng.normal(0, 0.2, 120).cumsum(), day="2026-10-20")
    base = pd.concat([day1, day2], ignore_index=True)
    first = prepare_research_frame(base)
    changed = base.copy()
    changed.loc[200:, ["high", "low", "close"]] *= 1.2
    second = prepare_research_frame(changed)
    for col in ("ema9", "ema21", "vwap"):
        pd.testing.assert_series_equal(first.loc[:199, col], second.loc[:199, col])
    # The first bar of a session has VWAP equal to its own typical price.
    row = first.iloc[120]
    assert row.vwap == (row.high + row.low + row.close) / 3


def test_ema_cross_profile_fires_on_the_cross_bar_only():
    from trading_agent.research.strategy_lab import profile_actions

    closes = list(np.linspace(110, 100, 80)) + list(np.linspace(100, 115, 80))
    frame = _frame(closes).assign(ema9=None, ema21=None)
    frame["ema9"] = frame.close.ewm(span=9, adjust=False).mean()
    frame["ema21"] = frame.close.ewm(span=21, adjust=False).mean()
    frame["vwap"], frame["adx"] = 90.0, 30.0
    actions = profile_actions(frame, "ema_cross")
    cross = int(np.argmax((frame.ema9 > frame.ema21).to_numpy()))
    assert np.flatnonzero(actions == 1).tolist() == [cross]
    assert not (actions == 2).any()  # never closes below a 90 VWAP


def test_chart_payload_includes_intraday_averages():
    from trading_agent.analysis.research_engine import chart_payload
    from trading_agent.strategy.research_features import prepare_research_frame

    df = prepare_research_frame(_frame(100 + np.random.default_rng(2).normal(0, 0.2, 120).cumsum()))
    payload = chart_payload("TEST", df, 50)
    assert {"ema9", "ema21", "vwap"} <= set(payload["indicators"])
    assert len(payload["indicators"]["vwap"]) == 50
