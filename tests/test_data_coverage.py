"""Candle coverage: up to date, behind, holes, empty; holidays and the in-progress session."""
from datetime import date

import pandas as pd

from trading_agent.ingest.coverage import check, last_closed_session

IST = "Asia/Kolkata"


def _day(day, until="15:29"):
    return pd.date_range(f"{day} 09:15", f"{day} {until}", freq="min", tz=IST).tz_convert("UTC")


class FakeStore:
    def __init__(self, days):
        self.frames = {s: pd.DataFrame({"timestamp": pd.DatetimeIndex([]).append([_day(*d) for d in v])
                                        if v else pd.DatetimeIndex([], tz="UTC")}) for s, v in days.items()}

    def read_candles(self, symbol, interval, columns=None):
        return self.frames[symbol]


def at(stamp):
    return pd.Timestamp(stamp, tz=IST).to_pydatetime()


SESSIONS = [(f"2026-10-{d:02d}",) for d in (12, 13, 14, 15, 16, 19, 21, 22, 23)]  # Oct 20 a holiday


def test_coverage_classifies_symbols_against_the_store_calendar():
    store = FakeStore({
        "OK": SESSIONS,
        "AUCTION": [(d, "15:14") for (d,) in SESSIONS],          # closing-auction stocks stop at 15:14
        "BEHIND": SESSIONS[:-2],
        "HOLES": SESSIONS[:3] + SESSIONS[5:],                     # Oct 15-16 missing
        "PARTIAL": SESSIONS[:-1] + [("2026-10-23", "10:30")],    # ticker died mid-session
        "EMPTY": [],
    })
    report = check(list(store.frames), store=store, days=30, now=at("2026-10-24 12:00"))
    assert report["last_closed_weekday"] == "2026-10-23" and report["store_current"]
    assert report["sessions_in_window"] == len(SESSIONS)        # the holiday is not required
    problems = report["problems"]
    assert set(problems) == {"BEHIND", "HOLES", "PARTIAL", "EMPTY"}
    assert problems["BEHIND"] == {"status": "behind", "last_complete": "2026-10-21", "missing": []}
    assert problems["HOLES"]["missing"] == ["2026-10-15", "2026-10-16"]
    assert problems["PARTIAL"]["status"] == "behind"
    assert report["counts"] == {"up_to_date": 2, "behind": 2, "holes": 1, "empty": 1} and not report["ok"]


def test_store_is_stale_when_its_newest_session_predates_the_last_close():
    store = FakeStore({"OK": SESSIONS[:-1]})
    stale = check(["OK"], store=store, now=at("2026-10-23 16:00"))
    assert not stale["store_current"] and not stale["ok"]
    # During the session, today is not yet required.
    assert check(["OK"], store=store, now=at("2026-10-23 11:00"))["ok"]


def test_last_closed_session_skips_weekends_and_the_open_session():
    assert last_closed_session(at("2026-10-24 12:00")) == date(2026, 10, 23)   # Saturday -> Friday
    assert last_closed_session(at("2026-10-26 10:00")) == date(2026, 10, 23)   # Monday morning -> Friday
    assert last_closed_session(at("2026-10-26 15:31")) == date(2026, 10, 26)


def test_data_check_reports_unlisted_symbols_apart_and_fails_on_real_gaps(monkeypatch):
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from trading_agent import cli
    from trading_agent.ingest import coverage
    from trading_agent.ingest import instrument_lookup
    from trading_agent.research import ranker_trial

    store = FakeStore({"OK": SESSIONS, "GONE": SESSIONS[:4], "LATE": SESSIONS[:-1]})
    real_check = coverage.check
    monkeypatch.setattr(coverage, "check", lambda symbols, interval, days: real_check(
        symbols, interval, days, store=store, now=at("2026-10-24 12:00")))
    monkeypatch.setattr(instrument_lookup, "get_instrument_lookup",
                        lambda: SimpleNamespace(get_token=lambda s: None if s == "GONE" else 1))
    monkeypatch.setattr(ranker_trial, "status", lambda arm: {"status": "not_started"})
    run = CliRunner().invoke(cli.app, ["data-check", "OK", "GONE", "LATE", "--no-token"])
    assert run.exit_code == 1, run.output                     # LATE is a real gap
    assert "not listed by Kite 1" in run.output and "GONE" in run.output
    assert "fix: trading-agent backfill LATE --interval minute --days 30 --missing-only" in run.output
    healthy = CliRunner().invoke(cli.app, ["data-check", "OK", "GONE", "--no-token"])
    assert healthy.exit_code == 0, healthy.output             # an unlisted symbol alone does not fail
