"""Ranker paper trial: frozen spec, causal picks, entry after the pick, missed sessions, fixed-sample verdict."""
import json

import numpy as np
import pandas as pd
import pytest

from trading_agent.research import ranker_trial as rt
from trading_agent.research.cross_sectional import RankConfig

IST = "Asia/Kolkata"
DAYS = pd.bdate_range("2026-09-01", periods=40)
BAR_TIMES = ("09:15", "09:16", "09:20", "10:00", "15:10", "15:14", "15:25")
SYMBOLS = [f"S{i}" for i in range(8)]
CONFIG = RankConfig(max_lag=2, train_days=20, top_k=2, n_estimators=10, max_depth=3, min_avg_value_cr=0,
                    random_draws=20)


def _candles(seed):
    rng = np.random.default_rng(seed)
    rows, price = [], 100.0
    for day in DAYS:
        for clock in BAR_TIMES:
            price *= 1 + rng.normal(0, 0.004)
            rows.append({"timestamp": pd.Timestamp(f"{day.date()} {clock}", tz=IST).tz_convert("UTC"),
                         "open": price, "high": price * 1.001, "low": price * 0.999,
                         "close": price * (1 + rng.normal(0, 0.001)), "volume": 1000.0})
    return pd.DataFrame(rows)


class FakeStore:
    def __init__(self, data, until=None):
        self.data, self.until = data, until

    def read_candles(self, symbol, interval, start=None, end=None):
        frame = self.data[symbol]
        mask = pd.Series(True, index=frame.index)
        if start is not None:
            mask &= frame.timestamp >= pd.Timestamp(start)
        if end is not None:
            mask &= frame.timestamp <= pd.Timestamp(end)
        if self.until is not None:
            mask &= frame.timestamp + pd.Timedelta(minutes=1) <= self.until  # only completed bars
        return frame[mask].reset_index(drop=True)


def at(day, clock):
    return pd.Timestamp(f"{DAYS[day].date()} {clock}", tz=IST).to_pydatetime()


@pytest.fixture
def store(monkeypatch, tmp_path):
    from trading_agent.core.config import get_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(rt, "git_revision", lambda: "test")
    yield FakeStore({s: _candles(i) for i, s in enumerate(SYMBOLS)})
    get_settings.cache_clear()


def _start(day=30):
    return rt.start(universe=SYMBOLS, config=CONFIG, now=at(day, "08:00"))


def test_spec_is_frozen_prospective_and_tamper_evident(store):
    with pytest.raises(rt.TrialError, match="not prospective"):
        rt.start(DAYS[29].date(), SYMBOLS, CONFIG, now=at(30, "08:00"))
    spec = _start()
    assert spec["start_session"] == str(DAYS[30].date()) and spec["config"]["top_k"] == 2
    with pytest.raises(FileExistsError):
        _start()
    edited = json.loads(rt.spec_path().read_text())
    edited["config"]["top_k"] = 5
    rt.spec_path().write_text(json.dumps(edited))
    with pytest.raises(rt.TrialError, match="hash"):
        rt.load_spec()


def test_pick_is_causal_once_and_inside_its_window(store):
    _start()
    with pytest.raises(rt.TrialError, match="starts on"):
        rt.pick(at(29, "09:20"), store, refresh=False)
    with pytest.raises(rt.TrialError, match="not complete"):
        rt.pick(at(30, "09:15"), store, refresh=False)
    with pytest.raises(rt.TrialError, match="deadline"):
        rt.pick(at(30, "10:30"), store, refresh=False)
    now = at(30, "09:20")
    live = rt.pick(now, FakeStore(store.data, until=pd.Timestamp(now)), refresh=False)
    assert live["train_end"] < live["session"] == str(DAYS[30].date())
    assert len(live["long"]) == len(live["short"]) == 2 and not {p["symbol"] for p in live["long"]} & {
        p["symbol"] for p in live["short"]}
    with pytest.raises(FileExistsError):
        rt.pick(now, store, refresh=False)
    # The whole day's bars in the store must not change the book.
    rt.record_path(DAYS[30].date(), "pick").unlink()
    assert rt.pick(now, store, refresh=False)["long"] == live["long"]


