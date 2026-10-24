"""Prospective paper trial of the frozen cross-sectional ranker.

Every historical window has already been inspected, so only sessions after the
trial starts are clean evidence. ``start`` freezes the configuration, universe
and first session in an immutable, hashed spec. Each session then has two
records, each written once and never overwritten:

  pick    after the 09:15 bar and before the deadline: train on the preceding
          ``train_days`` labelled sessions, rank the liquid universe and record
          the top and bottom ``k`` with the time of the pick.
  settle  after 15:20: price the book. The primary return enters at the open of
          the first minute bar that starts after the pick was recorded and exits
          at the 15:15 price. The at-open return is kept only for comparison
          with the backtest, which assumed an entry exactly at the open.

A trading session without a pick is recorded as missed, never filled in later.

There are two arms, each with its own frozen spec and records: ``rf`` (the
paper's random forest; the original trial) and ``hgb`` (gradient boosting on
per-session ranked features, the ranking lab's validation-best). They share the
morning's data load. ``DONE_T_STAT = 2.0`` keeps the one-sided family-wise
error across both arms below 5% (Bonferroni bar for two arms: 1.96).
The verdict is computed once, on the first ``DONE_SESSIONS`` settled sessions,
so watching the running t-stat cannot stop the trial on a lucky streak.

Research only: nothing here places orders or changes a live decision.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger
from trading_agent.research.cross_sectional import (
    CRORE,
    EXIT_MINUTE,
    IST,
    SESSION_OPEN,
    RankConfig,
    _stats,
    build_features,
    default_universe,
    latest_ranking,
    load_panel,
)
from trading_agent.research.strategy_search import _write_atomic, git_revision

logger = get_logger(__name__)

VERSION = "ranker-trial-v1"
INTERVAL = "minute"
PICK_DEADLINE = 9 * 60 + 45  # IST minutes; later picks are refused and the session counts as missed
SETTLE_AFTER = EXIT_MINUTE + 5
HISTORY_DAYS = 160  # calendar days read for features: covers lags, training and the 20-day liquidity mean
DONE_SESSIONS = 60
DONE_T_STAT = 2.0
ARMS = {"rf": "ranker_trial", "hgb": "ranker_trial_hgb"}  # arm -> directory under data/research
ARM_CONFIGS = {"rf": RankConfig(), "hgb": RankConfig(model="hgb")}


class TrialError(RuntimeError):
    """A step that cannot run now; nothing was written."""


# --- files ---------------------------------------------------------------------------------

def trial_dir(arm: str = "rf") -> Path:
    if arm not in ARMS:
        raise TrialError(f"Unknown arm {arm!r}; choose from {', '.join(ARMS)}")
    return get_settings().data_dir / "research" / ARMS[arm]


def spec_path(arm: str = "rf") -> Path:
    return trial_dir(arm) / "spec.json"


def record_path(session: date, kind: str, arm: str = "rf") -> Path:
    return trial_dir(arm) / "sessions" / f"{session}.{kind}.json"


def _digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, default=str, sort_keys=True).encode("utf-8")).hexdigest()


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _ist(now: datetime | None) -> pd.Timestamp:
    return pd.Timestamp(now or datetime.now(UTC)).tz_convert(IST)


def _minute(stamp: pd.Timestamp) -> int:
    return stamp.hour * 60 + stamp.minute


# --- spec ----------------------------------------------------------------------------------

def first_session(now: datetime | None = None) -> date:
    """Today if the market has not opened yet, otherwise the next weekday."""
    stamp = _ist(now)
    day = stamp.date()
    if stamp.weekday() < 5 and _minute(stamp) < SESSION_OPEN:
        return day
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def start(start_session: date | None = None, universe: list[str] | None = None,
          config: RankConfig | None = None, now: datetime | None = None, arm: str = "rf") -> dict:
    """Freeze one arm. Refuses to start in the past or to replace an existing spec."""
    config = config or ARM_CONFIGS[arm]
    if spec_path(arm).exists():
        raise FileExistsError(f"{spec_path(arm)} exists; the {arm} arm is already frozen")
    earliest = first_session(now)
    start_session = start_session or earliest
    if start_session < earliest:
        raise TrialError(f"Start {start_session} is not prospective; the earliest allowed is {earliest}")
    symbols = sorted({s.upper() for s in (universe or default_universe())})
    spec = {
        "version": VERSION, "created_at": _ist(now).tz_convert(UTC).isoformat(),
        "start_session": str(start_session), "interval": INTERVAL, "config": asdict(config),
        "universe": symbols, "universe_sha256": _digest({"universe": symbols}), "git_revision": git_revision(),
        "pick_deadline_ist": f"{PICK_DEADLINE // 60:02d}:{PICK_DEADLINE % 60:02d}",
        "entry": "open of the first minute bar that starts after the pick is recorded",
        "exit": "close of the last bar before 15:15 IST",
        "retraining": f"every session, on the preceding {config.train_days} labelled sessions",
        "cost": f"{config.cost_bps} bps round trip per position",
        "done_when": {"sessions": DONE_SESSIONS, "mean_daily_net_bps_above": 0, "t_stat_at_least": DONE_T_STAT,
                      "beats_random_baseline": True,
                      "evaluated_on": f"the first {DONE_SESSIONS} settled sessions only"},
    }
    if arm != "rf":  # the rf spec was frozen before arms existed; its content must not change
        spec["arm"] = arm
        spec["done_when"]["family_wise"] = (f"{len(ARMS)} arms; t >= {DONE_T_STAT} keeps the one-sided "
                                            "family-wise error below 5%")
    spec["spec_sha256"] = _digest(spec)
    _write_atomic(spec_path(arm), spec, exclusive=True)
    logger.info("ranker_trial_started", arm=arm, start_session=str(start_session), symbols=len(symbols))
    return spec


def load_spec(arm: str = "rf") -> dict | None:
    spec = _read(spec_path(arm))
    if spec is None:
        return None
    body = {k: v for k, v in spec.items() if k != "spec_sha256"}
    if _digest(body) != spec.get("spec_sha256"):
        raise TrialError("Trial spec does not match its hash; it was edited after freezing")
    return spec


def _require_spec(arm: str = "rf") -> tuple[dict, RankConfig]:
    spec = load_spec(arm)
    if spec is None:
        raise TrialError(f"The {arm} arm has not been started")
    return spec, RankConfig(**spec["config"])


def started_arms() -> list[str]:
    return [arm for arm in ARMS if spec_path(arm).exists()]


# --- data ----------------------------------------------------------------------------------

def _refresh(symbols: list[str], session: date, until: pd.Timestamp) -> list[str]:
    """Fetch the session's minute candles from Kite; returns symbols that failed."""
    from trading_agent.ingest.fetch_historical import HistoricalFetcher

    fetcher = HistoricalFetcher()
    begin = pd.Timestamp(f"{session} 09:15", tz=IST)
    end = min(until, pd.Timestamp(f"{session} 15:30", tz=IST))
    failed = []
    for symbol in symbols:
        try:
            fetcher.fetch_symbol(symbol, INTERVAL, from_date=begin.tz_convert(UTC).to_pydatetime(),
                                 to_date=end.tz_convert(UTC).to_pydatetime())
        except Exception as error:  # one symbol must not stop the session
            logger.warning("ranker_trial_refresh_failed", symbol=symbol, error=str(error))
            failed.append(symbol)
    return failed


