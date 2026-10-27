"""Trade replay: how the rest of today could treat one bracket (after the day-trader-skill
pack's trader-timemachine).

Before a plan is sized, ask: if the rest of today behaves like this stock's recent
sessions, how often does the bracket hit its target first, its stop first, or
neither by the 15:15 MIS exit?

Bars. Stored candles become fixed, non-overlapping bars anchored at 09:15 IST
(open first, high max, low min, close last): 1-minute bars when the 1-minute
history is as complete as the 5-minute one, so exits are checked minute by minute,
otherwise 5-minute bars. A session counts when it has its 09:15 bar and at most 2%
of its bars missing; a missing bar had no trade and is filled flat at the previous
close. Only sessions before the entry date are used.

Simulation (filtered historical simulation with a stationary block bootstrap):

1. Each past bar becomes log open/high/low/close returns over the previous close,
   divided by that session's volatility and by the time-of-day volatility profile.
2. Today's volatility comes from today's bars before the entry, blended with the
   median of the last 10 sessions (one hour of bars' weight), so an early entry
   leans on the recent regime.
3. A replay walks bar by bar to 15:15. It copies bars from one past session at the
   same time of day (plus or minus ``window_minutes``) and keeps copying that
   session's next bars, switching to a new random session with probability 1/L,
   rescaled to today's volatility. The mean block length L is chosen from the data
   (Politis and White 2004, on the returns and on their absolute values, the longer
   of the two), so trends and volatility bursts inside a block survive.

Exits follow research/trade_outcomes.py: a bar that opens through the stop exits at
that open, one that opens through the target exits at the target, a bar touching
both counts as the stop, and anything still open exits at the last close before
15:15. R is the signed move over the risk; net R also subtracts ``cost_bps``.

Conclusion trigger. 10,000 replays make Monte Carlo noise small; the pool of past
sessions is the real uncertainty. The simulation is rerun on 200 resamples of the
pool to get a 90% range of net R and of the target-first share. The result is
``conclusive_negative`` or ``conclusive_positive`` only when the pool has 40+
sessions, both ranges clear zero and break-even on the same side, and three checks
agree in sign: single-bar draws (no blocks), unscaled bars (no volatility filter) and
the same bracket on the real sessions. Otherwise ``inconclusive``. A positive result
is a recent base rate for this bracket at this time of day, not evidence that the
signal has an edge. Research only; nothing here sizes, records or places an order.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd

IST = "Asia/Kolkata"
OPEN_MINUTE = 9 * 60 + 15
EXIT_MINUTE = 15 * 60 + 15
SESSION_MINUTES = EXIT_MINUTE - OPEN_MINUTE  # 360
MAX_MISSING = 0.02
MIN_SESSIONS, CONCLUSIVE_SESSIONS = 20, 40
CI_GROUPS, CI_REPLAYS = 200, 250
MAX_BLOCK_MINUTES = 60
RECENT_SESSIONS = 10
COIN_FLIP, STRONG, MANY_NEITHER = 0.05, 0.15, 0.40
BAND_QUANTILES = (5, 25, 50, 75, 95)
LIMITATIONS = ("Simulated from this stock's own recent bars, rescaled to today's volatility and drawn in "
               "random-length blocks at the same time of day: no news, earnings or halts, and dependence longer "
               "than a block is lost. Fills are bar by bar, never tick by tick. Not a win probability, not a "
               "prediction and not evidence of an edge.")


@dataclass(frozen=True)
class ReplayConfig:
    replays: int = 10_000
    seed: int = 19
    sessions: int = 60
    window_minutes: int = 10
    cost_bps: float = 14.0  # 10 bps charges + 2 bps slippage per fill, as decision outcomes
    bar_minutes: int | None = None  # None: 1 when the 1-minute history is complete enough, else 5

    def __post_init__(self):
        if (not 100 <= self.replays <= 100_000 or not MIN_SESSIONS <= self.sessions <= 1000
                or not 0 <= self.window_minutes <= 30 or not 0 <= self.cost_bps <= 100
                or self.bar_minutes not in (None, 1, 5)):
            raise ValueError("replays 100-100000, sessions 20-1000, window_minutes 0-30, cost_bps 0-100, "
                             "bar_minutes 1 or 5")


@dataclass(frozen=True)
class Plan:
    symbol: str
    side: str
    entry: float
    stop: float
    target: float
    entry_time: datetime  # tz-aware

    def __post_init__(self):
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        values = (self.entry, self.stop, self.target)
        if not all(math.isfinite(v) and v > 0 for v in values):
            raise ValueError("entry, stop and target must be positive prices")
        if self.sign * (self.entry - self.stop) <= 0:
            raise ValueError("Stop must be on the loss side of the entry")
        if self.sign * (self.target - self.entry) <= 0:
            raise ValueError("Target must be on the profit side of the entry")
        if self.entry_time.tzinfo is None:
            raise ValueError("entry_time must be timezone-aware")

    @property
    def sign(self) -> int:
        return 1 if self.side == "BUY" else -1

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def r_target(self) -> float:
        return abs(self.target - self.entry) / self.risk

    def cost_r(self, cost_bps: float) -> float:
        return cost_bps / 10000 * self.entry / self.risk

    def break_even(self, cost_bps: float) -> float:
        """Target-first share of decided trades that breaks even: p (R - c) = (1 - p)(1 + c)."""
        return min(1.0, (1 + self.cost_r(cost_bps)) / (1 + self.r_target))


# --- bars ---------------------------------------------------------------------------

def _grouped(frame: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    local = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(IST)
    minute = local.dt.hour * 60 + local.dt.minute
    keep = (local.dt.weekday < 5) & (minute >= OPEN_MINUTE) & (minute < EXIT_MINUTE)
    bars = frame.loc[keep, ["open", "high", "low", "close"]].astype(float).assign(
        day=local[keep].dt.date, slot=(minute[keep] - OPEN_MINUTE) // minutes, stamp=local[keep])
    return bars.sort_values("stamp").groupby(["day", "slot"], sort=True).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))


def _valid(values: np.ndarray) -> bool:
    return bool(np.isfinite(values).all() and (values > 0).all()
                and not ((values[:, 1] < values.max(axis=1)) | (values[:, 2] > values.min(axis=1))).any())


def session_bars(frame: pd.DataFrame, minutes: int = 5) -> dict[date, np.ndarray]:
    """{IST date: (360 / minutes, 4) open/high/low/close} for usable 09:15-15:15 sessions.

    Fixed bars anchored at 09:15. A session needs its 09:15 bar and at most 2% of its
    bars missing; a missing bar (no trade) is filled flat at the previous close.
    """
    slots = SESSION_MINUTES // minutes
    allowed = int(slots * MAX_MISSING)
    sessions = {}
    for day, rows in _grouped(frame, minutes).groupby(level="day"):
        present = rows.index.get_level_values("slot")
        if present[0] != 0 or slots - len(rows) > allowed:
            continue
        full = rows.droplevel("day").reindex(range(slots))
        close = full["close"].ffill()
        for column in ("open", "high", "low"):
            full[column] = full[column].fillna(close)
        full["close"] = close
        values = full[["open", "high", "low", "close"]].to_numpy()
        if _valid(values):
            sessions[day] = values
    return sessions


def first_slot(entry_time: datetime, minutes: int = 5) -> int:
    """Index of the first bar starting at or after the entry (never the 09:15 bar)."""
    local = pd.Timestamp(entry_time).tz_convert(IST)
    if local.weekday() >= 5:
        raise ValueError("Entry time falls on a weekend")
    seconds = (local.hour * 60 + local.minute - OPEN_MINUTE) * 60 + local.second + local.microsecond / 1e6
    if seconds < 0:
        raise ValueError("Entry time is before the 09:15 open")
    slot = max(1, math.ceil(seconds / (minutes * 60)))
    if slot >= SESSION_MINUTES // minutes:
        raise ValueError(f"No bar left before the 15:15 exit for an entry at {local:%Y-%m-%d %H:%M} IST; "
                         "replay during the session or give an earlier entry time")
    return slot


# --- volatility and blocks ------------------------------------------------------------

def log_returns(sessions: np.ndarray) -> np.ndarray:
    """(S, T, 4) log open/high/low/close over the previous bar's close; bar 0 is NaN."""
    out = np.full(sessions.shape, np.nan)
    out[:, 1:, :] = np.log(sessions[:, 1:, :] / sessions[:, :-1, 3:4])
    return out


