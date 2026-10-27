"""Forward test of two entry filters, written down before its first session (2026-10-28).

Every historical window has been inspected, so these filters are judged only on
sessions from ``START_SESSION`` on. Two questions, fixed in ``SPEC``:

  time window   do directional signals do better when taken inside 09:30-14:30 IST
                than when the first signal of the session is taken, whenever it comes?
  replay gate   inside the window, do trades whose trade replay is not
                ``conclusive_negative`` do better than the ones it would refuse?

Trades. Stored BUY/SELL signals of every strategy. One trade per symbol, strategy
and session: ``all`` takes the session's first directional signal, ``window`` the
first one inside the window. Entry at the open of the first bar at or after the
signal, stop 0.7% away (the plan floor; 1.5 x 1-minute ATR is narrower on almost
every live name), target 2R, exit at the close of the last bar ending by 15:15. A
bar touching both counts as the stop, a bar opening through the stop exits at its
open. Net R subtracts 14 bps round trip. 1-minute bars when the session has them,
else 5-minute bars.

Replay gate. Up to ``replay_cap`` window trades a session, in a seeded random
order, are replayed at their entry time with the default replay settings; the
replay sees only sessions before the entry date and today's bars before the entry.

Records. ``record`` writes one file per completed session, once, after 15:20 IST.
A session can be recorded late (nothing is decided, only scored), but never
rewritten. The verdict uses the first ``verdict_sessions`` recorded sessions only:
a filter is adopted when the per-session mean net R of the trades it keeps beats
the comparison group with a paired t of at least ``t_bar``. Research only: nothing
here changes a plan, sizes or places an order.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger
from trading_agent.research.strategy_search import _write_atomic, git_revision

logger = get_logger(__name__)

IST = "Asia/Kolkata"
VERSION = "forward-filters-v1"
START_SESSION = date(2026, 10, 28)
SPEC = {
    "version": VERSION,
    "start_session": START_SESSION.isoformat(),
    "window_ist": ["09:30", "14:30"],
    "stop_pct": 0.007,
    "reward_risk": 2.0,
    "exit_ist": "15:15",
    "cost_bps": 14.0,
    "replay_cap": 40,
    "replay_seed": 23,
    "verdict_sessions": 20,
    "t_bar": 2.0,
    "decision": ("time window: per-session mean net R of window trades minus all trades; replay gate: "
                 "per-session mean net R of kept minus refused window trades, sessions with both. "
                 "Adopt a filter when the mean difference is positive with paired t >= t_bar over the "
                 "first verdict_sessions recorded sessions."),
}
SPEC_SHA256 = hashlib.sha256(json.dumps(SPEC, sort_keys=True).encode("utf-8")).hexdigest()
SETTLE_AFTER = 15 * 60 + 20


def _minutes(clock: str) -> int:
    hours, minutes = clock.split(":")
    return int(hours) * 60 + int(minutes)


WINDOW = tuple(_minutes(c) for c in SPEC["window_ist"])
EXIT_MINUTE = _minutes(SPEC["exit_ist"])


def forward_dir() -> Path:
    return get_settings().data_dir / "research" / "forward_filters"


def record_path(session: date) -> Path:
    return forward_dir() / "sessions" / f"{session}.json"


def in_window(signal_time: pd.Timestamp) -> bool:
    local = signal_time.tz_convert(IST)
    return WINDOW[0] <= local.hour * 60 + local.minute < WINDOW[1]


def session_frame(candles: pd.DataFrame, session: date, bar_minutes: int) -> pd.DataFrame | None:
    """The session's bars with IST minutes, or None when they do not reach the 15:15 exit."""
    if candles is None or candles.empty:
        return None
    stamp = pd.to_datetime(candles["timestamp"], utc=True)
    local = stamp.dt.tz_convert(IST)
    day = candles.assign(stamp=stamp, minute=local.dt.hour * 60 + local.dt.minute)[local.dt.date == session]
    day = day.sort_values("stamp")
    if day.empty or day["minute"].max() + bar_minutes < EXIT_MINUTE - 15:  # store stops well before the exit
        return None
    return day


def bracket(day: pd.DataFrame, signal_time: pd.Timestamp, side: str, bar_minutes: int) -> dict | None:
    """Fixed-percentage bracket from the first bar at or after the signal to 15:15."""
    sign = 1 if side == "BUY" else -1
    after = day[(day["stamp"] >= signal_time) & (day["minute"] + bar_minutes <= EXIT_MINUTE)]
    if after.empty:
        return None
    entry = float(after["open"].iloc[0])
    stop_pct = SPEC["stop_pct"]
    stop = entry * (1 - sign * stop_pct)
    target = entry * (1 + sign * stop_pct * SPEC["reward_risk"])
    exit_price, reason = None, None
    for bar in after.itertuples():
        o, h, low = float(bar.open), float(bar.high), float(bar.low)
        if (o <= stop) if sign == 1 else (o >= stop):
            exit_price, reason = o, "gap_stop"
        elif (o >= target) if sign == 1 else (o <= target):
            exit_price, reason = target, "target"
        elif (low <= stop) if sign == 1 else (h >= stop):
            exit_price, reason = stop, "stop"
        elif (h >= target) if sign == 1 else (low <= target):
            exit_price, reason = target, "target"
        if exit_price is not None:
            break
    if exit_price is None:
        exit_price, reason = float(after["close"].iloc[-1]), "session_exit"
    net = sign * (exit_price - entry) / entry - SPEC["cost_bps"] / 10000
    return {"entry_time": after["stamp"].iloc[0].isoformat(), "entry": entry, "stop": stop, "target": target,
            "exit": exit_price, "exit_reason": reason, "net_bps": round(net * 1e4, 3),
            "net_r": round(net / stop_pct, 4), "win": int(net > 0)}