def _store(store):
    from trading_agent.storage.parquet_store import get_parquet_store

    return store or get_parquet_store()


def _liquid(symbols: list[str], config: RankConfig, store, since: pd.Timestamp) -> list[str]:
    """Symbols whose trailing 20-session mean traded value, as of the last stored session, passes the filter."""
    if config.min_avg_value_cr <= 0:
        return symbols
    _, _, _, values = load_panel(symbols, INTERVAL, store, start=since.to_pydatetime())
    if values.empty:
        return []
    mean = values.rolling(20, min_periods=10).mean().iloc[-1]
    return sorted(mean[mean >= config.min_avg_value_cr * CRORE].index)


def session_prices(candles: pd.DataFrame, entry_after: pd.Timestamp) -> dict:
    """Open (09:15 bar), entry (first bar starting at or after ``entry_after``) and 15:15 exit for one session."""
    if candles is None or candles.empty:
        return {"open": None, "entry": None, "exit": None}
    stamp = pd.to_datetime(candles["timestamp"], utc=True).dt.tz_convert(IST)
    frame = candles.assign(stamp=stamp, minute=stamp.dt.hour * 60 + stamp.dt.minute).sort_values("stamp")
    opening = frame[frame["minute"] == SESSION_OPEN]
    entry = frame[(frame["stamp"] >= entry_after) & (frame["minute"] < EXIT_MINUTE)]
    before_exit = frame[frame["minute"] < EXIT_MINUTE]
    exit_ok = len(before_exit) and before_exit["minute"].iloc[-1] >= EXIT_MINUTE - 5
    return {"open": float(opening["open"].iloc[0]) if len(opening) else None,
            "entry": float(entry["open"].iloc[0]) if len(entry) else None,
            "exit": float(before_exit["close"].iloc[-1]) if exit_ok else None}


