"""Strategy search: alignment, causality, holdout isolation, frozen selection, gates and provenance."""
import json

import numpy as np
import pandas as pd
import pytest

from trading_agent.research import strategy_search as ss
from trading_agent.research.trade_outcomes import TradePolicy, outcome_arrays

IST = "Asia/Kolkata"


def _minutes(day="2026-10-19", n=375, seed=0, drift=0.0):
    rng = np.random.default_rng(seed)
    ts = pd.date_range(f"{day} 09:15", periods=n, freq="min", tz=IST).tz_convert("UTC")
    c = 100 + (rng.normal(0, 0.1, n) + drift).cumsum()
    return pd.DataFrame({"timestamp": ts, "open": c, "high": c + 0.05, "low": c - 0.05, "close": c, "volume": 10.0})


def _history(sessions=30, seed=0):
    days = pd.bdate_range("2026-06-01", periods=sessions)
    return pd.concat([_minutes(str(d.date()), seed=seed * 1000 + i) for i, d in enumerate(days)], ignore_index=True)


class FakeStore:
    def __init__(self, data):
        self.data = data

    def read_candles(self, symbol, interval):
        if symbol not in self.data:
            raise KeyError(symbol)
        return self.data[symbol].copy()


@pytest.fixture
def small(monkeypatch, tmp_path):
    monkeypatch.setattr(ss, "BAR_MINUTES", (15,))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from trading_agent.core.config import get_settings

    get_settings.cache_clear()
    store = FakeStore({"AAA": _history(seed=1), "BBB": _history(seed=2)})
    yield store
    get_settings.cache_clear()


# --- data and replay --------------------------------------------------------------------

def test_resample_aligns_bars_and_drops_out_of_session_rows():
    minute = pd.concat([_minutes(), _minutes(n=20).assign(timestamp=lambda d: d.timestamp + pd.Timedelta(hours=7))])
    bars = ss.resample(minute, 15)
    local = bars.timestamp.dt.tz_convert(IST)
    assert local.iloc[0].strftime("%H:%M") == "09:15" and local.iloc[1].strftime("%H:%M") == "09:30"
    assert len(bars) == 25 and local.dt.hour.max() == 15   # 16:15+ rows dropped
    first = _minutes().iloc[:15]
    assert bars.high.iloc[0] == first.high.max() and bars.close.iloc[0] == first.close.iloc[-1]


def test_non_overlapping_skips_signals_inside_an_open_trade():
    actions = np.array([1, 1, 2, 0, 1, 0], dtype=np.int8)
    net = np.array([0.01, 0.02, np.nan, np.nan, 0.03, np.nan])
    exits = np.array([2, 3, -1, -1, 5, -1])
    assert ss.non_overlapping(actions, net, exits, net, exits, np.ones(6, dtype=bool)) == [(0, 0.01), (4, 0.03)]


def test_every_family_is_prefix_causal():
    days = pd.bdate_range("2026-09-01", periods=4)
    minute = pd.concat([_minutes(str(d.date()), seed=i) for i, d in enumerate(days)], ignore_index=True)
    f = ss.prepare(ss.resample(minute, 5))
    cut = len(f) - 40
    prefix = ss.prepare(ss.resample(minute.iloc[: (cut * 5)], 5))
    assert len(prefix) == cut
    for family, params in ss.entry_families(5):
        full = ss.family_actions(f, family, params)
        assert full.shape == (len(f),) and set(np.unique(full)) <= {0, 1, 2}
        if family == "random_entry":
            continue  # seeded noise, not a function of prices
        np.testing.assert_array_equal(ss.family_actions(prefix, family, params), full[:cut], err_msg=family)


def test_trades_never_cross_a_session_boundary():
    days = pd.bdate_range("2026-09-01", periods=3)
    f = ss.prepare(ss.resample(pd.concat([_minutes(str(d.date()), seed=i) for i, d in enumerate(days)]), 15))
    policy = TradePolicy(horizon=16, stop_atr=2.0, reward_risk=3.0)
    for side in ("BUY", "SELL"):
        net, exits = outcome_arrays(f, side, policy, 900)
        done = np.flatnonzero(np.isfinite(net))
        assert len(done) and (f.session.to_numpy()[exits[done]] == f.session.to_numpy()[done]).all()


def test_costs_shift_every_trade_mean_by_the_difference():
    f = ss.prepare(ss.resample(_history(sessions=6), 15))
    means = []
    for cost in (6.0, 10.0):
        policy = TradePolicy(horizon=8, stop_atr=1.5, reward_risk=2.0, cost_bps=cost)
        nb, eb = outcome_arrays(f, "BUY", policy, 900)
        ns, es = outcome_arrays(f, "SELL", policy, 900)
        trades = ss.non_overlapping(ss.family_actions(f, "always_long", {}), nb, eb, ns, es, np.ones(len(f), bool))
        means.append(np.mean([r for _, r in trades]))
    assert means[0] - means[1] == pytest.approx(4e-4)


def test_size_aware_costs_come_from_the_charge_model():
    small, large = ss.size_aware_costs(50_000), ss.size_aware_costs(500_000)
    assert small.cost_bps > large.cost_bps > 0 and small.round_trip_bps == small.cost_bps + 4.0


# --- isolation and selection -------------------------------------------------------------

def test_selection_scoring_never_replays_holdout(small):
    universe = ss.load_universe(["AAA", "BBB", "MISSING"], small)
    assert "MISSING" in universe.skipped and set(universe.fingerprints) == {"AAA", "BBB"}
    ledgers = ss.score_rules(universe, ss.CONSERVATIVE)
    holdout = universe.period_sessions("holdout")
    assert ledgers and all(ledger.by_session[holdout].sum() == 0 for ledger in ledgers.values())
    assert any(ledger.by_session[:, 1].sum() > 0 for ledger in ledgers.values())