def first_trades(signals: pd.DataFrame) -> dict[str, list[dict]]:
    """The ``all`` and ``window`` signal of each symbol and strategy, before pricing."""
    directional = signals[signals["action"].isin(["BUY", "SELL"])].sort_values(["timestamp", "id"])
    out = {"all": [], "window": []}
    for _, group in directional.groupby(["symbol", "strategy"], sort=True):
        out["all"].append(group.iloc[0].to_dict())
        inside = group[group["timestamp"].map(in_window)]
        if not inside.empty:
            out["window"].append(inside.iloc[0].to_dict())
    return out


def _load(symbol: str, session: date, store) -> tuple[pd.DataFrame, pd.DataFrame, int] | None:
    """Full history (for the replay pool) and the session's bars, 1-minute when complete."""
    for interval, minutes in (("minute", 1), ("5minute", 5)):
        try:
            candles = store.read_candles(symbol, interval)
        except Exception:
            continue
        day = session_frame(candles, session, minutes)
        if day is not None:
            return candles, day, minutes
    return None


def _replay(trade: dict, candles: pd.DataFrame, minutes: int) -> str:
    from trading_agent.research import trade_replay as tr

    plan = tr.Plan(symbol=trade["symbol"], side=trade["side"], entry=trade["entry"], stop=trade["stop"],
                   target=trade["target"], entry_time=pd.Timestamp(trade["entry_time"]).to_pydatetime())
    try:
        doc = tr.replay(plan, frame=candles, interval="minute" if minutes == 1 else "5minute")
    except Exception as e:  # too little history and the like: not a verdict either way
        return f"error: {e}"
    return doc["conclusion"]["status"]


def score_session(session: date, signals: pd.DataFrame, store, replay=_replay) -> dict:
    """Priced ``all`` and ``window`` trades for one session; window trades carry a replay status."""
    picked = first_trades(signals)
    cache: dict[str, tuple | None] = {}
    priced = {"all": [], "window": []}
    for variant, rows in picked.items():
        for row in rows:
            symbol = row["symbol"]
            if symbol not in cache:
                cache[symbol] = _load(symbol, session, store)
            loaded = cache[symbol]
            base = {"signal_id": int(row["id"]), "symbol": symbol, "strategy": row["strategy"],
                    "side": row["action"], "signal_time": row["timestamp"].isoformat()}
            result = bracket(loaded[1], row["timestamp"], row["action"], loaded[2]) if loaded else None
            priced[variant].append({**base, **result, "bar_minutes": loaded[2]} if result
                                   else {**base, "status": "unpriced"})
    window = [t for t in priced["window"] if "net_r" in t]
    order = np.random.default_rng(SPEC["replay_seed"] + session.toordinal()).permutation(len(window))
    for rank, index in enumerate(order):
        trade = window[index]
        if rank < SPEC["replay_cap"]:
            loaded = cache[trade["symbol"]]
            trade["replay"] = replay(trade, loaded[0], loaded[2])
        else:
            trade["replay"] = "not_sampled"
    return priced


def _signals_for(session: date, load=None) -> pd.DataFrame:
    from trading_agent.research.decision_outcomes import load_signals

    start = pd.Timestamp(f"{session} 00:00", tz=IST).tz_convert(UTC).to_pydatetime()
    frame = (load or load_signals)(start)
    if frame.empty:
        return frame
    return frame[frame["timestamp"].dt.tz_convert(IST).dt.date == session]


def record(session: date, store=None, signals: pd.DataFrame | None = None, now: datetime | None = None,
           replay=_replay) -> dict | None:
    """Write the session's record once; None when it already exists or is not due."""
    path = record_path(session)
    now_ist = pd.Timestamp(now or datetime.now(UTC)).tz_convert(IST)
    if path.exists() or session < START_SESSION or session.weekday() >= 5:
        return None
    if session > now_ist.date() or (session == now_ist.date()
                                    and now_ist.hour * 60 + now_ist.minute < SETTLE_AFTER):
        return None
    if store is None:
        from trading_agent.storage.parquet_store import get_parquet_store
        store = get_parquet_store()
    signals = _signals_for(session) if signals is None else signals
    if signals.empty:
        return None  # no stored signals (the scheduler did not run): nothing to score
    priced = score_session(session, signals, store, replay)
    doc = {"session": session.isoformat(), "spec_sha256": SPEC_SHA256, "git_revision": git_revision(),
           "recorded_at": now_ist.tz_convert(UTC).isoformat(), "signals": int(len(signals)), **priced}
    _write_atomic(path, doc, exclusive=True)
    return doc