def _ret(price: dict, key: str) -> float:
    if price.get(key) and price.get("exit"):
        return price["exit"] / price[key] - 1
    return float("nan")


def _finite(value: float) -> float | None:
    return float(value) if np.isfinite(value) else None


# --- daily steps ---------------------------------------------------------------------------

def _check_pick(arm: str, spec: dict, stamp: pd.Timestamp) -> None:
    session = stamp.date()
    if session < date.fromisoformat(spec["start_session"]):
        raise TrialError(f"The {arm} arm starts on {spec['start_session']}")
    path = record_path(session, "pick", arm)
    if path.exists():
        raise FileExistsError(f"{path} exists; picks are recorded once")
    if _minute(stamp) <= SESSION_OPEN:
        raise TrialError("The 09:15 bar is not complete yet")
    if _minute(stamp) > PICK_DEADLINE:
        raise TrialError(f"Past the {spec['pick_deadline_ist']} deadline; this session will be recorded as missed")


def _morning_table(spec: dict, config: RankConfig, stamp: pd.Timestamp, store, refresh: bool):
    """Feature table for today's pick from the liquid universe, with everything after the open blanked."""
    session = stamp.date()
    since = stamp.normalize() - pd.Timedelta(days=HISTORY_DAYS)
    candidates = _liquid(spec["universe"], config, store, since)
    failed = _refresh(candidates, session, stamp) if refresh else []
    opens, closes, exits, values = load_panel(candidates, INTERVAL, store, start=since.to_pydatetime())
    if session not in opens.index or opens.loc[session].notna().sum() < 2 * config.top_k:
        raise TrialError(f"No 09:15 bars stored for {session} (holiday, or data not available yet)")
    # Only today's open is known at the pick; blank anything later so no bar after the pick can leak in.
    for frame in (closes, exits, values):
        if session in frame.index:
            frame.loc[session] = np.nan
    return build_features(opens, closes, config.lags, exits, values, config.min_avg_value_cr * CRORE), failed


def _record_pick(arm: str, spec: dict, config: RankConfig, table: pd.DataFrame, failed: list[str],
                 now: datetime | None, stamp: pd.Timestamp) -> dict:
    session = stamp.date()
    ranking = latest_ranking(table, config, session=session)
    if ranking is None:
        raise TrialError(f"Not enough labelled history before {session} to train")
    picked_at = stamp if now is not None else _ist(None)
    record = {"session": str(session), "status": "picked", "picked_at": picked_at.tz_convert(UTC).isoformat(),
              "spec_sha256": spec["spec_sha256"], "refresh_failed": failed, **ranking}
    _write_atomic(record_path(session, "pick", arm), record, exclusive=True)
    logger.info("ranker_trial_picked", arm=arm, session=str(session), eligible=len(ranking["eligible"]))
    return record


