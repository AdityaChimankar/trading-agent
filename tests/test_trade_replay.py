"""Trade replay: bars, exits, block bootstrap, volatility scaling, conclusion and the API."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from trading_agent.research import trade_replay as tr

IST = ZoneInfo("Asia/Kolkata")


def minute_frame(days, step=0.0, seed=7, start=date(2026, 9, 1), noise=0.0005, last_day_noise=None):
    """1-minute candles 09:15-15:29 IST on `days` weekdays; `step` is the per-minute drift."""
    rng = np.random.default_rng(seed)
    rows, price, day, count = [], 100.0, start, 0
    while count < days:
        if day.weekday() < 5:
            count += 1
            sigma = last_day_noise if (last_day_noise is not None and count == days) else noise
            for m in range(375):
                stamp = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST) + timedelta(minutes=m)
                close = price * (1 + step + rng.normal(0, sigma))
                rows.append((stamp, price, max(price, close) * 1.0002, min(price, close) * 0.9998, close))
                price = close
        day += timedelta(days=1)
    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"]).dt.tz_convert("UTC")
    return frame


def plan(side="BUY", entry=100.0, stop=99.0, target=102.0, at="2026-10-30 10:20"):
    return tr.Plan("TEST", side, entry, stop, target, pd.Timestamp(at, tz=IST).to_pydatetime())


def reference_settle(o, h, low, last_close, p):
    """Loop version of research/trade_outcomes.py's exit order."""
    outcomes, exits = [], []
    for i in range(o.shape[0]):
        outcome, exit_price = 0, last_close[i]
        for j in range(o.shape[1]):
            if p.sign * (o[i, j] - p.stop) <= 0:
                outcome, exit_price = -1, o[i, j]
            elif p.sign * (o[i, j] - p.target) >= 0:
                outcome, exit_price = 1, p.target
            elif (low[i, j] <= p.stop) if p.sign > 0 else (h[i, j] >= p.stop):
                outcome, exit_price = -1, p.stop
            elif (h[i, j] >= p.target) if p.sign > 0 else (low[i, j] <= p.target):
                outcome, exit_price = 1, p.target
            else:
                continue
            break
        outcomes.append(outcome)
        exits.append(exit_price)
    return np.array(outcomes), np.array(exits)


