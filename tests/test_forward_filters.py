"""Entry-filter forward test: fixed bracket, first signals, once-only records, replay sample, fixed-sample verdict."""
from datetime import date, timedelta

import pandas as pd
import pytest

from trading_agent.research import forward_filters as ff

IST = "Asia/Kolkata"
DAY = date(2026, 10, 28)


def ist(clock, day=DAY):
    return pd.Timestamp(f"{day} {clock}", tz=IST).tz_convert("UTC")


def _bars(day=DAY, path=None):
    """1-minute bars 09:15-15:29 at 100, with optional {"HH:MM": (open, high, low, close)} overrides."""
    stamps = pd.date_range(f"{day} 09:15", f"{day} 15:29", freq="1min", tz=IST)
    rows = []
    for stamp in stamps:
        o, h, low, c = (path or {}).get(stamp.strftime("%H:%M"), (100.0, 100.05, 99.95, 100.0))
        rows.append({"timestamp": stamp.tz_convert("UTC"), "open": o, "high": h, "low": low, "close": c,
                     "volume": 100.0})
    return pd.DataFrame(rows)


class FakeStore:
    def __init__(self, frames):
        self.frames = frames

    def read_candles(self, symbol, interval, start=None, end=None):
        if (symbol, interval) not in self.frames:
            raise KeyError(symbol)
        return self.frames[(symbol, interval)]


def _signals(rows):
    return pd.DataFrame([{"id": i, "symbol": s, "timestamp": ist(t), "interval": "minute", "action": a,
                          "strategy": k, "confidence": 0.8} for i, (s, t, a, k) in enumerate(rows)])


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    from trading_agent.core.config import get_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(ff, "git_revision", lambda: "test")
    yield tmp_path
    get_settings.cache_clear()


def _day(path=None):
    return ff.session_frame(_bars(path=path), DAY, 1)


def test_bracket_enters_on_the_next_bar_and_settles_target_stop_or_1515():
    win = ff.bracket(_day({"10:30": (100.0, 101.5, 99.9, 101.4)}), ist("10:00"), "BUY", 1)
    assert win["exit_reason"] == "target" and win["entry_time"] == ist("10:00").isoformat()
    assert win["net_r"] == pytest.approx(2 - 0.0014 / 0.007)
    both = ff.bracket(_day({"10:30": (100.0, 101.5, 99.2, 100.0)}), ist("10:00"), "BUY", 1)
    assert both["exit_reason"] == "stop" and both["net_r"] == pytest.approx(-1 - 0.2)  # touching both: stop first
    flat = ff.bracket(_day(), ist("10:00"), "BUY", 1)
    assert flat["exit_reason"] == "session_exit" and flat["exit"] == 100.0  # 15:14 close, not 15:15 or later
    assert ff.bracket(_day(), ist("15:15"), "BUY", 1) is None  # no bar ends by 15:15 after the signal


def test_short_gap_through_the_stop_exits_at_the_open():
    gap = ff.bracket(_day({"11:00": (101.0, 101.1, 100.9, 101.0)}), ist("10:00"), "SELL", 1)
    assert gap["exit_reason"] == "gap_stop" and gap["exit"] == 101.0


def test_first_signal_of_the_session_and_first_inside_the_window():
    signals = _signals([("A", "09:20", "BUY", "rule"), ("A", "10:00", "SELL", "rule"),
                        ("A", "09:40", "HOLD", "llm"), ("A", "14:40", "BUY", "llm"), ("B", "11:00", "SELL", "rule")])
    picked = ff.first_trades(signals)
    assert [(t["symbol"], t["strategy"], t["action"]) for t in picked["all"]] == [
        ("A", "llm", "BUY"), ("A", "rule", "BUY"), ("B", "rule", "SELL")]
    assert [(t["symbol"], t["strategy"], t["action"]) for t in picked["window"]] == [
        ("A", "rule", "SELL"), ("B", "rule", "SELL")]  # the llm signal at 14:40 is outside 09:30-14:30


def test_record_is_due_after_1520_written_once_and_replays_only_the_sample(data_dir, monkeypatch):
    monkeypatch.setitem(ff.SPEC, "replay_cap", 1)
    store = FakeStore({("A", "minute"): _bars(), ("B", "minute"): _bars()})
    signals = _signals([("A", "10:00", "BUY", "rule"), ("B", "10:05", "SELL", "rule"), ("C", "10:10", "BUY", "rule")])
    calls = []

    def replay(trade, candles, minutes):
        calls.append(trade["symbol"])
        return "conclusive_negative"

    assert ff.record(DAY, store=store, signals=signals, now=ist("15:19"), replay=replay) is None
    assert ff.record(DAY - timedelta(days=1), store=store, signals=signals, now=ist("16:00"), replay=replay) is None
    doc = ff.record(DAY, store=store, signals=signals, now=ist("16:00"), replay=replay)
    assert doc["spec_sha256"] == ff.SPEC_SHA256 and len(calls) == 1
    assert sorted(str(t.get("replay")) for t in doc["window"]) == ["None", "conclusive_negative", "not_sampled"]
    assert next(t for t in doc["all"] if t["symbol"] == "C")["status"] == "unpriced"
    assert ff.record(DAY, store=store, signals=signals, now=ist("17:00"), replay=replay) is None  # never rewritten
    assert ff.record_path(DAY).exists()


def test_five_minute_bars_price_a_symbol_without_a_complete_minute_session(data_dir):
    five = _bars().iloc[::5].reset_index(drop=True)
    store = FakeStore({("A", "minute"): _bars(day=DAY - timedelta(days=1)), ("A", "5minute"): five})
    doc = ff.record(DAY, store=store, signals=_signals([("A", "10:00", "BUY", "rule")]), now=ist("16:00"),
                    replay=lambda *a: "inconclusive")
    assert doc["all"][0]["bar_minutes"] == 5


def _record(session, window_r, all_r, kept_r=None, refused_r=None):
    window = [{"strategy": "rule", "net_r": window_r, "net_bps": window_r * 70}]
    if kept_r is not None:
        window = [{"strategy": "rule", "net_r": kept_r, "net_bps": 0, "replay": "inconclusive"},
                  {"strategy": "rule", "net_r": refused_r, "net_bps": 0, "replay": "conclusive_negative"}]
    return {"session": str(session), "spec_sha256": ff.SPEC_SHA256,
            "all": [{"strategy": "rule", "net_r": all_r, "net_bps": all_r * 70}], "window": window}


def test_verdict_waits_for_and_uses_only_the_first_fixed_sample():
    days = [DAY + timedelta(days=i) for i in range(25)]
    early = [_record(d, -0.1 + 0.01 * (i % 3), -0.5, kept_r=-0.1 + 0.01 * (i % 2), refused_r=-0.4)
             for i, d in enumerate(days[:19])]
    assert ff.report(early)["verdict"]["time_window"] == "collecting"
    better = early + [_record(days[19], -0.1, -0.5, kept_r=-0.1, refused_r=-0.4)]
    result = ff.report(better + [_record(d, -2.0, 0.5, kept_r=-2.0, refused_r=0.5) for d in days[20:]])
    assert result["verdict"] == {"time_window": "adopt", "replay_gate": "adopt",
                                 "on_sessions": [str(d) for d in days[:20]]}
    assert result["all_sessions"]["time_window"]["paired"]["sessions"] == 25
    worse = ff.report([_record(d, -0.6 - 0.01 * (i % 2), -0.5) for i, d in enumerate(days[:20])])
    assert worse["verdict"]["time_window"] == "reject" and worse["verdict"]["replay_gate"] == "reject"