def pick(now: datetime | None = None, store=None, refresh: bool = True, arm: str = "rf") -> dict:
    """Rank today's session after the 09:15 bar and record one arm's book once."""
    spec, config = _require_spec(arm)
    stamp = _ist(now)
    _check_pick(arm, spec, stamp)
    table, failed = _morning_table(spec, config, stamp, _store(store), refresh)
    return _record_pick(arm, spec, config, table, failed, now, stamp)


def _data_key(spec: dict, config: RankConfig) -> tuple:
    """What the morning table depends on; arms that share it share one data load."""
    return spec["universe_sha256"], config.max_lag, config.min_avg_value_cr, config.top_k


def pick_all(now: datetime | None = None, store=None, refresh: bool = True) -> dict:
    """Record every started arm's book, loading (and fetching) the morning's data once per distinct need."""
    stamp = _ist(now)
    store = _store(store)
    results: dict = {}
    tables: dict = {}
    for arm in started_arms():
        try:
            spec, config = _require_spec(arm)
            _check_pick(arm, spec, stamp)
            key = _data_key(spec, config)
            if key not in tables:
                tables[key] = _morning_table(spec, config, stamp, store, refresh and not tables)
            results[arm] = _record_pick(arm, spec, config, *tables[key], now, stamp)
        except (TrialError, FileExistsError) as error:
            logger.warning("ranker_trial_pick_skipped", arm=arm, error=str(error))
            results[arm] = str(error)
    return results


def _is_session(session: date, symbols: list[str], store) -> bool:
    """Whether any of a few universe symbols has a 09:15 bar on ``session``."""
    begin = pd.Timestamp(f"{session} 09:15", tz=IST)
    for symbol in symbols[:10]:
        try:
            frame = store.read_candles(symbol, INTERVAL, start=begin.to_pydatetime(),
                                       end=(begin + pd.Timedelta(minutes=1)).to_pydatetime())
        except Exception:
            continue
        if session_prices(frame, begin)["open"] is not None:
            return True
    return False


