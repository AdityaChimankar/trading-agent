"""Intraday strategy search with frozen selection, baselines and rolling walk-forward.

Candidates are entry family x parameters x bar size x exit policy (stop multiple,
reward/risk, maximum bars), scored with the shared conservative bracket replay:
next-open entry, stop-first ambiguous bars, slippage on both fills, forced 15:25
exit, no overlapping trades per symbol. Sessions are split chronologically for
all symbols into development (60%), validation (20%) and holdout (20%).

Two phases, in this order:

1. Selection sees development and validation only. A candidate is shortlisted
   only if, at conservative costs, it has enough validation trades, a
   session-clustered t-stat above a Bonferroni bar for the number of candidates
   tried, a session-bootstrap lower bound above zero, positive results in most
   symbols, and a validation mean above the always-long, always-short and
   random-entry baselines with the same exit policy. The shortlist is written to
   an immutable run file, with its SHA-256, before any holdout trade is replayed.
2. Holdout scoring runs only for that frozen shortlist and the baselines.

Rolling walk-forward (selection inside development + validation only): each fold
picks the best candidate on a trailing window and records the next window's
result, so the out-of-sample series tests the selection procedure, including
exit rules. It runs once over exit policies for the existing rule, and once over
every non-ML candidate. ML candidates are trained on development and are
therefore excluded from walk-forward.

Statistics treat a session as the unit of independence (daily sums across
symbols), not each trade. Research only: no orders, no model files, nothing
wired into the live rule.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import platform
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger
from trading_agent.core.signals import apply_ma_filter, evaluate_signals, ma_trend
from trading_agent.research.trade_outcomes import TradePolicy, outcome_arrays

logger = get_logger(__name__)

IST = "Asia/Kolkata"
DEFAULT_SYMBOLS = (
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "AXISBANK", "MARUTI", "BHARTIARTL",
    "KOTAKBANK", "SUNPHARMA", "TITAN", "ULTRACEMCO", "WIPRO", "NTPC", "POWERGRID", "ONGC", "TATASTEEL",
    "JSWSTEEL", "ADANIENT", "ASIANPAINT", "BAJAJFINSV", "COALINDIA", "DRREDDY", "EICHERMOT", "GRASIM",
    "HEROMOTOCO", "HINDALCO", "M&M", "TECHM",
)
BAR_MINUTES = (5, 15, 30)
SPLITS = (0.6, 0.8)  # development | validation | holdout boundaries by session
SELECTION_PERIODS = ("development", "validation")
ML_FEATURES = ("rsi", "adx", "atr_pct", "return_1", "return_3", "return_6", "volume_ratio",
               "dist_from_ma20", "ema_spread", "vwap_dist_atr", "minute_of_day", "pattern_code")
BASELINE_FAMILIES = ("always_long", "always_short", "random_entry")
RANDOM_ENTRY_RATE = 0.02
ML_MIN_TRAIN_SESSIONS = 60  # sessions before the first ML prediction
ML_RETRAIN_SESSIONS = 20    # refit cadence; each block is predicted by a model fitted on earlier sessions
ML_MIN_TRAIN_ROWS = 1000
SEED = 7


@dataclass(frozen=True)
class Costs:
    cost_bps: float
    slippage_bps: float  # per fill

    @property
    def round_trip_bps(self) -> float:
        return self.cost_bps + 2 * self.slippage_bps


# Selection costs: the conservative assumption used by earlier experiments (charge model unverified).
CONSERVATIVE = Costs(cost_bps=10.0, slippage_bps=2.0)


def size_aware_costs(notional: float, slippage_bps: float = 2.0) -> Costs:
    """Charges from execution/costs.py for a position of ``notional`` rupees (sensitivity only)."""
    from trading_agent.execution.costs import round_trip_charges

    price = 1000.0
    quantity = max(1, int(notional // price))
    charges = round_trip_charges("BUY", price, price, quantity)
    return Costs(cost_bps=round(charges / (price * quantity) * 1e4, 3), slippage_bps=slippage_bps)


@dataclass(frozen=True)
class SelectionRules:
    min_dev_trades: int = 300
    min_val_trades: int = 100
    min_symbol_share: float = 0.6     # share of symbols (with >= 5 val trades) whose val mean > 0
    family_wise_alpha: float = 0.05   # Bonferroni across every candidate tried
    bootstrap_draws: int = 2000


@dataclass(frozen=True)
class WalkForward:
    train_sessions: int = 60
    test_sessions: int = 20
    min_train_trades: int = 100


# --- data -------------------------------------------------------------------------------

def resample(minute: pd.DataFrame, bar_minutes: int) -> pd.DataFrame:
    """IST-aligned bars starting 09:15; keeps only bars inside the session with valid OHLC."""
    df = minute[["timestamp", "open", "high", "low", "close", "volume"]].drop_duplicates("timestamp")
    local = df.set_index(pd.to_datetime(df.timestamp, utc=True).dt.tz_convert(IST)).drop(columns="timestamp")
    origin = pd.Timestamp("2000-01-03 09:15", tz=IST)
    bars = local.resample(f"{bar_minutes}min", origin=origin, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    minutes = bars.index.hour * 60 + bars.index.minute
    bars = bars[(minutes >= 555) & (minutes < 930)]
    bars = bars[(bars.low <= bars[["open", "close"]].min(axis=1)) & (bars.high >= bars[["open", "close"]].max(axis=1))
                & (bars[["open", "high", "low", "close"]] > 0).all(axis=1)]
    out = bars.reset_index().rename(columns={"index": "timestamp"})
    out["timestamp"] = out["timestamp"].dt.tz_convert("UTC")
    return out


def prepare(bars: pd.DataFrame) -> pd.DataFrame:
    from trading_agent.strategy.research_features import prepare_research_frame

    f = prepare_research_frame(bars)
    local = f.timestamp.dt.tz_convert(IST)
    f["session"] = local.dt.date
    f["minute_of_day"] = local.dt.hour * 60 + local.dt.minute
    f["ema_spread"] = (f.ema9 - f.ema21) / f.close
    f["vwap_dist_atr"] = (f.close - f.vwap) / f.atr.replace(0, np.nan)
    return f


def fingerprint(minute: pd.DataFrame) -> dict:
    stamps = pd.to_datetime(minute.timestamp, utc=True)
    digest = hashlib.sha256()
    digest.update(stamps.astype("int64").to_numpy().tobytes())
    digest.update(minute.close.to_numpy(dtype=float).tobytes())
    return {"rows": int(len(minute)), "first": stamps.min().isoformat(), "last": stamps.max().isoformat(),
            "sha256": digest.hexdigest()}


# --- entry families -------------------------------------------------------------------------

def _actions(buy, sell) -> np.ndarray:
    a = np.zeros(len(buy), dtype=np.int8)
    a[np.asarray(buy, dtype=bool)] = 1
    a[np.asarray(sell, dtype=bool) & (a == 0)] = 2
    return a


def _first_per_session(mask: pd.Series, session: pd.Series) -> pd.Series:
    return mask & mask.astype(int).groupby(session).cumsum().eq(1)


def entry_families(bar_minutes: int) -> list[tuple[str, dict]]:
    fams: list[tuple[str, dict]] = [("rule", {}), ("rule_ma", {})]
    fams += [("trend_pullback", {"lo": lo, "hi": hi}) for lo, hi in ((35, 45), (40, 50))]
    fams += [("breakout", {"n": n, "adx": 20}) for n in (10, 20, 40)]
    fams += [("ema_cross", {"vwap": v}) for v in (True, False)]
    fams += [("orb", {"minutes": m}) for m in (15, 30, 60) if m % bar_minutes == 0]
    fams += [("vwap_revert", {"k": k}) for k in (1.5, 2.5)]
    fams += [("rsi_revert", {"lo": lo}) for lo in (20, 30)]
    fams += [("vwap_trend", {})]
    fams += [(name, {}) for name in BASELINE_FAMILIES]
    return fams


def family_actions(f: pd.DataFrame, family: str, p: dict) -> np.ndarray:
    """Actions for every bar; each family uses only the current and earlier bars."""
    if family == "always_long":
        return np.ones(len(f), dtype=np.int8)
    if family == "always_short":
        return np.full(len(f), 2, dtype=np.int8)
    if family == "random_entry":
        # Seeded per frame length so the baseline is reproducible; side is a coin flip.
        rng = np.random.default_rng(SEED + len(f))
        take = rng.random(len(f)) < RANDOM_ENTRY_RATE
        return np.where(take, rng.integers(1, 3, len(f)), 0).astype(np.int8)
    if family == "rule":
        return evaluate_signals(f.rsi.to_numpy(), f.adx.to_numpy(), f.pattern_code.to_numpy())
    if family == "rule_ma":
        rule = evaluate_signals(f.rsi.to_numpy(), f.adx.to_numpy(), f.pattern_code.to_numpy())
        return apply_ma_filter(rule, ma_trend(f.ema9.to_numpy(), f.ema21.to_numpy()))
    if family == "trend_pullback":
        return _actions((f.ma20 > f.ma50) & f.rsi.between(p["lo"], p["hi"]) & (f.adx >= 20),
                        (f.ma20 < f.ma50) & f.rsi.between(100 - p["hi"], 100 - p["lo"]) & (f.adx >= 20))
    if family == "breakout":
        return _actions((f.close > f.high.shift(1).rolling(p["n"]).max()) & (f.adx >= p["adx"]),
                        (f.close < f.low.shift(1).rolling(p["n"]).min()) & (f.adx >= p["adx"]))
    if family == "ema_cross":
        above = f.ema9 > f.ema21
        up, down = above & ~above.shift(1, fill_value=True), ~above & above.shift(1, fill_value=False)
        if p["vwap"]:
            up, down = up & (f.close > f.vwap), down & (f.close < f.vwap)
        return _actions(up & (f.adx >= 20), down & (f.adx >= 20))
    if family == "orb":
        minute = f.minute_of_day - 555
        window = (minute >= 0) & (minute < p["minutes"])
        has_open = minute.groupby(f.session).transform("min").eq(0)
        # The range is used only by bars after the window, so it is fully in the past.
        hi = f.high.where(window).groupby(f.session).transform("max")
        lo = f.low.where(window).groupby(f.session).transform("min")
        after = has_open & (minute >= p["minutes"])
        return _actions(_first_per_session(after & (f.close > hi), f.session),
                        _first_per_session(after & (f.close < lo), f.session))
    if family == "vwap_revert":
        d = f.vwap_dist_atr
        return _actions((d < -p["k"]) & (f.rsi < 35), (d > p["k"]) & (f.rsi > 65))
    if family == "rsi_revert":
        return _actions(f.rsi < p["lo"], f.rsi > 100 - p["lo"])
    if family == "vwap_trend":
        return _actions((f.close > f.vwap) & (f.ema9 > f.ema21) & (f.low <= f.ema21) & (f.close > f.ema21),
                        (f.close < f.vwap) & (f.ema9 < f.ema21) & (f.high >= f.ema21) & (f.close < f.ema21))
    raise ValueError(family)


def exit_policies(bar_minutes: int, costs: Costs) -> list[TradePolicy]:
    horizons = sorted({max(1, m // bar_minutes) for m in (30, 60, 120, 240)})
    return [TradePolicy(horizon=h, stop_atr=s, reward_risk=r, cost_bps=costs.cost_bps,
                        slippage_bps=costs.slippage_bps)
            for h, s, r in itertools.product(horizons, (1.0, 1.5, 2.0), (1.0, 1.5, 2.0, 3.0))]


def policy_dict(policy: TradePolicy) -> dict:
    return {"horizon_bars": policy.horizon, "stop_atr": policy.stop_atr, "reward_risk": policy.reward_risk}


# --- replay and accumulation ------------------------------------------------------------------

def non_overlapping(actions: np.ndarray, net_buy, exit_buy, net_sell, exit_sell, allowed: np.ndarray) -> list[tuple[int, float]]:
    """(signal index, net return) for trades that start after the previous one exits."""
    trades, next_free = [], -1
    for i in np.flatnonzero((actions > 0) & allowed):
        if i <= next_free:
            continue
        net, exit_ = (net_buy[i], exit_buy[i]) if actions[i] == 1 else (net_sell[i], exit_sell[i])
        if np.isnan(net):
            continue
        trades.append((int(i), float(net)))
        next_free = int(exit_)
    return trades


@dataclass
class Ledger:
    """Per-candidate sums by session and by symbol, so statistics can cluster by day."""

    n_sessions: int
    n_symbols: int
    by_session: np.ndarray = field(init=False)   # [session, (sum, count, wins)]
    by_symbol: np.ndarray = field(init=False)    # [symbol, (sum, count)]

    def __post_init__(self):
        self.by_session = np.zeros((self.n_sessions, 3))
        self.by_symbol = np.zeros((self.n_symbols, 2))

    def add(self, symbol_index: int, session_index: np.ndarray, returns: np.ndarray) -> None:
        np.add.at(self.by_session[:, 0], session_index, returns)
        np.add.at(self.by_session[:, 1], session_index, 1)
        np.add.at(self.by_session[:, 2], session_index, returns > 0)
        self.by_symbol[symbol_index] += (returns.sum(), len(returns))


def summarize(ledger: Ledger, sessions: np.ndarray, rules: SelectionRules | None = None,
              symbols: Ledger | None = None) -> dict:
    """Stats over the given session indices; the t-stat and bootstrap use daily sums."""
    s = ledger.by_session[sessions]
    total, count, wins = s[:, 0].sum(), s[:, 1].sum(), s[:, 2].sum()
    if count == 0:
        return {"trades": 0, "sessions": int(len(sessions))}
    daily = s[:, 0]
    std = float(daily.std(ddof=1)) if len(daily) > 1 else 0.0
    out = {"trades": int(count), "sessions": int(len(sessions)),
           "trades_per_session": round(float(count / len(sessions)), 3),
           "win_rate_pct": round(float(wins / count * 100), 2),
           "mean_net_bps": round(float(total / count * 1e4), 3),
           "clustered_t": round(float(daily.mean() / (std / math.sqrt(len(daily)))), 3) if std > 0 else None}
    if rules is not None:
        rng = np.random.default_rng(SEED)
        picks = rng.integers(0, len(s), (rules.bootstrap_draws, len(s)))
        sums, counts = s[:, 0][picks].sum(axis=1), s[:, 1][picks].sum(axis=1)
        means = np.divide(sums, counts, out=np.full(len(sums), np.nan), where=counts > 0)
        out["bootstrap_mean_bps_95"] = [round(float(np.nanpercentile(means, q) * 1e4), 3) for q in (2.5, 97.5)]
    return out


def symbol_share(ledger: Ledger, min_trades: int = 5) -> float | None:
    sums, counts = ledger.by_symbol[:, 0], ledger.by_symbol[:, 1]
    active = counts >= min_trades
    return round(float((sums[active] > 0).mean()), 3) if active.any() else None


@dataclass
class Universe:
    frames: dict            # (symbol, bar_minutes) -> prepared frame
    sessions: list          # ordered session dates
    split_dates: tuple      # (validation_start, holdout_start)
    fingerprints: dict
    skipped: dict

    def period_sessions(self, period: str) -> np.ndarray:
        v, h = self.split_dates
        idx = np.arange(len(self.sessions))
        dates = np.array(self.sessions)
        return {"development": idx[dates < v], "validation": idx[(dates >= v) & (dates < h)],
                "holdout": idx[dates >= h]}[period]


def load_universe(symbols, store=None, bar_minutes=BAR_MINUTES) -> Universe:
    from trading_agent.storage.parquet_store import get_parquet_store

    store = store or get_parquet_store()
    frames, all_sessions, prints, skipped = {}, set(), {}, {}
    for symbol in symbols:
        try:
            minute = store.read_candles(symbol, "minute")
        except Exception as error:
            skipped[symbol] = f"unreadable: {error}"
            continue
        if minute.empty:
            skipped[symbol] = "no minute candles"
            continue
        prints[symbol] = fingerprint(minute)
        for bm in bar_minutes:
            bars = resample(minute, bm)
            if len(bars) < 200:
                skipped[f"{symbol}@{bm}m"] = f"only {len(bars)} valid bars"
                continue
            f = prepare(bars)
            frames[(symbol, bm)] = f
            all_sessions.update(f.session.unique())
    ordered = sorted(all_sessions)
    if len(ordered) < 20:
        raise ValueError("Not enough stored sessions for a three-way split")
    split = (ordered[int(len(ordered) * SPLITS[0])], ordered[int(len(ordered) * SPLITS[1])])
    return Universe(frames, ordered, split, prints, skipped)


def candidate_key(family: str, params: dict, bar_minutes: int, policy: dict) -> str:
    return json.dumps([family, params, bar_minutes, policy], sort_keys=True)


def _period_mask(f: pd.DataFrame, universe: Universe, periods) -> np.ndarray:
    v, h = universe.split_dates
    masks = {"development": f.session < v, "validation": (f.session >= v) & (f.session < h),
             "holdout": f.session >= h}
    out = np.zeros(len(f), dtype=bool)
    for period in periods:
        out |= masks[period].to_numpy()
    return out


def score_rules(universe: Universe, costs: Costs, periods=SELECTION_PERIODS, only: set | None = None) -> dict:
    """key -> Ledger for every (or only the listed) rule candidate, trades restricted to ``periods``."""
    session_index = {d: i for i, d in enumerate(universe.sessions)}
    symbols = sorted({s for s, _ in universe.frames})
    symbol_index = {s: i for i, s in enumerate(symbols)}
    ledgers: dict[str, Ledger] = {}
    for bm in BAR_MINUTES:
        frames = [(s, f) for (s, b), f in universe.frames.items() if b == bm]
        prepared = []
        for s, f in frames:
            prepared.append((s, f, _period_mask(f, universe, periods),
                             f.session.map(session_index).to_numpy(),
                             {(fam, json.dumps(p, sort_keys=True)): family_actions(f, fam, p)
                              for fam, p in entry_families(bm)}))
        for policy in exit_policies(bm, costs):
            pdict = policy_dict(policy)
            wanted = list(prepared[0][4]) if prepared else []
            if only is not None:
                wanted = [k for k in wanted if candidate_key(k[0], json.loads(k[1]), bm, pdict) in only]
                if not wanted:
                    continue
            for s, f, mask, sess, acts in prepared:
                nb, eb = outcome_arrays(f, "BUY", policy, bm * 60)
                ns, es = outcome_arrays(f, "SELL", policy, bm * 60)
                for fam, params in wanted:
                    key = candidate_key(fam, json.loads(params), bm, pdict)
                    ledger = ledgers.setdefault(key, Ledger(len(universe.sessions), len(symbols)))
                    trades = non_overlapping(acts[(fam, params)], nb, eb, ns, es, mask)
                    if trades:
                        idx = np.array([i for i, _ in trades])
                        ledger.add(symbol_index[s], sess[idx], np.array([r for _, r in trades]))
        logger.info("strategy_search_scored", bar_minutes=bm, candidates=len(ledgers))
    return ledgers


# --- ML ---------------------------------------------------------------------------------------

def ml_blocks(universe: Universe) -> list[tuple[str, np.ndarray]]:
    """(period, session indices) blocks the ML scores, each predicted by a model fitted on earlier sessions.

    Blocks hold at most ``ML_RETRAIN_SESSIONS`` sessions and never straddle a period
    boundary; the first ``ML_MIN_TRAIN_SESSIONS`` sessions are training-only.
    """
    blocks = []
    for period in ("development", "validation", "holdout"):
        idx = universe.period_sessions(period)
        idx = idx[idx >= ML_MIN_TRAIN_SESSIONS]
        blocks += [(period, idx[i:i + ML_RETRAIN_SESSIONS]) for i in range(0, len(idx), ML_RETRAIN_SESSIONS)]
    return blocks


def ml_folds(session: np.ndarray, blocks) -> list[tuple[np.ndarray, np.ndarray]]:
    """(train rows, scored rows) per block: training rows are strictly earlier sessions (expanding window).

    Trades never cross a session, so no label of a training row reaches into its block.
    """
    return [(session < block[0], np.isin(session, block)) for _, block in blocks]


def score_ml(universe: Universe, costs: Costs) -> tuple[dict, dict]:
    """Out-of-sample ML ledgers for development/validation, plus deferred holdout scorers.

    A model fitted on the development period and scored on it reports its own
    training labels: in the 2026-10-24 run that showed a 68% development win rate
    against 38% on validation, from a ~30% base rate. Every scored session here is
    therefore predicted walk-forward (see ``ml_blocks``), so development, validation
    and holdout ML results are all out of sample and comparable with the rules.
    Holdout models are fitted only inside the deferred callables, after freezing.
    """
    from sklearn.ensemble import HistGradientBoostingClassifier

    session_index = {d: i for i, d in enumerate(universe.sessions)}
    symbols = sorted({s for s, _ in universe.frames})
    blocks = ml_blocks(universe)
    ledgers, deferred = {}, {}
    for bm in BAR_MINUTES:
        frames = [(s, f) for (s, b), f in universe.frames.items() if b == bm]
        if not frames:
            continue
        for h, rr in itertools.product(sorted({max(1, m // bm) for m in (60, 120)}), (1.0, 1.5, 2.0)):
            policy = TradePolicy(horizon=h, stop_atr=1.5, reward_risk=rr, cost_bps=costs.cost_bps,
                                 slippage_bps=costs.slippage_bps)
            parts = []
            for s, f in frames:
                nb, eb = outcome_arrays(f, "BUY", policy, bm * 60)
                ns, es = outcome_arrays(f, "SELL", policy, bm * 60)
                parts.append((s, f, nb, eb, ns, es, f.session.map(session_index).to_numpy()))
            x = np.vstack([f[list(ML_FEATURES)].to_numpy(dtype=float) for _, f, *_ in parts])
            ok = np.isfinite(x).all(axis=1)
            clean = np.nan_to_num(x)
            session = np.concatenate([p[6] for p in parts])
            net = {"BUY": np.concatenate([p[2] for p in parts]), "SELL": np.concatenate([p[4] for p in parts])}
            bounds = np.cumsum([0] + [len(p[1]) for p in parts])

            def predict(period_filter, x=x, ok=ok, clean=clean, session=session, net=net):
                """BUY/SELL probabilities for the blocks of the given periods; zero elsewhere."""
                prob = {"BUY": np.zeros(len(x)), "SELL": np.zeros(len(x))}
                chosen = [b for b in blocks if b[0] in period_filter]
                for train, rows in ml_folds(session, chosen):
                    fitted = {}
                    for side in ("BUY", "SELL"):
                        m = train & ok & np.isfinite(net[side])
                        y = net[side][m] > 0
                        if len(y) < ML_MIN_TRAIN_ROWS or y.mean() in (0.0, 1.0):
                            break
                        fitted[side] = HistGradientBoostingClassifier(
                            max_iter=200, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=200,
                            l2_regularization=1.0, random_state=SEED).fit(x[m], y)
                    if len(fitted) < 2:
                        continue
                    for side in ("BUY", "SELL"):
                        prob[side][rows] = np.where(ok[rows], fitted[side].predict_proba(clean[rows])[:, 1], 0)
                return prob

            def replay(prob, periods, threshold, parts=parts, bounds=bounds):
                ledger = Ledger(len(universe.sessions), len(symbols))
                for k, (s, f, nb, eb, ns, es, sess) in enumerate(parts):
                    pb, ps = prob["BUY"][bounds[k]:bounds[k + 1]], prob["SELL"][bounds[k]:bounds[k + 1]]
                    acts = _actions((pb >= threshold) & (pb >= ps), (ps >= threshold) & (ps > pb))
                    trades = non_overlapping(acts, nb, eb, ns, es, _period_mask(f, universe, periods))
                    if trades:
                        idx = np.array([i for i, _ in trades])
                        ledger.add(symbols.index(s), sess[idx], np.array([r for _, r in trades]))
                return ledger

            selection_prob = predict(SELECTION_PERIODS)
            holdout_prob: dict = {}

            def holdout(threshold, predict=predict, replay=replay, cache=holdout_prob):
                if not cache:
                    cache.update(predict(("holdout",)))
                return replay(cache, ("holdout",), threshold)

            for threshold in (0.5, 0.55, 0.6, 0.65, 0.7, 0.75):
                key = candidate_key("ml_gbm", {"threshold": threshold}, bm, policy_dict(policy))
                ledgers[key] = replay(selection_prob, SELECTION_PERIODS, threshold)
                deferred[key] = lambda threshold=threshold, holdout=holdout: holdout(threshold)
        logger.info("strategy_search_ml_scored", bar_minutes=bm)
    return ledgers, deferred


# --- selection ------------------------------------------------------------------------------

def bonferroni_t(candidates: int, alpha: float) -> float:
    from scipy.stats import norm

    return float(norm.ppf(1 - alpha / max(1, candidates)))


def describe(key: str) -> dict:
    family, params, bar_minutes, policy = json.loads(key)
    return {"family": family, "params": params, "bar_minutes": bar_minutes, "policy": policy}


def select(ledgers: dict, universe: Universe, rules: SelectionRules = SelectionRules()) -> dict:
    """Development + validation only. Returns per-candidate stats and the shortlist."""
    dev, val = universe.period_sessions("development"), universe.period_sessions("validation")
    tried = [k for k in ledgers if describe(k)["family"] not in BASELINE_FAMILIES]
    t_bar = bonferroni_t(len(tried), rules.family_wise_alpha)
    rows, baselines = {}, {}
    for key, ledger in ledgers.items():
        d = describe(key)
        # Symbol sums span development + validation: a stability check, not a fit.
        row = {**d, "development": summarize(ledger, dev), "validation": summarize(ledger, val, rules),
               "symbol_share_positive": symbol_share(ledger)}
        rows[key] = row
        if d["family"] in BASELINE_FAMILIES:
            group = candidate_key("_", {}, d["bar_minutes"], d["policy"])
            baselines.setdefault(group, {})[d["family"]] = row["validation"].get("mean_net_bps")

    def checks(row):
        group = candidate_key("_", {}, row["bar_minutes"], row["policy"])
        best_baseline = max((v for v in baselines.get(group, {}).values() if v is not None), default=None)
        v, dv = row["validation"], row["development"]
        result = {
            "development_trades": dv.get("trades", 0) >= rules.min_dev_trades,
            "development_positive": dv.get("mean_net_bps", -1) > 0,
            "validation_trades": v.get("trades", 0) >= rules.min_val_trades,
            "clustered_t_above_bonferroni": (v.get("clustered_t") or 0) >= t_bar,
            "bootstrap_lower_above_zero": (v.get("bootstrap_mean_bps_95") or [-1])[0] > 0,
            "positive_in_most_symbols": (row["symbol_share_positive"] or 0) >= rules.min_symbol_share,
            "beats_baselines": best_baseline is not None and v.get("mean_net_bps", -1e9) > best_baseline,
        }
        return result

    shortlist = []
    for key, row in rows.items():
        if row["family"] in BASELINE_FAMILIES:
            continue
        row["checks"] = checks(row)
        if all(row["checks"].values()):
            shortlist.append(key)
    nominal = sorted((k for k, r in rows.items() if r["family"] not in BASELINE_FAMILIES
                      and r["development"].get("mean_net_bps", -1) > 0 and r["validation"].get("mean_net_bps", -1) > 0
                      and r["validation"].get("trades", 0) >= rules.min_val_trades),
                     key=lambda k: -rows[k]["validation"]["mean_net_bps"])
    return {"rows": rows, "shortlist": sorted(shortlist, key=lambda k: -rows[k]["validation"]["mean_net_bps"]),
            "nominal_positive": nominal, "candidates_tried": len(tried), "bonferroni_t": round(t_bar, 3)}


def walk_forward(ledgers: dict, universe: Universe, keys: list[str], wf: WalkForward = WalkForward()) -> dict:
    """Pick the best candidate on each trailing window; chain the following windows' results."""
    pool = np.concatenate([universe.period_sessions(p) for p in SELECTION_PERIODS])
    folds, oos = [], np.zeros((len(universe.sessions), 3))
    start = wf.train_sessions
    while start < len(pool):
        train, test = pool[start - wf.train_sessions:start], pool[start:start + wf.test_sessions]
        if len(test) < max(1, wf.test_sessions // 2):
            break  # a stub fold of a session or two says nothing
        best, best_mean = None, -np.inf
        for key in keys:
            s = ledgers[key].by_session[train]
            if s[:, 1].sum() >= wf.min_train_trades and s[:, 0].sum() / s[:, 1].sum() > best_mean:
                best, best_mean = key, s[:, 0].sum() / s[:, 1].sum()
        if best is not None:
            oos[test] += ledgers[best].by_session[test]
            test_stats = summarize(ledgers[best], test)
            folds.append({"train_start": str(universe.sessions[train[0]]), "test_start": str(universe.sessions[test[0]]),
                          "test_end": str(universe.sessions[test[-1]]), "chosen": describe(best),
                          "train_mean_bps": round(best_mean * 1e4, 3), "test": test_stats,
                          "_test": test.tolist()})
        start += wf.test_sessions
    chained = Ledger(len(universe.sessions), 1)
    chained.by_session = oos
    tested = np.concatenate([np.array(f["_test"]) for f in folds]) if folds else np.array([], dtype=int)
    for f in folds:
        f.pop("_test")
    return {"folds": folds, "out_of_sample": summarize(chained, tested, SelectionRules()),
            "candidates_per_fold": len(keys), "config": asdict(wf)}


# --- provenance and persistence -------------------------------------------------------------

def git_revision() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent, capture_output=True,
                              text=True, timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def versions() -> dict:
    import scipy
    import sklearn

    return {"python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__,
            "scikit_learn": sklearn.__version__, "scipy": scipy.__version__}


def _write_atomic(path: Path, payload: dict, exclusive: bool = False) -> str:
    text = json.dumps(payload, default=str, indent=1, sort_keys=True)
    if exclusive and path.exists():
        raise FileExistsError(f"{path} already exists; runs are immutable")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{uuid.uuid4().hex[:8]}.tmp")  # short: Windows paths cap at 260 characters
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def runs_dir() -> Path:
    return get_settings().data_dir / "research" / "strategy_search" / "runs"


def run(symbols=DEFAULT_SYMBOLS, include_ml: bool = True, store=None,
        rules: SelectionRules = SelectionRules(), wf: WalkForward = WalkForward()) -> dict:
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    directory = runs_dir() / run_id
    universe = load_universe(list(symbols), store)
    meta = {"run_id": run_id, "started_at": datetime.now(UTC).isoformat(), "git_revision": git_revision(),
            "versions": versions(), "symbols": sorted({s for s, _ in universe.frames}),
            "skipped": universe.skipped, "data_fingerprints": universe.fingerprints,
            "bar_minutes": list(BAR_MINUTES), "splits": list(SPLITS),
            "split_dates": {"validation_start": str(universe.split_dates[0]),
                            "holdout_start": str(universe.split_dates[1])},
            "sessions": {p: int(len(universe.period_sessions(p))) for p in ("development", "validation", "holdout")},
            "selection_costs": asdict(CONSERVATIVE), "selection_rules": asdict(rules)}

    # Phase 1: development + validation only.
    ledgers = score_rules(universe, CONSERVATIVE)
    deferred = {}
    if include_ml:
        ml_ledgers, deferred = score_ml(universe, CONSERVATIVE)
        ledgers.update(ml_ledgers)
    selection = select(ledgers, universe, rules)
    # ML ledgers are walk-forward predictions, so ML joins the candidate pool on equal terms.
    candidates = [k for k in ledgers if describe(k)["family"] not in BASELINE_FAMILIES]
    rule_exits = [k for k in candidates if describe(k)["family"] == "rule"]
    walk = {"rule_exit_policies": walk_forward(ledgers, universe, rule_exits, wf),
            "all_candidates": walk_forward(ledgers, universe, candidates, wf)}
    frozen = {**meta, "phase": "selection_frozen", "shortlist": selection["shortlist"],
              "candidates_tried": selection["candidates_tried"], "bonferroni_t": selection["bonferroni_t"]}
    frozen_sha = _write_atomic(directory / "selection.json", frozen, exclusive=True)

    # Phase 2: holdout only for the frozen shortlist and the matching baselines.
    holdout_keys = set(selection["shortlist"])
    for key in selection["shortlist"]:
        d = describe(key)
        holdout_keys |= {candidate_key(b, {}, d["bar_minutes"], d["policy"]) for b in BASELINE_FAMILIES}
    rule_keys = {k for k in holdout_keys if describe(k)["family"] != "ml_gbm"}
    holdout_ledgers = score_rules(universe, CONSERVATIVE, periods=("holdout",), only=rule_keys) if rule_keys else {}
    for key in holdout_keys - rule_keys:
        holdout_ledgers[key] = deferred[key]()
    holdout_idx = universe.period_sessions("holdout")
    sensitivity = size_aware_costs(200_000)
    holdout = {key: {**describe(key), "holdout": summarize(ledger, holdout_idx, rules)}
               for key, ledger in holdout_ledgers.items()}

    rows = selection["rows"]
    best_family = {}
    for key, row in rows.items():
        v = row["validation"]
        if row["family"] in BASELINE_FAMILIES or v.get("trades", 0) < rules.min_val_trades:
            continue
        if row["family"] not in best_family or v["mean_net_bps"] > rows[best_family[row["family"]]]["validation"]["mean_net_bps"]:
            best_family[row["family"]] = key
    report = {
        **meta, "phase": "complete", "selection_sha256": frozen_sha,
        "scope": "research_only", "candidates_tried": selection["candidates_tried"],
        "bonferroni_t": selection["bonferroni_t"], "shortlist": [rows[k] for k in selection["shortlist"]],
        "nominal_positive_count": len(selection["nominal_positive"]),
        "nominal_positive_top": [rows[k] for k in selection["nominal_positive"][:10]],
        "best_per_family_validation": sorted((rows[k] for k in best_family.values()),
                                             key=lambda r: -r["validation"]["mean_net_bps"]),
        "baselines_validation": [rows[k] for k in rows if rows[k]["family"] in BASELINE_FAMILIES
                                 and rows[k]["policy"] == {"horizon_bars": max(1, 120 // rows[k]["bar_minutes"]),
                                                           "stop_atr": 1.5, "reward_risk": 2.0}],
        "walk_forward": walk, "holdout": list(holdout.values()),
        "cost_sensitivity": {"size_aware_at_inr_2_lakh": asdict(sensitivity),
                             "note": (f"Size-aware round trip is {sensitivity.round_trip_bps:.1f} bps vs "
                                      f"{CONSERVATIVE.round_trip_bps:.1f} bps used for selection; every trade "
                                      "mean shifts by the difference.")},
        "notes": ("Holdout is scored only for the frozen shortlist and its baselines. Results from earlier "
                  "searches over the same dates were inspected, so this holdout window is not pristine; "
                  "only prospective sessions are."),
    }
    sha = _write_atomic(directory / "report.json", report, exclusive=True)
    _write_atomic(runs_dir().parent / "latest.json", {"run_id": run_id, "report": str(directory / "report.json"),
                                                      "report_sha256": sha})
    report["path"] = str(directory / "report.json")
    return report


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Frozen-selection intraday strategy search (research only)")
    parser.add_argument("symbols", nargs="*")
    parser.add_argument("--no-ml", action="store_true")
    args = parser.parse_args(argv)
    report = run([s.upper() for s in args.symbols] or DEFAULT_SYMBOLS, include_ml=not args.no_ml)
    print(json.dumps({k: report[k] for k in ("run_id", "symbols", "split_dates", "sessions", "candidates_tried",
                                             "bonferroni_t", "path")}, indent=1))
    print("shortlist:", len(report["shortlist"]), "| nominal positive:", report["nominal_positive_count"])


if __name__ == "__main__":
    main()
