"""Conservative next-open bracket replay. No orders, downloads or model writes."""
from dataclasses import asdict, dataclass
from datetime import time

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class TradePolicy:
    horizon: int = 24
    stop_atr: float = 1.5
    reward_risk: float = 2.0
    cost_bps: float = 10.0  # Total round-trip fees/spread assumption.
    slippage_bps: float = 2.0  # Additional adverse slippage on EACH fill.
    entry_delay_bars: int = 1  # Outcome ML uses 2 to allow prospective paper recording.

    def __post_init__(self):
        values = list(asdict(self).values())
        if not np.isfinite(values).all():
            raise ValueError("Trade policy must be finite")
        if not isinstance(self.horizon, int) or not 1 <= self.horizon <= 120 or not 0 < self.stop_atr <= 10:
            raise ValueError("Invalid horizon or ATR stop")
        if self.entry_delay_bars not in (1, 2) or not isinstance(self.entry_delay_bars, int):
            raise ValueError("Entry delay must be one or two bars")
        if not 1 <= self.reward_risk <= 5 or not 0 <= self.cost_bps <= 100 or not 0 <= self.slippage_bps <= 50:
            raise ValueError("Invalid reward/risk or costs")


def trade_outcome(frame, signal_index, side, policy=TradePolicy(), interval_seconds=60):
    """Returns None for unobservable outcomes; never label an unknown future a loss.

    Bars are start-stamped. Entries before 15:20 IST; forced exit by 15:25, or
    on the session's last bar when it ends at or after 15:15: since NSE's closing
    auction (2026-08-03) many stocks have no continuous-trading bars after 15:14.
    Same-bar stop/target ambiguity assumes stop first. Gap stops fill at the
    worse opening price, gap targets at the target (no optimistic improvement).
    Weekday/time checks are not an official holiday calendar.
    """
    entry_index = signal_index + policy.entry_delay_bars
    if side not in {"BUY", "SELL"} or entry_index >= len(frame):
        return None
    sign = 1 if side == "BUY" else -1
    signal, first = frame.iloc[signal_index], frame.iloc[entry_index]
    local = pd.Timestamp(first.timestamp).tz_convert("Asia/Kolkata")
    if local.weekday() >= 5 or not time(9, 15) <= local.time() < time(15, 20):
        return None
    if pd.Timestamp(signal.timestamp).tz_convert("Asia/Kolkata").date() != local.date():
        return None
    distance = float(signal.atr) * policy.stop_atr
    slip = policy.slippage_bps / 10000
    entry = float(first.open) * (1 + sign * slip)
    if not np.isfinite([distance, entry]).all() or distance <= 0 or entry <= 0:
        return None
    stop, target = entry - sign * distance, entry + sign * distance * policy.reward_risk
    if min(stop, target) <= 0:
        return None
    entry_stamps = pd.to_datetime(frame.timestamp.iloc[signal_index:entry_index+1], utc=True)
    if not entry_stamps.diff().dropna().dt.total_seconds().between(1, interval_seconds*1.5).all():
        return None
    previous = pd.Timestamp(frame.timestamp.iloc[entry_index-1])
    final_index = entry_index + policy.horizon - 1
    for j in range(entry_index, min(len(frame), final_index + 1)):
        bar = frame.iloc[j]
        stamp = pd.Timestamp(bar.timestamp)
        bar_local = stamp.tz_convert("Asia/Kolkata")
        bar_end = bar_local + pd.Timedelta(seconds=interval_seconds)
        elapsed = (stamp - previous).total_seconds()
        if (not 0 < elapsed <= interval_seconds * 1.5 or bar_local.date() != local.date()
                or bar_end.time() > time(15, 25)):
            return None
        previous = stamp
        o, h, low, close = map(float, [bar.open, bar.high, bar.low, bar.close])
        if not np.isfinite([o, h, low, close]).all() or low <= 0 or low > min(o, close) or h < max(o, close):
            return None
        stop_hit = low <= stop if sign == 1 else h >= stop
        target_hit = h >= target if sign == 1 else low <= target
        # An opening gap has observable ordering, before the bar's other prices.
        gap_stop = o <= stop if sign == 1 else o >= stop
        gap_target = o >= target if sign == 1 else o <= target
        if gap_stop:
            exit_price, reason = o, "gap_stop"
        elif gap_target:
            exit_price, reason = target, "target"
        elif stop_hit:
            exit_price, reason = stop, "ambiguous_stop_first" if target_hit else "stop"
        elif target_hit:
            exit_price, reason = target, "target"
        elif ((bar_end + pd.Timedelta(seconds=interval_seconds)).time() > time(15, 25)
              or (bar_end.time() >= time(15, 15) and (
                  j + 1 >= len(frame)
                  or pd.Timestamp(frame.timestamp.iloc[j + 1]).tz_convert("Asia/Kolkata").date() != bar_local.date()))):
            exit_price, reason = close, "session_exit"
        elif j == final_index:
            exit_price, reason = close, "timeout"
        else:
            continue
        exit_price *= 1 - sign * slip
        net = sign * (exit_price - entry) / entry - policy.cost_bps / 10000
        return {"signal_index": signal_index, "exit_index": j, "side": side,
                "timestamp": pd.Timestamp(signal.timestamp).isoformat(),
                "exit_timestamp": stamp.isoformat(), "session": str(local.date()),
                "entry": entry, "stop": stop, "target": target, "exit": exit_price,
                "exit_reason": reason, "net_return": float(net), "win": int(net > 0)}
    return None