def settle(session: date, now: datetime | None = None, store=None, refresh: bool = True,
           arm: str = "rf") -> dict:
    """Price a recorded book after 15:20, or record the session as missed if it has no pick."""
    spec, config = _require_spec(arm)
    stamp = _ist(now)
    if session > stamp.date() or (session == stamp.date() and _minute(stamp) < SETTLE_AFTER):
        raise TrialError(f"{session} cannot be settled before 15:20 IST")
    path = record_path(session, "settle", arm)
    if path.exists():
        raise FileExistsError(f"{path} exists; settlements are recorded once")
    store = _store(store)
    picked = _read(record_path(session, "pick", arm))
    if picked is None:
        if refresh:
            _refresh(spec["universe"][:3], session, stamp)
        if not _is_session(session, spec["universe"], store):
            raise TrialError(f"{session} is not a trading session in the stored data")
        record = {"session": str(session), "status": "missed", "recorded_at": stamp.tz_convert(UTC).isoformat(),
                  "spec_sha256": spec["spec_sha256"]}
        _write_atomic(path, record, exclusive=True)
        return record
    if picked["spec_sha256"] != spec["spec_sha256"]:
        raise TrialError("Pick was recorded under a different spec")
    eligible = picked["eligible"]
    if refresh:
        _refresh(eligible, session, stamp)
    entry_after = pd.Timestamp(picked["picked_at"]).tz_convert(IST).floor("min") + pd.Timedelta(minutes=1)
    begin = pd.Timestamp(f"{session} 09:15", tz=IST)
    prices = {}
    for symbol in eligible:
        try:
            frame = store.read_candles(symbol, INTERVAL, start=begin.to_pydatetime(),
                                       end=(begin + pd.Timedelta(minutes=375)).to_pydatetime())
        except Exception:
            frame = None
        prices[symbol] = session_prices(frame, entry_after)
    cost = config.cost_bps / 1e4

    def side(rows):
        price = [prices.get(r["symbol"], {}) for r in rows]
        ret = np.array([_ret(p, "entry") for p in price])
        at_open = np.array([_ret(p, "open") for p in price])
        out = [{"symbol": r["symbol"], **p, "ret": _finite(a), "ret_at_open": _finite(b)}
               for r, p, a, b in zip(rows, price, ret, at_open)]
        return out, ret, at_open

    longs, long_ret, long_open = side(picked["long"])
    shorts, short_ret, short_open = side(picked["short"])
    unpriced = [r["symbol"] for r in longs + shorts if r["ret"] is None]
    if np.isfinite(long_ret).sum() == 0 or np.isfinite(short_ret).sum() == 0:
        raise TrialError(f"No 15:15 prices for one side of the {session} book; refresh the data and retry")
    gross = 0.5 * np.nanmean(long_ret) - 0.5 * np.nanmean(short_ret)
    at_open_ok = np.isfinite(long_open).any() and np.isfinite(short_open).any()
    gross_open = 0.5 * np.nanmean(long_open) - 0.5 * np.nanmean(short_open) if at_open_ok else None
    # Random books from the same eligible, priced set, with the same entry and exit times.
    pool = np.array([_ret(prices[s], "entry") for s in eligible], dtype=float)
    pool = pool[np.isfinite(pool)]
    random_net = None
    if len(pool) >= 2 * config.top_k:
        rng = np.random.default_rng(config.seed + session.toordinal())
        draws = [rng.permutation(pool) for _ in range(config.random_draws)]
        random_net = float(np.mean([0.5 * d[:config.top_k].mean() - 0.5 * d[-config.top_k:].mean()
                                    for d in draws])) - cost
    record = {"session": str(session), "status": "settled", "settled_at": stamp.tz_convert(UTC).isoformat(),
              "spec_sha256": spec["spec_sha256"], "pick_sha256": _digest(picked),
              "entry_after": entry_after.tz_convert(UTC).isoformat(),
              "net": float(gross) - cost, "gross": float(gross),
              "net_at_open": float(gross_open) - cost if gross_open is not None else None,
              "random_net": random_net, "long": longs, "short": shorts, "unpriced": unpriced}
    _write_atomic(path, record, exclusive=True)
    logger.info("ranker_trial_settled", arm=arm, session=str(session), net_bps=round(record["net"] * 1e4, 2))
    return record


def settle_pending(now: datetime | None = None, store=None, refresh: bool = True,
                   arm: str = "rf") -> list[dict]:
    """Settle every unsettled pick that is due, then record today as missed if it traded without a pick."""
    spec, _ = _require_spec(arm)
    stamp = _ist(now)
    today = stamp.date()
    done = []
    for path in sorted((trial_dir(arm) / "sessions").glob("*.pick.json")):
        session = date.fromisoformat(path.name.split(".")[0])
        if record_path(session, "settle", arm).exists():
            continue
        try:
            done.append(settle(session, now, store, refresh, arm))
        except TrialError as error:
            logger.warning("ranker_trial_settle_deferred", arm=arm, session=str(session), error=str(error))
    due = today >= date.fromisoformat(spec["start_session"]) and _minute(stamp) >= SETTLE_AFTER
    if due and not record_path(today, "pick", arm).exists() and not record_path(today, "settle", arm).exists():
        try:
            done.append(settle(today, now, store, refresh=False, arm=arm))
        except TrialError:
            pass  # not a trading day
    return done


def settle_all(now: datetime | None = None, store=None, refresh: bool = True) -> dict:
    """Settle every started arm. Only the first fetches: the arms rank the same eligible names."""
    out = {}
    for arm in started_arms():
        out[arm] = settle_pending(now, store, refresh, arm)
        refresh = refresh and not any(r.get("status") == "settled" for r in out[arm])
    return out


# --- report --------------------------------------------------------------------------------

