"""Decision outcomes: next-bar entry, horizon/15:15 exit, statuses, signs, costs, alpha; LLM model tiers."""
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from trading_agent.research import decision_outcomes as do

IST = "Asia/Kolkata"


def _bars(day="2026-10-23", start="09:15", end="15:29", freq="5min", step=0.1):
    ts = pd.date_range(f"{day} {start}", f"{day} {end}", freq=freq, tz=IST).tz_convert("UTC")
    price = 100 + np.arange(len(ts)) * step
    return pd.DataFrame({"timestamp": ts, "open": price, "high": price + 0.2, "low": price - 0.2,
                         "close": price + 0.05, "volume": 100.0})


def ist(stamp):
    return pd.Timestamp(stamp, tz=IST).tz_convert("UTC")


class FakeStore:
    def __init__(self, frames):
        self.frames = frames

    def read_candles(self, symbol, interval, start=None):
        if symbol not in self.frames:
            raise KeyError(symbol)
        return self.frames[symbol]


def test_price_window_enters_on_the_next_bar_and_exits_by_horizon_or_1515():
    bars = pd.concat([_bars(), _bars(day="2026-10-26")])
    p = do.price_window(bars, ist("2026-10-23 10:02"), 60)
    assert p["status"] == "scored"
    assert p["entry_time"] == ist("2026-10-23 10:05") and p["exit_time"] == ist("2026-10-23 11:00")
    late = do.price_window(bars, ist("2026-10-23 14:50"), 60)
    assert late["exit_time"] == ist("2026-10-23 15:10")          # capped at the 15:15 MIS exit
    assert do.price_window(bars, ist("2026-10-23 15:16"), 60)["status"] == "after_cutoff"
    today = _bars(end="11:30")                                    # store stops before the window ends
    assert do.price_window(today, ist("2026-10-23 11:00"), 60)["status"] == "pending"
    thin = _bars(end="10:00")                                     # no bar after the signal that day
    assert do.price_window(pd.concat([thin, _bars(day="2026-10-26")]), ist("2026-10-23 12:00"), 60)["status"] == "unpriced"


def _signals(rows):
    return pd.DataFrame([{"id": i, "symbol": s, "timestamp": ist(t), "interval": "5minute", "action": a,
                          "strategy": k, "confidence": c} for i, (s, t, a, k, c) in enumerate(rows)])


def test_scores_are_signed_net_of_costs_with_alpha_and_holds_counted():
    bars = pd.concat([_bars(), _bars(day="2026-10-26")])
    index = pd.concat([_bars(step=0.05), _bars(day="2026-10-26", step=0.05)])
    store = FakeStore({"AAA": bars, do.INDEX: index})
    signals = _signals([("AAA", "2026-10-23 10:02", "BUY", "llm", 0.8), ("AAA", "2026-10-23 10:02", "SELL", "rule", 0.7),
                        ("AAA", "2026-10-23 10:02", "HOLD", "llm", 0.5), ("AAA", "2026-10-23 15:20", "BUY", "ml", 0.9)])
    config = do.ScoreConfig(horizon_minutes=60, cost_bps=14.0)
    scored = do.score(signals, config, store).set_index("id")
    p = do.price_window(bars, ist("2026-10-23 10:02"), 60)
    raw = p["exit"] / p["entry"] - 1
    assert scored.loc[0, "net"] == pytest.approx(raw - 0.0014)
    assert scored.loc[1, "net"] == pytest.approx(-raw - 0.0014)
    bench = do.index_return(index, p["entry_time"], p["exit_time"])
    assert bench is not None and scored.loc[0, "alpha"] == pytest.approx(raw - bench - 0.0014)
    assert scored.loc[2, "status"] == "hold" and scored.loc[3, "status"] == "after_cutoff"
    summary = do.summarize(scored.reset_index())
    assert summary["by_strategy"]["llm"]["scored"] == 1 and summary["by_strategy"]["llm"]["hold"] == 1
    assert summary["by_strategy"]["ml"]["after_cutoff"] == 1 and "mean_net_bps" not in summary["by_strategy"]["ml"]
    assert summary["by_confidence"]["llm >=0.75"]["win_rate_pct"] == 100.0
    assert summary["by_action"]["rule SELL"]["mean_net_bps"] < 0


def test_missing_candles_are_unpriced_not_losses():
    scored = do.score(_signals([("NONE", "2026-10-23 10:02", "BUY", "llm", 0.8)]), store=FakeStore({}))
    assert scored["status"].tolist() == ["unpriced"] and scored["net"].isna().all()


def test_signals_load_from_the_ledger(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from trading_agent.storage.db import Base, Signal

    engine = create_engine(f"sqlite:///{(tmp_path / 's.db').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([Signal(symbol="AAA", timestamp=datetime(2026, 10, 23, 4, 32, tzinfo=UTC), interval="5minute",
                                action="BUY", strategy="llm", confidence=0.8),
                         Signal(symbol="AAA", timestamp=datetime(2026, 9, 1, 4, 32, tzinfo=UTC), interval="5minute",
                                action="SELL", strategy="rule", confidence=0.6)])
        session.commit()
        frame = do.load_signals(datetime(2026, 10, 1, tzinfo=UTC), session)
    assert frame["strategy"].tolist() == ["llm"] and str(frame["timestamp"].dt.tz) == "UTC"


def test_llm_tiers_fall_back_to_the_single_model(monkeypatch):
    from trading_agent.core.config import LLMSettings, get_settings
    from trading_agent.core import llm_client

    base = LLMSettings(model="m-base")
    assert base.model_for("quick") == base.model_for("deep") == "m-base"
    split = LLMSettings(model="m-base", quick_model="m-cheap", deep_model="m-strong")
    assert (split.model_for("quick"), split.model_for("deep")) == ("m-cheap", "m-strong")
    with pytest.raises(ValueError):
        split.model_for("medium")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_QUICK_MODEL", "m-cheap")
    monkeypatch.setenv("LLM_DEEP_MODEL", "m-strong")
    get_settings.cache_clear()
    try:
        assert llm_client.create_llm_client(tier="quick").model == "m-cheap"
        assert llm_client.create_llm_client(tier="deep").model == "m-strong"
    finally:
        get_settings.cache_clear()