def replay(frame, actions, scores, start, end, policy, interval_seconds):
    """Each partition owns its outcome bars; positions do not overlap."""
    trades, next_free = [], start
    for i in range(max(60, start), end - policy.horizon - policy.entry_delay_bars + 1):
        if i < next_free or actions[i] not in (1, 2):
            continue
        trade = trade_outcome(frame, i, "BUY" if actions[i] == 1 else "SELL", policy, interval_seconds)
        if trade and trade["exit_index"] < end:
            trade["score"] = float(scores[i])
            trades.append(trade)
            next_free = trade["exit_index"]
    return trades


def outcome_arrays(frame, side, policy=TradePolicy(), interval_seconds=60):
    """Vectorized labels matching trade_outcome; NaN denotes unknown outcomes.

    All columns are observed OHLC, never features from the label period.
    Kept separate from the readable scalar oracle and parity-tested against it.
    """
    n = len(frame)
    net, exits = np.full(n, np.nan), np.full(n, -1, dtype=int)
    if not n or side not in {"BUY", "SELL"}:
        return net, exits
    stamp = pd.DatetimeIndex(pd.to_datetime(frame.timestamp, utc=True))
    local = stamp.tz_convert("Asia/Kolkata")
    day = np.array(local.date)
    minute = np.asarray(local.hour*60 + local.minute) + np.asarray(local.second)/60
    seconds = stamp.asi8/1e9
    o, high, low, close, atr = [frame[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close", "atr")]
    sign = 1 if side == "BUY" else -1
    slip = policy.slippage_bps/10000
    indices = np.arange(n)
    entry_i = indices + policy.entry_delay_bars
    safe_entry = np.minimum(entry_i, n-1)
    entry = o[safe_entry]*(1+sign*slip)
    distance = atr*policy.stop_atr
    stop, target = entry-sign*distance, entry+sign*distance*policy.reward_risk
    active = ((entry_i < n) & (np.asarray(local.weekday)[safe_entry] < 5) &
              (minute[safe_entry] >= 555) & (minute[safe_entry] < 920) &
              (day == day[safe_entry]) & np.isfinite(entry) & np.isfinite(distance) &
              (entry > 0) & (distance > 0) & (stop > 0) & (target > 0))
    for offset in range(1, policy.entry_delay_bars+1):
        j = np.minimum(indices+offset, n-1)
        prev = np.minimum(indices+offset-1, n-1)
        active &= (seconds[j]-seconds[prev] > 0) & (seconds[j]-seconds[prev] <= interval_seconds*1.5)
    for offset in range(policy.entry_delay_bars, policy.entry_delay_bars+policy.horizon):
        j = np.minimum(indices+offset, n-1)
        prev = np.minimum(indices+offset-1, n-1)
        end_minute = minute[j]+interval_seconds/60
        active &= ((indices+offset < n) & (day[j] == day[safe_entry]) &
                   (seconds[j]-seconds[prev] > 0) & (seconds[j]-seconds[prev] <= interval_seconds*1.5) &
                   (end_minute <= 925) & np.isfinite(o[j]) & np.isfinite(high[j]) &
                   np.isfinite(low[j]) & np.isfinite(close[j]) &
                   (low[j] > 0) & (low[j] <= np.minimum(o[j], close[j])) & (high[j] >= np.maximum(o[j], close[j])))
        gap_stop = o[j] <= stop if sign == 1 else o[j] >= stop
        gap_target = o[j] >= target if sign == 1 else o[j] <= target
        stop_hit = low[j] <= stop if sign == 1 else high[j] >= stop
        target_hit = high[j] >= target if sign == 1 else low[j] <= target
        last_of_day = (indices+offset+1 >= n) | (day[np.minimum(j+1, n-1)] != day[j])
        forced = ((end_minute+interval_seconds/60 > 925) | (last_of_day & (end_minute >= 915)) |
                  (offset == policy.entry_delay_bars+policy.horizon-1))
        done = active & (gap_stop | gap_target | stop_hit | target_hit | forced)
        price = np.where(gap_stop, o[j], np.where(gap_target, target,
                         np.where(stop_hit, stop, np.where(target_hit, target, close[j]))))
        net[done] = sign*(price[done]*(1-sign*slip)-entry[done])/entry[done]-policy.cost_bps/10000
        exits[done] = j[done]
        active[done] = False
    return net, exits