def status(arm: str = "rf") -> dict:
    spec = load_spec(arm)
    if spec is None:
        return {"status": "not_started", "arm": arm}
    sessions = trial_dir(arm) / "sessions"
    settles = [_read(p) for p in sorted(sessions.glob("*.settle.json"))]
    picks = sorted(p.name.split(".")[0] for p in sessions.glob("*.pick.json"))
    settled = [r for r in settles if r and r["status"] == "settled"]
    missed = [r["session"] for r in settles if r and r["status"] == "missed"]
    net = np.array([r["net"] for r in settled])
    report = {
        "status": "collecting", "arm": arm, "version": spec["version"], "start_session": spec["start_session"],
        "spec_sha256": spec["spec_sha256"], "config": spec["config"], "universe_size": len(spec["universe"]),
        "done_when": spec["done_when"], "picks": len(picks), "settled": len(settled), "missed": missed,
        "pending": sorted(set(picks) - {r["session"] for r in settles if r}),
        "model": _stats(net),
        "at_open": _stats(np.array([r["net_at_open"] for r in settled if r["net_at_open"] is not None])),
        "random_baseline": _stats(np.array([r["random_net"] for r in settled if r["random_net"] is not None])),
        "sessions": [{"session": r["session"], "net_bps": round(r["net"] * 1e4, 2),
                      "net_at_open_bps": round(r["net_at_open"] * 1e4, 2) if r["net_at_open"] is not None else None,
                      "random_bps": round(r["random_net"] * 1e4, 2) if r["random_net"] is not None else None,
                      "long": [p["symbol"] for p in r["long"]], "short": [p["symbol"] for p in r["short"]],
                      "unpriced": r["unpriced"]} for r in settled][-30:],
    }
    if len(settled) >= DONE_SESSIONS:
        first = settled[:DONE_SESSIONS]
        model = _stats(np.array([r["net"] for r in first]))
        baseline = _stats(np.array([r["random_net"] for r in first if r["random_net"] is not None]))
        checks = {"positive_after_costs": model["mean_daily_net_bps"] > 0,
                  "t_stat_at_least_2": (model.get("t_stat") or 0) >= DONE_T_STAT,
                  "beats_random_baseline": model["mean_daily_net_bps"] > baseline.get("mean_daily_net_bps", 0)}
        report.update(status="passed" if all(checks.values()) else "failed",
                      verdict={"sessions": [first[0]["session"], first[-1]["session"]], "model": model,
                               "random_baseline": baseline, "checks": checks})
    return report


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Prospective paper trial of the frozen cross-sectional ranker")
    parser.add_argument("action", choices=["start", "pick", "settle", "status"])
    parser.add_argument("--arm", choices=[*ARMS, "all"], default="all",
                        help="start needs one arm; the other actions default to every started arm")
    parser.add_argument("--start-session", type=date.fromisoformat, help="First session (default: next one)")
    parser.add_argument("--session", type=date.fromisoformat, help="settle: one session instead of all due")
    parser.add_argument("--no-refresh", action="store_true", help="Use stored candles; do not fetch from Kite")
    args = parser.parse_args(argv)
    refresh = not args.no_refresh
    arms = started_arms() if args.arm == "all" else [args.arm]
    try:
        if args.action == "start":
            if args.arm == "all":
                raise TrialError("start needs --arm rf or --arm hgb")
            result = start(args.start_session, arm=args.arm)
            result = {k: v for k, v in result.items() if k != "universe"}
        elif args.action == "pick":
            result = pick_all(refresh=refresh) if args.arm == "all" else pick(refresh=refresh, arm=args.arm)
        elif args.action == "settle" and args.session:
            result = {arm: settle(args.session, refresh=refresh and i == 0, arm=arm) for i, arm in enumerate(arms)}
        elif args.action == "settle":
            result = settle_all(refresh=refresh) if args.arm == "all" else settle_pending(refresh=refresh, arm=args.arm)
        else:
            result = {arm: status(arm) for arm in (arms or ["rf"])}
    except (TrialError, FileExistsError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