def test_ml_holdout_is_deferred(small):
    universe = ss.load_universe(["AAA", "BBB"], small)
    ledgers, deferred = ss.score_ml(universe, ss.CONSERVATIVE)
    holdout = universe.period_sessions("holdout")
    for key, ledger in ledgers.items():
        assert ledger.by_session[holdout].sum() == 0
        assert deferred[key]().by_session[np.setdiff1d(np.arange(len(universe.sessions)), holdout)].sum() == 0


def _ledger(universe, daily_returns, trades_per_day=10, symbols=2):
    ledger = ss.Ledger(len(universe.sessions), symbols)
    for i, r in enumerate(daily_returns):
        if r is None:
            continue
        returns = np.full(trades_per_day, r)
        ledger.add(i % symbols, np.full(trades_per_day, i), returns)
    return ledger


def test_gates_require_trades_baselines_and_significance(small):
    universe = ss.load_universe(["AAA", "BBB"], small)
    n = len(universe.sessions)
    rng = np.random.default_rng(0)
    policy = {"horizon_bars": 8, "stop_atr": 1.5, "reward_risk": 2.0}
    strong = ss.candidate_key("x_strong", {}, 15, policy)
    weak = ss.candidate_key("x_weak", {}, 15, policy)
    sparse = ss.candidate_key("x_sparse", {}, 15, policy)
    ledgers = {
        strong: _ledger(universe, list(0.01 + rng.normal(0, 0.001, n))),
        weak: _ledger(universe, list(0.0002 + rng.normal(0, 0.01, n))),
        sparse: _ledger(universe, list(0.01 + rng.normal(0, 0.001, n)), trades_per_day=1),
    }
    for name in ss.BASELINE_FAMILIES:
        ledgers[ss.candidate_key(name, {}, 15, policy)] = _ledger(universe, [-0.001] * n)
    rules = ss.SelectionRules(min_dev_trades=50, min_val_trades=30, bootstrap_draws=200)
    picked = ss.select(ledgers, universe, rules)
    assert picked["shortlist"] == [strong]
    assert not picked["rows"][weak]["checks"]["clustered_t_above_bonferroni"]
    assert not picked["rows"][sparse]["checks"]["validation_trades"]
    assert picked["candidates_tried"] == 3

    # A baseline that does better blocks the candidate even when it is significant.
    ledgers[ss.candidate_key("always_long", {}, 15, policy)] = _ledger(universe, [0.05] * n)
    assert ss.select(ledgers, universe, rules)["shortlist"] == []


def test_walk_forward_chooses_on_the_training_window_only(small):
    universe = ss.load_universe(["AAA", "BBB"], small)
    pool = np.concatenate([universe.period_sessions(p) for p in ss.SELECTION_PERIODS])
    n = len(universe.sessions)
    # A wins on the first window and loses afterwards; B is the reverse.
    a = [0.01 if i in pool[:10] else -0.01 for i in range(n)]
    b = [-0.01 if i in pool[:10] else 0.01 for i in range(n)]
    policy = {"horizon_bars": 8, "stop_atr": 1.5, "reward_risk": 2.0}
    ka, kb = ss.candidate_key("a", {}, 15, policy), ss.candidate_key("b", {}, 15, policy)
    result = ss.walk_forward({ka: _ledger(universe, a), kb: _ledger(universe, b)}, universe, [ka, kb],
                             ss.WalkForward(train_sessions=10, test_sessions=5, min_train_trades=10))
    assert result["folds"][0]["chosen"]["family"] == "a"       # best on its own training window
    assert result["folds"][0]["test"]["mean_net_bps"] == pytest.approx(-100.0)
    assert all(f["chosen"]["family"] == "b" for f in result["folds"][2:])


# --- end to end ------------------------------------------------------------------------

def test_run_freezes_selection_before_holdout_and_records_provenance(small, monkeypatch):
    original = ss.score_rules
    seen = {}

    def spy(universe, costs, periods=ss.SELECTION_PERIODS, only=None):
        if "holdout" in periods:
            seen["frozen_before_holdout"] = any(ss.runs_dir().glob("*/selection.json"))
        return original(universe, costs, periods, only)

    monkeypatch.setattr(ss, "score_rules", spy)
    monkeypatch.setattr(ss, "select", lambda ledgers, universe, rules: {
        **original_select(ledgers, universe, rules), "shortlist": [next(iter(ledgers))]})
    report = ss.run(["AAA", "BBB"], include_ml=False, store=small,
                    rules=ss.SelectionRules(bootstrap_draws=100),
                    wf=ss.WalkForward(train_sessions=8, test_sessions=4, min_train_trades=5))
    assert seen["frozen_before_holdout"] is True
    for field in ("run_id", "git_revision", "versions", "data_fingerprints", "skipped", "selection_sha256"):
        assert field in report
    run_dir = ss.runs_dir() / report["run_id"]
    frozen = json.loads((run_dir / "selection.json").read_text())
    assert frozen["phase"] == "selection_frozen" and "holdout" not in json.dumps(frozen["shortlist"])
    assert {h["family"] for h in report["holdout"]} >= set(ss.BASELINE_FAMILIES)
    assert json.loads((ss.runs_dir().parent / "latest.json").read_text())["run_id"] == report["run_id"]
    with pytest.raises(FileExistsError):
        ss._write_atomic(run_dir / "report.json", {}, exclusive=True)
    assert report["walk_forward"]["rule_exit_policies"]["candidates_per_fold"] > 0


original_select = ss.select