def trending_sessions(count=60, drift=0.001, bars=72, seed=5):
    """Each session moves the same way every bar; half up, half down."""
    rng = np.random.default_rng(seed)
    out = []
    for sign in rng.permutation([1, -1] * (count // 2)):
        close = 100 * (1 + sign * drift) ** np.arange(1, bars + 1)
        opens = np.concatenate([[100.0], close[:-1]])
        out.append(np.stack([opens, np.maximum(opens, close), np.minimum(opens, close), close], axis=1))
    return np.stack(out)


# --- exits ------------------------------------------------------------------------------

@pytest.mark.parametrize("side, stop, target", [("BUY", 98.5, 102.0), ("SELL", 101.5, 98.0)])
def test_vectorized_exits_match_the_loop_reference(side, stop, target):
    rng = np.random.default_rng(3)
    o = 100 + rng.normal(0, 1.2, (400, 4))
    h, low = o + rng.uniform(0, 1.5, o.shape), o - rng.uniform(0, 1.5, o.shape)
    last = 100 + rng.normal(0, 1, 400)
    p = plan(side, 100.0, stop, target)
    outcome, exit_price = tr.settle(o, h, low, last, p)
    expected_outcome, expected_exit = reference_settle(o, h, low, last, p)
    assert (outcome == expected_outcome).all() and np.allclose(exit_price, expected_exit)
    assert {-1, 0, 1} <= set(outcome)


def test_gap_through_the_stop_exits_at_the_open_and_both_touched_is_a_stop():
    o = np.array([[100.0, 98.0], [100.0, 100.0]])
    h = np.array([[100.5, 98.5], [103.0, 100.0]])
    low = np.array([[99.5, 97.5], [98.0, 100.0]])
    outcome, exit_price = tr.settle(o, h, low, np.array([98.0, 100.0]), plan())
    assert outcome.tolist() == [-1, -1] and exit_price.tolist() == [98.0, 99.0]


# --- bars -------------------------------------------------------------------------------

def _drop(frame, day_index, start, minutes):
    local = pd.to_datetime(frame["timestamp"]).dt.tz_convert(IST)
    day = local.dt.date.iloc[day_index * 375]
    minute = local.dt.hour * 60 + local.dt.minute
    return frame[~((local.dt.date == day) & (minute >= start) & (minute < start + minutes))]


def test_bars_are_fixed_ohlc_windows_from_0915():
    frame = minute_frame(2)
    bars = tr.session_bars(frame, 5)
    first = next(iter(bars.values()))
    assert first.shape == (72, 4)
    assert first[0].tolist() == pytest.approx([frame.open.iloc[0], frame.high.iloc[:5].max(),
                                               frame.low.iloc[:5].min(), frame.close.iloc[4]])
    assert next(iter(tr.session_bars(frame, 1).values())).shape == (360, 4)


def test_a_few_missing_bars_are_filled_flat_and_gappy_sessions_are_dropped():
    frame = _drop(minute_frame(2), 0, 11 * 60, 5)  # one 5-minute bar, five 1-minute bars
    for minutes, slot in ((5, 21), (1, 105)):
        bars = list(tr.session_bars(frame, minutes).values())
        assert len(bars) == 2
        assert bars[0][slot].tolist() == [bars[0][slot - 1, 3]] * 4
    gappy = _drop(minute_frame(2), 0, 11 * 60, 15)  # three 5-minute bars, 15 minutes
    assert len(tr.session_bars(gappy, 5)) == 1 and len(tr.session_bars(gappy, 1)) == 1
    assert len(tr.session_bars(_drop(minute_frame(2), 0, 9 * 60 + 15, 5), 5)) == 1  # no 09:15 bar


@pytest.mark.parametrize("clock, minutes, slot", [("09:15", 5, 1), ("09:21", 5, 2), ("10:20", 5, 13),
                                                  ("10:17", 5, 13), ("15:10", 5, 71), ("10:17", 1, 62),
                                                  ("15:14", 1, 359)])
def test_first_slot_is_the_next_bar_start(clock, minutes, slot):
    assert tr.first_slot(pd.Timestamp(f"2026-10-30 {clock}", tz=IST), minutes) == slot


@pytest.mark.parametrize("stamp", ["2026-10-30 15:11", "2026-10-30 09:00", "2026-10-31 10:00"])
def test_first_slot_refuses_times_with_no_bar_left(stamp):
    with pytest.raises(ValueError):
        tr.first_slot(pd.Timestamp(stamp, tz=IST), 5)


# --- block bootstrap and volatility -------------------------------------------------------

def test_one_endless_block_replays_a_real_session_exactly():
    sessions = trending_sessions(count=2)
    returns = tr.log_returns(sessions)
    p = plan("BUY", 100, 99, 101.5)
    scale = 100 / sessions[0, 12, 3]
    real = tr.settle(*(sessions[:1, 13:, i] * scale for i in range(3)), sessions[:1, -1, 3] * scale, p)
    outcome, exit_price, _, _ = tr.run_paths(returns[:1], np.ones(72), p, 13, 1e9, 0, 50,
                                             np.random.default_rng(1))
    assert (outcome == real[0][0]).all() and np.allclose(exit_price, real[1][0])


def test_long_blocks_copy_whole_sessions_and_single_bars_mix_them():
    sessions = trending_sessions()
    returns = tr.log_returns(sessions)
    p = plan("BUY", 100, 90, 120)  # never reached: every replay ends at its last close
    ends = 100 * sessions[:, -1, 3] / sessions[:, 12, 3]
    _, _, blocks, _ = tr.run_paths(returns, np.ones(72), p, 13, 1e9, 0, 300, np.random.default_rng(2))
    assert np.isclose(blocks[:, None], ends[None, :]).any(axis=1).all()
    _, _, single, _ = tr.run_paths(returns, np.ones(72), p, 13, 1.0, 0, 300, np.random.default_rng(2))
    assert np.isclose(single[:, None], ends[None, :]).any(axis=1).mean() < 0.1


def test_blocks_keep_trends_that_single_bars_break_up():
    sessions = trending_sessions()
    returns = tr.log_returns(sessions)
    p = plan("BUY", 100, 99, 101.5)
    single = tr.run_paths(returns, np.ones(72), p, 13, 1.0, 2, 3000, np.random.default_rng(3))[0]
    blocks = tr.run_paths(returns, np.ones(72), p, 13, 6.0, 2, 3000, np.random.default_rng(3))[0]
    assert (blocks == 0).mean() < (single == 0).mean() - 0.1


def test_optimal_block_is_short_for_noise_and_long_for_persistent_series():
    rng = np.random.default_rng(4)
    noise = rng.normal(size=(60, 300))
    persistent = np.zeros_like(noise)
    for t in range(1, 300):
        persistent[:, t] = 0.8 * persistent[:, t - 1] + noise[:, t]
    assert tr.optimal_block(noise, 60) < 2
    assert tr.optimal_block(persistent, 60) > 5


def test_today_volatility_is_measured_before_entry_and_shrunk_to_the_recent_median():
    frame = minute_frame(45, last_day_noise=0.0015)  # the entry day is three times as volatile
    entry_day = pd.Timestamp(frame.timestamp.iloc[-1]).tz_convert(IST).date()
    sessions = np.stack(list(tr.session_bars(frame, 1).values())[:-1])
    returns = tr.log_returns(sessions)
    profile = tr.vol_profile(returns, 1)
    recent = float(np.median(tr.day_vols(returns, profile)))
    early = tr.today_vol(frame, entry_day, 30, 1, profile, recent)
    late = tr.today_vol(frame, entry_day, 300, 1, profile, recent)
    assert early["today_bars"] == 29 and late["today_bars"] == 299
    assert late["today"] == pytest.approx(3 * recent, rel=0.15)
    assert recent < early["used"] < late["used"] < late["today"]
    none = tr.today_vol(frame, entry_day + timedelta(days=3), 30, 1, profile, recent)
    assert none["today_bars"] == 0 and none["used"] == pytest.approx(recent)


# --- replay, conclusion and API --------------------------------------------------------------

def test_replay_uses_minute_bars_when_complete_and_is_repeatable():
    frame = minute_frame(45)
    p = plan(at="2026-11-03 10:20")
    config = tr.ReplayConfig(replays=1000, sessions=40)
    first = tr.replay(p, config, frame)
    assert first == tr.replay(p, config, frame)
    assert first["bar_minutes"] == 1 and first["first_bar"] == "10:20" and first["bars_left"] == 295
    assert first["pool"]["last"] < "2026-11-03" and first["pool"]["sessions"] == 40
    sim = first["simulation"]
    assert sim["p_target"] + sim["p_stop"] + sim["p_neither"] == pytest.approx(1)
    assert sim["bands"]["time"][0] == "10:25" and sim["bands"]["time"][-1] == "15:15"
    low, high = sim["avg_r_net_range"]
    assert low <= high and set(first["variants"]) == {"single_bars", "unscaled"}
    assert 1 <= first["mean_block_minutes"] <= 60
    five = tr.replay(p, tr.ReplayConfig(replays=1000, sessions=40, bar_minutes=5), frame)
    assert five["bar_minutes"] == 5 and five["bars_left"] == 59


def test_a_persistent_fall_is_conclusively_negative_for_a_long():
    frame = minute_frame(45, step=-0.00004)
    doc = tr.replay(plan("BUY", 100, 99, 102, at="2026-11-03 10:20"), tr.ReplayConfig(replays=1000), frame)
    assert doc["conclusion"]["status"] == "conclusive_negative"
    assert all(check["passed"] for check in doc["conclusion"]["checks"])


def test_a_driftless_market_loses_to_costs_and_is_inconclusive_without_them():
    frame = minute_frame(45, noise=0.0008)
    p = plan("BUY", 100, 99, 101, at="2026-11-03 10:20")  # 1R: needs 57% target-first at 14 bps
    assert tr.replay(p, tr.ReplayConfig(replays=1000), frame)["conclusion"]["status"] == "conclusive_negative"
    doc = tr.replay(p, tr.ReplayConfig(replays=1000, cost_bps=0), frame)
    conclusion = doc["conclusion"]
    assert conclusion["status"] == "inconclusive" and conclusion["line"].startswith("Inconclusive: ")
    assert not next(c for c in conclusion["checks"] if c["name"] == "range")["passed"]


def test_conclusion_needs_every_check():
    p = plan("BUY", 100, 99, 102)  # break-even share 38% at 14 bps
    sim = {"avg_r_net": 0.3, "avg_r_net_range": [0.1, 0.5], "share_range": [0.45, 0.6]}
    good = {"avg_r_net": 0.2}
    variants = {"single_bars": good, "unscaled": good}
    past = {"avg_r_net": 0.1, "count": 60}
    positive = tr.conclude(sim, variants, past, p, 14, 60)
    assert positive["status"] == "conclusive_positive" and "not evidence" in positive["line"]
    assert tr.conclude(sim, variants, past, p, 14, 39)["status"] == "inconclusive"
    assert tr.conclude(sim, variants, {**past, "avg_r_net": -0.1}, p, 14, 60)["status"] == "inconclusive"
    assert tr.conclude({**sim, "share_range": [0.3, 0.6]}, variants, past, p, 14, 60)["status"] == "inconclusive"
    negative = {"avg_r_net": -0.3, "avg_r_net_range": [-0.5, -0.1], "share_range": [0.1, 0.3]}
    bad = {"avg_r_net": -0.2}
    assert tr.conclude(negative, {"single_bars": bad, "unscaled": bad}, {**past, "avg_r_net": -0.2},
                       p, 14, 60)["status"] == "conclusive_negative"


def test_break_even_share_weighs_the_target_and_costs():
    p = plan("BUY", 100, 99, 102)  # 2R; 14 bps of 100 = 0.14 R of cost
    sim = {"p_target": 0.32, "p_stop": 0.48, "p_neither": 0.2, "avg_r_net": -0.1}
    line, _ = tr.verdict(sim, p, 14)
    assert "has to come first in 38%" in line and "it did in 40%" in line and "Close to break-even" in line
    assert "has to come first in 33%" in tr.verdict(sim, p, 0)[0]


def test_too_few_sessions_and_bad_plans_are_refused():
    with pytest.raises(ValueError, match="usable sessions"):
        tr.replay(plan(), tr.ReplayConfig(replays=500), minute_frame(10))
    with pytest.raises(ValueError, match="loss side"):
        plan("BUY", 100, 101, 102)
    with pytest.raises(ValueError, match="profit side"):
        plan("SELL", 100, 101, 102)
    with pytest.raises(ValueError):
        tr.ReplayConfig(bar_minutes=15)


def test_api_returns_the_replay_and_rejects_bad_requests(monkeypatch):
    from fastapi.testclient import TestClient

    from trading_agent.api.main import create_app

    frame = minute_frame(30)
    monkeypatch.setattr(tr, "load_candles", lambda symbol: (frame, "minute"))
    with TestClient(create_app()) as client:
        body = {"symbol": "test", "side": "BUY", "entry": 100, "stop": 99, "target": 102,
                "entry_time": "2026-10-30T10:20:00", "replays": 500, "sessions": 25}
        doc = client.post("/api/v1/analytics/trade-replay", json=body).json()
        assert doc["symbol"] == "TEST" and doc["first_bar"] == "10:20" and doc["config"]["replays"] == 500
        assert doc["conclusion"]["status"] in {"inconclusive", "conclusive_negative", "conclusive_positive"}
        # No entry time: the end of the latest stored bar (15:30 here) leaves no bar before 15:15.
        late = client.post("/api/v1/analytics/trade-replay", json=body | {"entry_time": None})
        assert late.status_code == 400 and "15:15" in late.json()["detail"]
        wrong = client.post("/api/v1/analytics/trade-replay", json=body | {"stop": 101})
        assert wrong.status_code == 400 and "loss side" in wrong.json()["detail"]
