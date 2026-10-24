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


def test_thin_stocks_are_covered_once_their_sessions_were_fully_fetched(tmp_path):
    from trading_agent.ingest.coverage import covered_days, manifest_path, record_fetch

    # A thin stock: first trade 10:30, nothing on Oct 15 at all.
    thin = [(d,) for (d,) in SESSIONS if d != "2026-10-15"]
    store = FakeStore({"THIN": [], "OK": SESSIONS})
    store.frames["THIN"] = pd.DataFrame({"timestamp": pd.DatetimeIndex([]).append(
        [pd.date_range(f"{d} 10:30", f"{d} 15:29", freq="7min", tz=IST).tz_convert("UTC") for (d,) in thin])})
    now = at("2026-10-24 12:00")
    before = check(["THIN", "OK"], store=store, now=now, manifest_root=tmp_path)
    assert before["problems"]["THIN"]["status"] == "behind"
    # Two overlapping fetches; the second ends mid-session on Oct 23, so that day is not yet covered.
    record_fetch("THIN", "minute", at("2026-10-01 00:00"), at("2026-10-16 16:00"), root=tmp_path)
    record_fetch("THIN", "minute", at("2026-10-16 09:00"), at("2026-10-23 12:00"), root=tmp_path)
    assert len(__import__("json").loads(manifest_path("THIN", "minute", tmp_path).read_text())["fetched"]) == 1
    partial = check(["THIN", "OK"], store=store, now=now, manifest_root=tmp_path)
    assert partial["problems"]["THIN"] == {"status": "behind", "last_complete": "2026-10-22", "missing": []}
    record_fetch("THIN", "minute", at("2026-10-23 12:00"), at("2026-10-23 16:00"), root=tmp_path)
    after = check(["THIN", "OK"], store=store, now=now, manifest_root=tmp_path)
    assert after["ok"] and after["counts"]["up_to_date"] == 2   # Oct 15 (no trades) is covered by the fetch
    covered, seen = covered_days(store, "THIN", "minute", root=tmp_path)
    assert date(2026, 10, 14) in covered and date(2026, 10, 15) not in seen


def test_backfill_window_starts_where_data_check_starts(monkeypatch):
    from datetime import datetime as real_datetime, timezone

    from trading_agent.ingest.coverage import window_start
    from trading_agent.ingest.fetch_historical import HistoricalFetcher

    seen = {}
    fetcher = object.__new__(HistoricalFetcher)
    monkeypatch.setattr(fetcher, "stored_days", lambda symbol, interval: (set(), set()), raising=False)
    monkeypatch.setattr(fetcher, "fetch_symbol", lambda symbol, interval, from_date, to_date: seen.update(
        start=from_date) or pd.DataFrame(), raising=False)
    fetcher.backfill(["X"], "minute", days=30, missing_only=True)
    start = pd.Timestamp(seen["start"]).tz_convert(IST)
    first = window_start(30)  # the first weekday on or after it, if it falls on a weekend
    assert first <= start.date() <= first + pd.Timedelta(days=2) and (start.hour, start.minute) == (9, 15)
    assert real_datetime.now(timezone.utc) - seen["start"] > pd.Timedelta(days=30)
