"""Ranking lab: causal snapshots, fixed-sign rules, walk-forward models, frozen selection."""
import json

import numpy as np
import pandas as pd
import pytest

from trading_agent.research import ranking_lab as lab

IST = "Asia/Kolkata"
SMALL = lab.LabConfig(train_days=20, retrain_days=10, min_days=10, wf_train=20, wf_test=10)


def _minutes(day="2026-09-01"):
    ts = pd.date_range(f"{day} 09:15", f"{day} 15:29", freq="min", tz=IST)
    price = 100 + np.arange(len(ts)) * 0.01
    return pd.DataFrame({"timestamp": ts.tz_convert("UTC"), "open": price, "high": price + 0.02,
                         "low": price - 0.02, "close": price + 0.005, "volume": 10.0})


def test_snapshots_take_each_price_at_its_time_and_opening_values_are_causal():
    frame = _minutes()
    snap = lab.snapshots(frame).iloc[0]
    assert snap["open"] == frame["open"].iloc[0]
    assert snap["p0945"] == frame["close"].iloc[29]          # 09:44 bar
    assert snap["e0945"] == frame["open"].iloc[30]           # 09:45 bar
    assert snap["x1515"] == frame["close"].iloc[359]         # 15:14 bar
    assert snap["vol_open"] == 300.0
    early = lab.snapshots(frame.iloc[:30]).iloc[0]           # only bars before 09:45 exist
    for column in ("open", "p0945", "vol_open"):
        assert early[column] == snap[column]


def _synthetic(sessions=150, symbols=40, seed=0, reversal=0.5):
    """Wide snapshots with a planted overnight-gap reversal: open-to-15:15 return = -reversal * gap."""
    rng = np.random.default_rng(seed)
    days = [d.date() for d in pd.bdate_range("2026-01-01", periods=sessions)]
    names = [f"S{i:02d}" for i in range(symbols)]
    close = np.full(symbols, 100.0)
    rows = {c: [] for c in ("open", "p0945", "e0945", "x1015", "x1200", "x1515", "vol_open", "value")}
    for _ in days:
        gap = rng.normal(0, 0.01, symbols)
        o = close * (1 + gap)
        ret = -reversal * gap + rng.normal(0, 0.004, symbols)
        x = o * (1 + ret)
        p = o * (1 + rng.normal(0, 0.002, symbols))
        for c, v in (("open", o), ("p0945", p), ("e0945", p), ("x1015", o * (1 + ret / 3)),
                     ("x1200", o * (1 + ret / 2)), ("x1515", x), ("vol_open", rng.uniform(5e4, 2e5, symbols)),
                     ("value", np.full(symbols, 1e9))):
            rows[c].append(v)
        close = x
    return {c: pd.DataFrame(np.array(v), index=days, columns=names) for c, v in rows.items()}


def test_fixed_sign_rules_find_a_planted_reversal_and_its_opposite_loses():
    table = lab.build_table(_synthetic(), SMALL)
    keys = {"gap_reversal|0915-1515|k5", "gap_momentum|0915-1515|k5"}
    series = lab.daily_series(table, SMALL, only=keys)
    good, bad = (lab.stats(series[k]) for k in sorted(keys, reverse=True))
    assert good["mean_gross_bps"] > 30 and good["day_win_rate_pct"] > 90
    assert bad["mean_gross_bps"] == pytest.approx(-good["mean_gross_bps"], rel=1e-9)
    assert bad["mean_net_bps"] < -SMALL.cost_bps


def test_model_folds_train_only_on_earlier_sessions():
    history = list(range(100))
    tested = history[30:]
    folds = lab.model_folds(history, tested, SMALL)
    assert folds and all(len(train) == SMALL.train_days and max(train) < min(block) for train, block in folds)
    assert sorted(d for _, block in folds for d in block) == tested


def test_liquidity_is_known_before_the_open():
    snap = _synthetic(sessions=60, symbols=4)
    snap["value"].iloc[25:, 0] = 1.0     # S00 (INR 100 crore a day) stops trading from session 25
    table = lab.build_table(snap, SMALL)
    s00 = table.xs("S00", level="symbol").index
    day = snap["open"].index
    assert day[25] in s00                 # the open of session 25 sees only earlier value
    assert day[43] in s00                 # two active days in the window: mean exactly INR 10 crore
    assert day[44] not in s00             # one active day: below the floor


def test_run_freezes_selection_before_reporting_holdout(tmp_path, monkeypatch):
    from trading_agent.core.config import get_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(lab, "git_revision", lambda: "test")
    monkeypatch.setattr(lab, "MODELS", (("logit_lags", "logit", "lags", "0915"),))
    report = lab.run(["X"], SMALL, snap=_synthetic())
    run_dir = lab.runs_dir() / report["run_id"]
    frozen = json.loads((run_dir / "selection.json").read_text())
    assert frozen["phase"] == "selection_frozen" and "holdout" not in json.dumps(frozen["validation_table"])
    assert "gap_reversal|0915-1515|k5" in frozen["shortlist"]
    assert set(report["holdout"]) == set(frozen["holdout_will_report"]) - {lab.REFERENCE}  # reference not run here
    assert report["holdout"]["gap_reversal|0915-1515|k5"]["mean_net_bps"] > 0
    assert report["variants_tried"] == len(lab.variants())
    with pytest.raises(FileExistsError):
        lab._write_atomic(run_dir / "report.json", {}, exclusive=True)
    get_settings.cache_clear()