def test_settlement_enters_after_the_pick_and_exits_at_1515(store):
    _start()
    picked = rt.pick(at(30, "09:20"), store, refresh=False)
    session = DAYS[30].date()
    with pytest.raises(rt.TrialError, match="15:20"):
        rt.settle(session, at(30, "15:10"), store, refresh=False)
    record = rt.settle(session, at(30, "15:40"), store, refresh=False)

    def price(symbol, clock, field):
        frame = store.data[symbol]
        return frame.loc[frame.timestamp == pd.Timestamp(f"{session} {clock}", tz=IST), field].iloc[0]

    def ret(symbol, entry_clock):  # first bar after the 09:20 pick is 10:00; exit is the 15:14 close
        return price(symbol, "15:14", "close") / price(symbol, entry_clock, "open") - 1

    long = np.mean([ret(p["symbol"], "10:00") for p in picked["long"]])
    short = np.mean([ret(p["symbol"], "10:00") for p in picked["short"]])
    assert record["net"] == pytest.approx(0.5 * long - 0.5 * short - CONFIG.cost_bps / 1e4)
    long_open = np.mean([ret(p["symbol"], "09:15") for p in picked["long"]])
    short_open = np.mean([ret(p["symbol"], "09:15") for p in picked["short"]])
    assert record["net_at_open"] == pytest.approx(0.5 * long_open - 0.5 * short_open - CONFIG.cost_bps / 1e4)
    assert record["random_net"] is not None and record["unpriced"] == []
    with pytest.raises(FileExistsError):
        rt.settle(session, at(30, "15:45"), store, refresh=False)


def test_unpicked_trading_session_is_recorded_missed_and_due_picks_settle(store):
    _start()
    rt.pick(at(30, "09:20"), store, refresh=False)
    done = rt.settle_pending(at(31, "15:40"), store, refresh=False)
    assert [r["status"] for r in done] == ["settled", "missed"]
    report = rt.status()
    assert report["settled"] == 1 and report["missed"] == [str(DAYS[31].date())] and report["pending"] == []
    assert report["status"] == "collecting"


def test_verdict_uses_only_the_first_fixed_sample(store, monkeypatch):
    monkeypatch.setattr(rt, "DONE_SESSIONS", 3)
    spec = _start()
    nets = [0.010, 0.011, 0.012, -0.5, -0.5]  # a later collapse must not change a verdict already due
    for day, net in zip(range(30, 35), nets):
        rt._write_atomic(rt.record_path(DAYS[day].date(), "settle"), {
            "session": str(DAYS[day].date()), "status": "settled", "spec_sha256": spec["spec_sha256"],
            "net": net, "net_at_open": net, "random_net": -0.001, "long": [], "short": [], "unpriced": []})
    report = rt.status()
    assert report["status"] == "passed" and report["verdict"]["sessions"][1] == str(DAYS[32].date())
    assert report["model"]["days"] == 5 and report["model"]["mean_daily_net_bps"] < 0


def test_trial_endpoint_reports_state(store, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from trading_agent.api.main import create_app
    from trading_agent.core.config import get_settings

    monkeypatch.setenv("DB_URL", f"sqlite:///{(tmp_path / 'ledger.db').as_posix()}")
    monkeypatch.setenv("API_AUTH_TOKEN", "")
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/analytics/cross-sectional/trial").json() == {"status": "not_started"}
        _start()
        body = client.get("/api/v1/analytics/cross-sectional/trial").json()
        assert body["status"] == "collecting" and body["settled"] == 0 and body["universe_size"] == len(SYMBOLS)
