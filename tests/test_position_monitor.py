"""Exit-path behaviour, exercised against a real schema/SQLite DB.

Covers the three states the monitor has to get right, and the ways it used to
get them wrong:
  * a bar whose LOW pierces the stop but whose CLOSE is back above it
  * a stalled feed, which must not be dressed up as HOLD or as a P&L
  * a live feed, taking the ordinary path

The last two tests run the whole surface - get_all_position_statuses() ->
run_monitor_cycle() -> position_alerts -> /api/wallet - rather than the parts.
"""
from datetime import datetime, timedelta

import pytest

from api.serializers import jsonable
from storage import db as db_module

# A weekday inside the NSE session (9:15-15:30 IST). Freshness is only ever
# judged against a clock inside the session, so both live and stale cases here
# are pinned to this "now".
SESSION_NOW = datetime(2026, 9, 29, 11, 0, 0)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A real database: real schema, real SQL, no network."""
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()
    return db_module


def write_candles(conn, symbol, last_bar_at, bars=40, close=100.0,
                  last_high=None, last_low=None):
    """`bars` flat one-minute candles ending at `last_bar_at`.

    Only the final bar's high/low matter to the exit checks, so they are the
    two knobs - everything else is a fill-in-the-blank series long enough for
    the indicators (>= 20 rows).
    """
    rows = []
    for i in range(bars):
        ts = (last_bar_at - timedelta(minutes=bars - 1 - i)).strftime("%Y-%m-%dT%H:%M:00")
        last = i == bars - 1
        high = last_high if (last and last_high is not None) else close + 0.5
        low = last_low if (last and last_low is not None) else close - 0.5
        rows.append((symbol, ts, close, high, low, close, 1000))
    conn.executemany(
        "INSERT INTO candles (symbol, timestamp, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", rows,
    )
    conn.commit()


def open_position(conn, symbol, action="BUY", entry_price=100.0, stop_loss=99.0,
                  take_profit=102.0, size=10):
    cur = conn.execute(
        "INSERT INTO open_positions (symbol, action, entry_price, position_size, "
        "position_value, risk_amount, stop_loss, take_profit, opened_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
        (symbol, action, entry_price, size, entry_price * size, 10.0,
         stop_loss, take_profit, "2026-09-29T09:20:00"),
    )
    conn.commit()
    return cur.lastrowid


def only_status(db, symbol, now=SESSION_NOW):
    from risk.position_monitor import get_all_position_statuses

    statuses = get_all_position_statuses(fetch_current_signals=False, now=now)
    assert [s.position["symbol"] for s in statuses] == [symbol]
    return statuses[0]


# --- the bar's low/high, not its close ------------------------------------

def test_low_pierces_stop_while_close_stays_above_it(db):
    """The whole point: this bar closed ABOVE the stop and still filled it."""
    conn = db.get_connection()
    write_candles(conn, "PIERCE", SESSION_NOW, close=100.5, last_low=98.6)
    open_position(conn, "PIERCE", stop_loss=99.0)
    conn.close()

    status = only_status(db, "PIERCE")

    assert status.recommendation == "SELL"
    assert status.alert_kind == "stop"
    assert "intrabar" in status.reason
    # The old close-based comparison is precisely what missed this bar.
    assert not (status.current_price <= 99.0)


def test_bar_that_never_reaches_a_level_is_not_an_exit(db):
    conn = db.get_connection()
    write_candles(conn, "QUIET", SESSION_NOW, close=100.5, last_high=100.9, last_low=99.4)
    open_position(conn, "QUIET", stop_loss=99.0)
    conn.close()

    status = only_status(db, "QUIET")

    assert status.recommendation == "HOLD"
    assert status.alert_kind is None
    assert status.current_price == 100.5
    assert status.pnl is not None


def test_stop_wins_when_one_bar_reaches_both_levels(db):
    """Same bar through the stop AND the target: the loss is the answer, and
    the two levels are not ordered inside the bar to justify the other one."""
    conn = db.get_connection()
    write_candles(conn, "BOTH", SESSION_NOW, close=100.5, last_high=102.6, last_low=98.6)
    open_position(conn, "BOTH", stop_loss=99.0, take_profit=102.0)
    conn.close()

    status = only_status(db, "BOTH")

    assert status.recommendation == "SELL"
    assert status.alert_kind == "stop"


def test_short_position_mirrors_on_the_high(db):
    conn = db.get_connection()
    write_candles(conn, "SHORT", SESSION_NOW, close=100.4, last_high=101.4, last_low=99.5)
    open_position(conn, "SHORT", action="SELL", stop_loss=101.0, take_profit=98.0)
    conn.close()

    status = only_status(db, "SHORT")

    assert status.recommendation == "SELL"
    assert status.alert_kind == "stop"
    assert "high 101.4" in status.reason


def test_take_profit_uses_the_favourable_extreme(db):
    conn = db.get_connection()
    write_candles(conn, "TARGET", SESSION_NOW, close=100.2, last_high=102.4, last_low=99.8)
    open_position(conn, "TARGET", stop_loss=99.0, take_profit=102.0)
    conn.close()

    status = only_status(db, "TARGET")

    assert status.recommendation == "SELL"
    assert status.alert_kind == "target"


def test_signal_reversal_still_fires_after_the_level_checks(db):
    from risk.position_monitor import get_position_status

    conn = db.get_connection()
    write_candles(conn, "FLIP", SESSION_NOW, close=100.2)
    position_id = open_position(conn, "FLIP", stop_loss=95.0, take_profit=110.0)
    row = dict(conn.execute("SELECT * FROM open_positions WHERE id = ?", (position_id,)).fetchone())
    conn.close()

    status = get_position_status(row, current_rule_action="SELL", now=SESSION_NOW)

    assert status.recommendation == "SELL"
    assert status.alert_kind == "reversal"
    # A HOLD from the rule path is an abstention, not a reversal.
    assert get_position_status(row, current_rule_action="HOLD", now=SESSION_NOW).alert_kind is None


# --- freshness: what may and may not be claimed ---------------------------

def test_stale_feed_reports_stale_with_no_fabricated_pnl(db):
    conn = db.get_connection()
    write_candles(conn, "STALE", SESSION_NOW - timedelta(minutes=90), close=100.5)
    open_position(conn, "STALE", stop_loss=95.0, take_profit=110.0)
    conn.close()

    status = only_status(db, "STALE")

    assert status.recommendation == "STALE"
    assert status.stale is True
    assert status.alert_kind == "stale"
    # The mark is withheld rather than shown as a fact, and NOT as zero.
    assert status.pnl is None and status.pnl_pct is None
    assert status.current_price is None and status.current_value is None
    assert status.candle_age_minutes == 90.0
    assert "HOLD cannot be confirmed" in status.reason


def test_a_breach_on_a_stale_candle_is_still_reported(db):
    """A breach is a positive observation about a bar that exists, so a
    stalled feed makes it late, not false."""
    conn = db.get_connection()
    write_candles(conn, "STALEBREACH", SESSION_NOW - timedelta(minutes=90),
                  close=100.5, last_low=98.6)
    open_position(conn, "STALEBREACH", stop_loss=99.0)
    conn.close()

    status = only_status(db, "STALEBREACH")

    assert status.recommendation == "SELL"
    assert status.alert_kind == "stop"
    assert status.stale is True
    assert "90 min old" in status.reason      # the age qualifies it ...
    assert status.current_price == 100.5      # ... and the price is still given


def test_outside_the_session_old_data_is_normal_not_stale(db):
    """After the close, an old candle is the correct state of the world -
    only a stall DURING the session is a failure."""
    conn = db.get_connection()
    write_candles(conn, "AFTERHOURS", SESSION_NOW - timedelta(minutes=90), close=100.5)
    open_position(conn, "AFTERHOURS", stop_loss=95.0, take_profit=110.0)
    conn.close()

    status = only_status(db, "AFTERHOURS", now=datetime(2026, 9, 29, 17, 0, 0))

    assert status.recommendation == "HOLD"
    assert status.stale is False
    assert status.pnl is not None


def test_a_position_with_no_candles_is_listed_not_dropped(db):
    conn = db.get_connection()
    write_candles(conn, "READABLE", SESSION_NOW, close=100.5)
    open_position(conn, "READABLE", stop_loss=95.0, take_profit=110.0)
    open_position(conn, "NOCANDLES", stop_loss=95.0, take_profit=110.0)
    conn.close()

    from risk.position_monitor import get_all_position_statuses

    statuses = get_all_position_statuses(fetch_current_signals=False, now=SESSION_NOW)
    by_symbol = {s.position["symbol"]: s for s in statuses}

    # Both come back: an unreadable position still risks capital, so hiding it
    # from the only view that shows it is the failure, not a tidy omission.
    assert set(by_symbol) == {"READABLE", "NOCANDLES"}
    unreadable = by_symbol["NOCANDLES"]
    assert unreadable.recommendation == "NO_DATA"
    assert unreadable.alert_kind == "no_data"
    assert unreadable.pnl is None


# --- the scheduled pass ---------------------------------------------------

def test_monitor_cycle_records_once_and_closes_nothing(db):
    from risk.position_monitor import run_monitor_cycle

    conn = db.get_connection()
    write_candles(conn, "PIERCE", SESSION_NOW, close=100.5, last_low=98.6)
    position_id = open_position(conn, "PIERCE", stop_loss=99.0)
    conn.close()

    first = run_monitor_cycle(now=SESSION_NOW)
    assert first == {"checked": 1, "alerts": 1, "recorded": 1}

    row = db.get_connection().execute("SELECT * FROM position_alerts").fetchone()
    assert row["position_id"] == position_id
    assert row["kind"] == "stop"
    assert row["recommendation"] == "SELL"
    assert row["candle_timestamp"] == "2026-09-29T11:00:00"

    # Re-firing on the same bar must not stack up rows every minute.
    second = run_monitor_cycle(now=SESSION_NOW)
    assert second == {"checked": 1, "alerts": 1, "recorded": 0}

    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM position_alerts").fetchone()[0] == 1
    # Recorded, not acted on.
    assert conn.execute("SELECT status FROM open_positions WHERE id = ?", (position_id,)).fetchone()[0] == "open"
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    conn.close()


def test_monitor_cycle_records_nothing_on_a_clean_book(db):
    from risk.position_monitor import run_monitor_cycle

    conn = db.get_connection()
    write_candles(conn, "CALM", SESSION_NOW, close=100.2)
    open_position(conn, "CALM", stop_loss=95.0, take_profit=110.0)
    conn.close()

    assert run_monitor_cycle(now=SESSION_NOW) == {"checked": 1, "alerts": 0, "recorded": 0}
    assert db.get_connection().execute("SELECT COUNT(*) FROM position_alerts").fetchone()[0] == 0


def test_monitor_cycle_on_an_empty_book(db):
    assert db.get_connection().execute("SELECT COUNT(*) FROM position_alerts").fetchone()[0] == 0

    from risk.position_monitor import run_monitor_cycle

    assert run_monitor_cycle(now=SESSION_NOW) == {"checked": 0, "alerts": 0, "recorded": 0}


# --- the API surface ------------------------------------------------------

def test_unreadable_position_cannot_break_the_wallet_sums(db):
    """The wallet keeps the numbers it had: a position the monitor cannot mark
    contributed nothing to them before, when the monitor dropped it entirely."""
    conn = db.get_connection()
    open_position(conn, "NOCANDLES", stop_loss=95.0, take_profit=110.0)
    conn.close()

    from api.routers.wallet import wallet

    payload = wallet()

    assert payload["open_positions"] == []
    assert payload["today"]["open_pnl"] == 0.0
    assert payload["today"]["open_positions_count"] == 0
    assert payload["fixed"]["book_balance"] == 500000.0


def test_withheld_mark_does_not_break_the_sidebar_join():
    """The sidebar joins these statuses onto ranked symbols and asks whether
    each held position is profitable. A withheld mark must read as "not a fact
    we have" - reading it as a number raises TypeError, which is a 500 on
    /api/rankings for every held symbol on a stalled feed."""
    from types import SimpleNamespace

    from analysis.rankings import annotate_contradictions

    scored = [{"symbol": "HELD", "bullish_score": 5.0, "bearish_score": 1.0,
               "dominant_bias": None, "contradiction": None}]
    unmarkable = SimpleNamespace(position={"symbol": "HELD", "action": "BUY"}, pnl=None)

    result = annotate_contradictions(scored, [unmarkable])

    assert result[0]["contradiction"] is None
    assert result[0]["dominant_bias"] == "bullish"


def test_status_serializes_with_a_null_mark(db):
    conn = db.get_connection()
    write_candles(conn, "STALE", SESSION_NOW - timedelta(minutes=90), close=100.5)
    open_position(conn, "STALE", stop_loss=95.0, take_profit=110.0)
    conn.close()

    payload = jsonable(only_status(db, "STALE"))

    assert payload["pnl"] is None
    assert payload["current_price"] is None
    assert payload["recommendation"] == "STALE"
    assert payload["stale"] is True
    assert payload["candle_age_minutes"] == 90.0