def vol_profile(returns: np.ndarray, minutes: int) -> np.ndarray:
    """Time-of-day volatility: RMS close return per bar, smoothed over +-15 minutes."""
    squared = np.nanmean(returns[:, 1:, 3] ** 2, axis=0)
    half = max(1, 15 // minutes)
    padded = np.pad(squared, half, mode="edge")
    smooth = np.convolve(padded, np.ones(2 * half + 1) / (2 * half + 1), mode="valid")
    profile = np.sqrt(np.maximum(smooth, 1e-12))
    profile = np.maximum(profile, 0.05 * np.median(profile))
    return np.concatenate([[profile[0]], profile])


def day_vols(returns: np.ndarray, profile: np.ndarray) -> np.ndarray:
    """Each session's volatility in units of the time-of-day profile."""
    scaled = returns[:, 1:, 3] / profile[None, 1:]
    return np.maximum(np.sqrt(np.mean(scaled ** 2, axis=1)), 1e-6)


def today_vol(frame: pd.DataFrame, entry_day: date, slot: int, minutes: int, profile: np.ndarray,
              recent: float) -> dict:
    """Today's volatility from closed bars before the entry, shrunk toward the recent median."""
    rows = _grouped(frame, minutes)
    closes = {}
    if entry_day in rows.index.get_level_values("day"):
        today = rows.xs(entry_day, level="day")["close"]
        closes = {int(s): float(c) for s, c in today.items() if s < slot}
    scaled = [math.log(closes[s] / closes[s - 1]) / profile[s] for s in range(1, slot)
              if s in closes and s - 1 in closes and closes[s] > 0 and closes[s - 1] > 0]
    n, prior = len(scaled), max(60 // minutes, 1)
    raw = math.sqrt(sum(u * u for u in scaled) / n) if n else None
    used = math.sqrt(((raw or 0) ** 2 * n + recent ** 2 * prior) / (n + prior))
    return {"today": raw, "today_bars": n, "recent": recent, "used": used}


def optimal_block(x: np.ndarray, max_block: int) -> float:
    """Politis-White (2004, corrected 2009) mean block length for the stationary bootstrap.

    ``x`` is (sessions, bars); autocovariances are pooled within sessions only.
    """
    x = x - x.mean()
    n = x.size
    max_lag = min(x.shape[1] - 1, math.ceil(math.sqrt(n)) + 10)
    acov = np.array([np.sum(x[:, : x.shape[1] - k] * x[:, k:]) / n for k in range(max_lag + 1)])
    if acov[0] <= 0:
        return 1.0
    rho = acov / acov[0]
    kn = max(5, math.ceil(math.sqrt(math.log10(n))))
    bound = 2 * math.sqrt(math.log10(n) / n)
    m_hat = next((m for m in range(max_lag - kn + 1)
                  if np.all(np.abs(rho[m + 1:m + 1 + kn]) < bound)), max_lag - kn)
    m = min(2 * m_hat, max_lag)
    if m == 0:
        return 1.0
    k = np.arange(1, m + 1)
    t = k / m
    lam = np.where(t <= 0.5, 1.0, 2 * (1 - t))
    g = 2 * np.sum(lam * k * acov[1:m + 1])
    g0 = acov[0] + 2 * np.sum(lam * acov[1:m + 1])
    if g0 <= 0:
        return 1.0
    b = (2 * g * g / (2 * g0 * g0)) ** (1 / 3) * n ** (1 / 3)
    return float(np.clip(b, 1, max_block))


# --- replays ------------------------------------------------------------------------

def _exit_masks(o, h, low, plan: Plan):
    if plan.sign > 0:
        return o <= plan.stop, o >= plan.target, low <= plan.stop, h >= plan.target
    return o >= plan.stop, o <= plan.target, h >= plan.stop, low <= plan.target


def settle(o, h, low, last_close, plan: Plan):
    """Per path: outcome (+1 target, -1 stop, 0 neither) and exit price.

    o/h/low are (paths, bars) in plan prices; last_close is each path's last close.
    """
    gap_stop, gap_target, stop_hit, target_hit = _exit_masks(o, h, low, plan)
    event = gap_stop | gap_target | stop_hit | target_hit
    hit = event.any(axis=1)
    first = event.argmax(axis=1)
    rows = np.arange(len(first))
    at_gap_stop, at_gap_target = gap_stop[rows, first], gap_target[rows, first]
    stopped = hit & (at_gap_stop | (~at_gap_target & stop_hit[rows, first]))
    outcome = np.where(hit, np.where(stopped, -1, 1), 0)
    exit_price = np.where(~hit, last_close, np.where(
        stopped, np.where(at_gap_stop, o[rows, first], plan.stop), plan.target))
    return outcome, exit_price


def run_paths(source: np.ndarray, scale: np.ndarray, plan: Plan, slot: int, mean_block: float,
              window: int, replays: int, rng: np.random.Generator, pools: np.ndarray | None = None,
              keep_closes: bool = False):
    """Stationary block bootstrap, bar by bar from ``slot`` to the last bar.

    ``source`` is (S, T, 4) log returns (standardized or raw); bar t of a replay is
    ``exp(source[s, j] * scale[t])`` times the previous close. A new block starts with
    probability 1 / mean_block, at a random session (from the replay's own pool row
    when ``pools`` is given) and at bar t +- ``window``; otherwise the block continues
    with the same session's next bar.
    """
    sessions, bars = source.shape[0], source.shape[1]
    if pools is None:
        pools = np.arange(sessions)[None, :]
    owner = np.arange(replays) * len(pools) // replays

    def pick(mask):
        rows = owner[mask]
        return pools[rows, rng.integers(0, pools.shape[1], size=rows.size)]

    price = np.full(replays, plan.entry)
    is_open = np.ones(replays, dtype=bool)
    outcome = np.zeros(replays, dtype=np.int8)
    exit_price = np.full(replays, np.nan)
    session = np.zeros(replays, dtype=np.int64)
    bar = np.zeros(replays, dtype=np.int64)
    closes = [] if keep_closes else None
    for t in range(slot, bars):
        new = (rng.random(replays) < 1 / mean_block) | (bar + 1 > bars - 1) | (t == slot)
        session[new] = pick(new)
        bar[new] = np.clip(t + rng.integers(-window, window + 1, size=int(new.sum())), 1, bars - 1)
        bar[~new] += 1
        move = np.exp(source[session, bar] * scale[t])
        o, h, low, close = (price * move[:, i] for i in range(4))
        gap_stop, gap_target, stop_hit, target_hit = _exit_masks(o, h, low, plan)
        stop_now = is_open & (gap_stop | (~gap_target & stop_hit))
        target_now = is_open & ~stop_now & (gap_target | target_hit)
        exit_price[stop_now] = np.where(gap_stop, o, plan.stop)[stop_now]
        exit_price[target_now] = plan.target
        outcome[stop_now], outcome[target_now] = -1, 1
        is_open &= ~(stop_now | target_now)
        price = close
        if keep_closes:
            closes.append(close.astype(np.float32))
    exit_price[is_open] = price[is_open]
    return outcome, exit_price, price, (np.stack(closes) if keep_closes else None)


def _summary(outcome, exit_price, last_close, plan: Plan, cost_bps: float) -> dict:
    r = plan.sign * (exit_price - plan.entry) / plan.risk
    green = (outcome == 1) | ((outcome == 0) & (plan.sign * (last_close - plan.entry) > 0))
    return {"count": int(len(outcome)), "p_target": float((outcome == 1).mean()),
            "p_stop": float((outcome == -1).mean()), "p_neither": float((outcome == 0).mean()),
            "p_green": float(green.mean()), "avg_r": float(r.mean()),
            "avg_r_net": float(r.mean() - plan.cost_r(cost_bps)),
            "targets": int((outcome == 1).sum()), "stops": int((outcome == -1).sum()),
            "neither": int((outcome == 0).sum())}


def pool_ranges(outcome, exit_price, plan: Plan, cost_bps: float, groups: int) -> dict:
    """5th-95th percentile across pool resamples of net R and the target-first share."""
    r = (plan.sign * (exit_price - plan.entry) / plan.risk - plan.cost_r(cost_bps)).reshape(groups, -1)
    o = outcome.reshape(groups, -1)
    targets, stops = (o == 1).sum(axis=1), (o == -1).sum(axis=1)
    decided = targets + stops
    share = np.where(decided > 0, targets / np.maximum(decided, 1), np.nan)
    net = np.percentile(r.mean(axis=1), [5, 95])
    shares = np.nanpercentile(share, [5, 95]) if np.isfinite(share).any() else [np.nan, np.nan]
    return {"avg_r_net_range": [float(net[0]), float(net[1])],
            "share_range": [float(shares[0]), float(shares[1])]}


def history(sessions: np.ndarray, plan: Plan, slot: int, cost_bps: float) -> dict:
    """The same percentage bracket entered at the same time on each real session."""
    scale = (plan.entry / sessions[:, slot - 1, 3])[:, None]
    walk = sessions[:, slot:, :]
    o, h, low, close = (walk[..., i] * scale for i in range(4))
    outcome, exit_price = settle(o, h, low, close[:, -1], plan)
    return _summary(outcome, exit_price, close[:, -1], plan, cost_bps)


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# --- verdict and conclusion -------------------------------------------------------------

def verdict(sim: dict, plan: Plan, cost_bps: float) -> tuple[str, list[str]]:
    """One line from the replays that hit a side, weighed by the target's R and costs."""
    pt, ps = sim["p_target"], sim["p_stop"]
    notes = []
    if pt + ps <= 0:
        return "No replay reached the target or the stop by 15:15: the 15:15 price decides every one.", notes
    r, even = plan.r_target, plan.break_even(cost_bps)
    share = pt / (pt + ps)
    gap = share - even
    if abs(gap) < COIN_FLIP:
        head = "Close to a coin flip" if abs(r - 1) < 0.005 else "Close to break-even"
    elif abs(gap) < STRONG:
        head = "Leans toward the target" if gap > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if gap > 0 else "Strongly toward the stop"
    line = (f"{head}: at {r:.2f}R and {cost_bps:g} bps costs the target has to come first in {even:.0%} of the "
            f"replays that hit a side; it did in {share:.0%} (target first {pt:.1%}, stop first {ps:.1%}).")
    if sim["p_neither"] > MANY_NEITHER:
        notes.append(f"{sim['p_neither']:.1%} of the replays hit neither by 15:15; the 15:15 price decides those.")
    if abs(gap) >= COIN_FLIP and sim["avg_r_net"] != 0 and (sim["avg_r_net"] > 0) != (gap > 0):
        notes.append(f"Average net R is {sim['avg_r_net']:+.3f} all the same: "
                     "the replays that hit neither pull it the other way.")
    return line, notes


def conclude(sim: dict, variants: dict, past: dict, plan: Plan, cost_bps: float, pool_size: int) -> dict:
    """``conclusive_negative`` / ``conclusive_positive`` only when every check passes."""
    low, high = sim["avg_r_net_range"]
    share_low, share_high = sim["share_range"]
    even = plan.break_even(cost_bps)
    direction = -1 if high < 0 else 1 if low > 0 else 0
    reference = direction or (1 if sim["avg_r_net"] > 0 else -1)
    clears = (direction < 0 and share_high < even) or (direction > 0 and share_low > even)
    side = "loses" if reference < 0 else "gains"

    def agrees(value):
        return (value > 0) == (reference > 0) and value != 0

    checks = [
        {"name": "pool", "passed": pool_size >= CONCLUSIVE_SESSIONS,
         "detail": f"{pool_size} past sessions (needs {CONCLUSIVE_SESSIONS})"},
        {"name": "range", "passed": bool(clears),
         "detail": (f"90% range over pool resamples: net R {low:+.3f} to {high:+.3f}, target-first share "
                    f"{share_low:.0%} to {share_high:.0%} against {even:.0%} break-even")},
        {"name": "single bars", "passed": agrees(variants["single_bars"]["avg_r_net"]),
         "detail": f"without blocks the bracket {side} too: net R {variants['single_bars']['avg_r_net']:+.3f}"
         if agrees(variants["single_bars"]["avg_r_net"])
         else f"without blocks net R is {variants['single_bars']['avg_r_net']:+.3f}"},
        {"name": "unscaled", "passed": agrees(variants["unscaled"]["avg_r_net"]),
         "detail": f"without today's volatility net R is {variants['unscaled']['avg_r_net']:+.3f}"},
        {"name": "history", "passed": agrees(past["avg_r_net"]),
         "detail": f"the same bracket on {past['count']} real sessions averaged {past['avg_r_net']:+.3f} net R"},
    ]
    failed = [c for c in checks if not c["passed"]]
    if failed:
        status = "inconclusive"
        line = "Inconclusive: " + "; ".join(c["detail"] for c in failed) + "."
    elif direction < 0:
        status = "conclusive_negative"
        line = (f"Conclusive: negative. The bracket loses after costs across the pool's uncertainty (net R {low:+.3f} "
                f"to {high:+.3f}), and single bars, unscaled bars and the real history agree.")
    else:
        status = "conclusive_positive"
        line = (f"Conclusive: positive (net R {low:+.3f} to {high:+.3f}, every check agrees). This is the recent base "
                "rate for this bracket at this time of day, not evidence that the signal has an edge.")
    return {"status": status, "line": line, "checks": checks}


# --- data and entry point -------------------------------------------------------------

def load_candles(symbol: str) -> tuple[pd.DataFrame, str]:
    """1-minute history when stored (it can make 1- and 5-minute bars), else 5-minute."""
    from trading_agent.storage.parquet_store import get_parquet_store

    store = get_parquet_store()
    intervals = store.get_intervals(symbol)
    for interval in ("minute", "5minute"):
        if interval in intervals:
            frame = store.read_candles(symbol, interval)
            if not frame.empty:
                return frame, interval
    raise ValueError(f"No stored minute or 5-minute candles for {symbol}")


def default_entry_time(frame: pd.DataFrame, interval: str) -> datetime:
    """End of the latest stored bar: the moment a plan priced on it could be entered."""
    if frame.empty:
        raise ValueError("No stored candles")
    step = timedelta(minutes=1 if interval == "minute" else 5)
    return pd.to_datetime(frame["timestamp"], utc=True).max().to_pydatetime() + step


def _pool(frame, minutes, entry_day, count):
    by_day = session_bars(frame, minutes)
    days = sorted(d for d in by_day if d < entry_day)[-count:]
    return days, (np.stack([by_day[d] for d in days]) if days else None)


def replay(plan: Plan, config: ReplayConfig = ReplayConfig(), frame: pd.DataFrame | None = None,
           interval: str = "minute") -> dict:
    """Simulation, variants, history, verdict and conclusion for one plan, JSON-ready."""
    if frame is None:
        frame, interval = load_candles(plan.symbol)
    entry_day = pd.Timestamp(plan.entry_time).tz_convert(IST).date()
    minutes = config.bar_minutes or 5
    days, sessions = _pool(frame, minutes, entry_day, config.sessions)
    if config.bar_minutes is None and interval == "minute":
        fine_days, fine = _pool(frame, 1, entry_day, config.sessions)
        if len(fine_days) >= max(MIN_SESSIONS, int(0.9 * len(days))):
            minutes, days, sessions = 1, fine_days, fine
    if len(days) < MIN_SESSIONS:
        raise ValueError(f"{len(days)} usable sessions before {entry_day}; at least {MIN_SESSIONS} are needed")
    slot = first_slot(plan.entry_time, minutes)
    bars = SESSION_MINUTES // minutes
    window = config.window_minutes // minutes

    returns = log_returns(sessions)
    profile = vol_profile(returns, minutes)
    vols = day_vols(returns, profile)
    standard = returns / (vols[:, None, None] * profile[None, :, None])
    vol = today_vol(frame, entry_day, slot, minutes, profile, float(np.median(vols[-RECENT_SESSIONS:])))
    vol["ratio"] = vol["used"] / float(np.median(vols))
    max_block = max(1, MAX_BLOCK_MINUTES // minutes)
    block = max(optimal_block(standard[:, 1:, 3], max_block),
                optimal_block(np.abs(standard[:, 1:, 3]), max_block))

    seeds = np.random.SeedSequence(config.seed).spawn(4)
    scaled = vol["used"] * profile
    outcome, exit_price, last, closes = run_paths(standard, scaled, plan, slot, block, window, config.replays,
                                                  np.random.default_rng(seeds[0]), keep_closes=True)
    sim = _summary(outcome, exit_price, last, plan, config.cost_bps)
    sim["close_p5"], sim["close_median"], sim["close_p95"] = (float(v) for v in np.percentile(last, [5, 50, 95]))
    marks = [i for i in range(len(closes)) if ((slot + i + 1) * minutes) % 5 == 0]
    bands = np.percentile(closes[marks], BAND_QUANTILES, axis=1)
    sim["bands"] = {"time": [_clock(OPEN_MINUTE + (slot + i + 1) * minutes) for i in marks],
                    **{f"p{q}": [round(float(v), 2) for v in row] for q, row in zip(BAND_QUANTILES, bands)}}

    ci_rng = np.random.default_rng(seeds[3])
    resamples = ci_rng.integers(0, len(days), size=(CI_GROUPS, len(days)))
    ci = run_paths(standard, scaled, plan, slot, block, window, CI_GROUPS * CI_REPLAYS, ci_rng, pools=resamples)
    sim.update(pool_ranges(ci[0], ci[1], plan, config.cost_bps, CI_GROUPS))

    variants = {
        "single_bars": _summary(*run_paths(standard, scaled, plan, slot, 1.0, window, config.replays,
                                           np.random.default_rng(seeds[1]))[:3], plan, config.cost_bps),
        "unscaled": _summary(*run_paths(returns, np.ones(bars), plan, slot, block, window, config.replays,
                                        np.random.default_rng(seeds[2]))[:3], plan, config.cost_bps),
    }
    past = history(sessions, plan, slot, config.cost_bps)
    line, notes = verdict(sim, plan, config.cost_bps)
    conclusion = conclude(sim, variants, past, plan, config.cost_bps, len(days))
    if vol["today_bars"] == 0:
        notes.append("No bars from today before the entry: today's volatility is the recent median.")
    return {
        "symbol": plan.symbol, "side": plan.side, "entry": plan.entry, "stop": plan.stop, "target": plan.target,
        "entry_time": pd.Timestamp(plan.entry_time).tz_convert(IST).isoformat(),
        "first_bar": _clock(OPEN_MINUTE + slot * minutes), "exit_time": _clock(EXIT_MINUTE),
        "bar_minutes": minutes, "bars_left": bars - slot, "risk_per_share": plan.risk, "r_target": plan.r_target,
        "break_even_share": plan.break_even(config.cost_bps), "mean_block_minutes": block * minutes,
        "config": asdict(config),
        "pool": {"sessions": len(days), "first": days[0].isoformat(), "last": days[-1].isoformat(),
                 "interval": interval},
        "volatility": vol, "simulation": sim, "variants": variants, "history": past,
        "verdict": line, "conclusion": conclusion, "notes": notes, "limitations": LIMITATIONS,
    }


def report(doc: dict) -> str:
    sim, past, cfg, vol = doc["simulation"], doc["history"], doc["config"], doc["volatility"]
    low, high = sim["avg_r_net_range"]
    lines = [
        f"{doc['symbol']} {doc['side']}  entry {doc['entry']:g}  stop {doc['stop']:g}  target {doc['target']:g}  "
        f"({doc['r_target']:.2f}R, risk {doc['risk_per_share']:.2f} a share)",
        f"{cfg['replays']:,} replays of {doc['first_bar']}-{doc['exit_time']} IST ({doc['bars_left']} "
        f"{doc['bar_minutes']}-minute bars) from {doc['pool']['sessions']} sessions {doc['pool']['first']} to "
        f"{doc['pool']['last']}, mean block {doc['mean_block_minutes']:.0f} min, seed {cfg['seed']}.",
        f"Volatility used: {vol['ratio']:.2f}x the pool median ({vol['today_bars']} bars from today).",
        f"  target first  {sim['p_target']:6.1%}",
        f"  stop first    {sim['p_stop']:6.1%}",
        f"  neither       {sim['p_neither']:6.1%}   still open at 15:15",
        f"  green         {sim['p_green']:6.1%}   target first, or on the right side of the entry at 15:15",
        f"  Price at 15:15: median {sim['close_median']:.2f}, 90% of replays between "
        f"{sim['close_p5']:.2f} and {sim['close_p95']:.2f}.",
        f"  Average R {sim['avg_r']:+.3f}, net of {cfg['cost_bps']:g} bps {sim['avg_r_net']:+.3f} "
        f"(90% over pool resamples {low:+.3f} to {high:+.3f}).",
    ]
    for name, label in (("single_bars", "Single bars"), ("unscaled", "Unscaled")):
        v = doc["variants"][name]
        lines.append(f"{label:12s} target first {v['p_target']:.1%}, stop first {v['p_stop']:.1%}, "
                     f"neither {v['p_neither']:.1%}, average net R {v['avg_r_net']:+.3f}.")
    lines += [
        f"History: the same bracket at {doc['first_bar']} on {past['count']} real sessions: {past['targets']} target, "
        f"{past['stops']} stop, {past['neither']} neither. Average net R {past['avg_r_net']:+.3f}.",
        doc["verdict"], *doc["notes"], doc["conclusion"]["line"], doc["limitations"],
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay one bracket over the rest of the session; research only.")
    parser.add_argument("symbol")
    parser.add_argument("--side", choices=["BUY", "SELL"], required=True)
    parser.add_argument("--entry", type=float, required=True)
    parser.add_argument("--stop", type=float, required=True)
    parser.add_argument("--target", type=float, required=True)
    parser.add_argument("--at", help='Entry time, IST "YYYY-MM-DD HH:MM" (default: end of the latest stored bar)')
    parser.add_argument("--replays", type=int, default=ReplayConfig.replays)
    parser.add_argument("--seed", type=int, default=ReplayConfig.seed)
    parser.add_argument("--sessions", type=int, default=ReplayConfig.sessions)
    parser.add_argument("--window-minutes", type=int, default=ReplayConfig.window_minutes)
    parser.add_argument("--cost-bps", type=float, default=ReplayConfig.cost_bps)
    parser.add_argument("--bar-minutes", type=int, choices=[1, 5], default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        symbol = args.symbol.strip().upper()
        frame, interval = load_candles(symbol)
        at = (pd.Timestamp(args.at, tz=IST).to_pydatetime() if args.at
              else default_entry_time(frame, interval))
        plan = Plan(symbol, args.side, args.entry, args.stop, args.target, at.astimezone(UTC))
        config = ReplayConfig(args.replays, args.seed, args.sessions, args.window_minutes, args.cost_bps,
                              args.bar_minutes)
        doc = replay(plan, config, frame, interval)
    except ValueError as error:
        print(f"Cannot replay: {error}")
        return 1
    if args.json:
        print(json.dumps({**doc, "simulation": {k: v for k, v in doc["simulation"].items() if k != "bands"}}, indent=2))
    else:
        print(report(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