def record_due(now: datetime | None = None, store=None) -> list[str]:
    """Record every completed session since the start that has no record yet."""
    today = pd.Timestamp(now or datetime.now(UTC)).tz_convert(IST).date()
    written, day = [], START_SESSION
    while day <= today:
        if record(day, store=store, now=now):
            written.append(day.isoformat())
        day += timedelta(days=1)
    return written


def _group(trades: list[dict]) -> dict:
    scored = [t for t in trades if "net_r" in t]
    if not scored:
        return {"trades": 0}
    r = np.array([t["net_r"] for t in scored])
    return {"trades": len(scored), "win_pct": round(float((r > 0).mean()) * 100, 1),
            "mean_net_r": round(float(r.mean()), 4),
            "mean_net_bps": round(float(np.mean([t["net_bps"] for t in scored])), 2)}


def _paired(pairs: list[tuple[float, float]]) -> dict:
    """Mean and t of per-session differences (first minus second)."""
    diff = np.array([a - b for a, b in pairs])
    if len(diff) < 2 or float(diff.std(ddof=1)) == 0:
        return {"sessions": int(len(diff)), "mean_diff_r": round(float(diff.mean()), 4) if len(diff) else None,
                "t": None}
    t = float(diff.mean() / (diff.std(ddof=1) / math.sqrt(len(diff))))
    return {"sessions": int(len(diff)), "mean_diff_r": round(float(diff.mean()), 4), "t": round(t, 3)}


def _kept(trade: dict) -> bool:
    """Replayed and not refused; unsampled trades and replay errors are in neither group."""
    return trade.get("replay") in ("inconclusive", "conclusive_positive")


def _refused(trade: dict) -> bool:
    return trade.get("replay") == "conclusive_negative"


def _session_mean(trades: list[dict]) -> float | None:
    r = [t["net_r"] for t in trades if "net_r" in t]
    return float(np.mean(r)) if r else None


def load_records() -> list[dict]:
    folder = forward_dir() / "sessions"
    docs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))] if folder.exists() else []
    return [d for d in docs if d.get("spec_sha256") == SPEC_SHA256]


def report(records: list[dict] | None = None) -> dict:
    records = load_records() if records is None else records
    records = sorted(records, key=lambda d: d["session"])
    judged = records[:SPEC["verdict_sessions"]]
    done = len(judged) >= SPEC["verdict_sessions"]

    def tally(docs):
        all_t = [t for d in docs for t in d["all"]]
        window = [t for d in docs for t in d["window"]]
        kept = [t for t in window if _kept(t)]
        refused = [t for t in window if _refused(t)]
        time_pairs, gate_pairs = [], []
        for d in docs:
            a, w = _session_mean(d["all"]), _session_mean(d["window"])
            if a is not None and w is not None:
                time_pairs.append((w, a))
            k = _session_mean([t for t in d["window"] if _kept(t)])
            r = _session_mean([t for t in d["window"] if _refused(t)])
            if k is not None and r is not None:
                gate_pairs.append((k, r))
        strategies = sorted({t["strategy"] for t in all_t})
        return {
            "time_window": {"all": _group(all_t), "window": _group(window), "paired": _paired(time_pairs)},
            "replay_gate": {"kept": _group(kept), "refused": _group(refused), "paired": _paired(gate_pairs)},
            "by_strategy": {s: {"all": _group([t for t in all_t if t["strategy"] == s]),
                                "window": _group([t for t in window if t["strategy"] == s])} for s in strategies},
        }

    def decide(paired):
        if not done:
            return "collecting"
        t = paired.get("t")
        return "adopt" if t is not None and t >= SPEC["t_bar"] and paired["mean_diff_r"] > 0 else "reject"

    judged_tally = tally(judged)
    return {
        "spec": SPEC, "spec_sha256": SPEC_SHA256, "sessions_recorded": len(records),
        "sessions": [d["session"] for d in records],
        "verdict": {"time_window": decide(judged_tally["time_window"]["paired"]),
                    "replay_gate": decide(judged_tally["replay_gate"]["paired"]),
                    "on_sessions": [d["session"] for d in judged]},
        "judged": judged_tally,
        "all_sessions": tally(records) if len(records) > len(judged) else None,
        "scope": "research_only",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--record", action="store_true", help="Record every completed session first")
    args = parser.parse_args(argv)
    if args.record:
        print("recorded:", ", ".join(record_due()) or "none due")
    print(json.dumps(report(), indent=1, default=str))
    return 0
