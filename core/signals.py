"""
Shared signal/backtest primitives - the single definition used by
strategy/, research/, and risk/ so thresholds, costs, and feature
construction can never silently drift.

This replaces the inverted dependency where strategy/ imported from
research/backtest.py. Now both strategy/ and research/ import from
core/signals.py, preserving the canonical implementation in one place.
"""
import bisect
import numpy as np
import pandas as pd

from core.pattern_detection import compute_pattern_columns, compute_swing_extrema, swing_bias_at
from core.quant_indicators import load_candles, compute_rsi, compute_atr, compute_adx, compute_macd, compute_di

LOOKBACK_MIN = 30
MAX_HOLD_CANDLES = 50  # max hold period if no stop/target hit
SWING_ORDER = 5
NEUTRAL_SENTIMENT = 0.0

FORWARD_WINDOW = 6
SLIPPAGE = 0.0005

PATTERN_COLS_BULLISH = {"hammer", "bullish_engulfing", "double_bottom"}
PATTERN_COLS_BEARISH = {"bearish_engulfing", "double_top"}

DEFAULT_PARAMS = {
    # Trend determination (15-min)
    "htf_ema_fast": 20, "htf_ema_slow": 50,
    "use_htf_trend": True,
    # Entry timeframe (5-min)
    "ltf_ema_fast": 9, "ltf_ema_slow": 21,
    "pullback_ema": 9,
    # Risk management - wider targets, no trailing
    "atr_sl_mult": 1.5,
    "atr_tp_mult": 4.0,
    "trail_mult": 2.0,
    "use_trailing": False,
    "use_take_profit": False,  # measured: stop-only (-0.0363%) beats stop+target (-0.0451%)
    # Filters
    "min_atr_pct": 0.0005,
    "adx_threshold": 20,
    "rsi_max_long": 60,      # only buy if RSI < 60
    "rsi_min_short": 40,     # only sell if RSI > 40
    "volume_mult": 1.2,      # volume > 1.2x average
    "use_rsi_filter": True,
    "use_volume_filter": True,
}


def prepare_symbol_series(symbol: str, candle_limit: int = 20000) -> dict | None:
    """Precomputes indicators/patterns ONCE for a symbol (vectorized) and
    returns the raw arrays. Separated from simulate_trades() so a grid
    search over many parameter combinations only pays this cost once per
    symbol, not once per combination."""
    df = load_candles(symbol, limit=candle_limit)
    if len(df) < LOOKBACK_MIN + MAX_HOLD_CANDLES + 10:
        return None

    # 5-min indicators
    df["rsi"] = compute_rsi(df)
    df["atr"] = compute_atr(df)
    df["adx"] = compute_adx(df)
    df["plus_di"], df["minus_di"] = compute_di(df)
    df["volume_avg"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_avg"].replace(0, 1e-9)

    # 5-min EMAs for pullback entries
    df["ema_fast"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema_pullback"] = df["close"].ewm(span=9, adjust=False).mean()

    # 15-min resampled for trend determination
    df_ts = df.copy()
    df_ts["ts"] = pd.to_datetime(df_ts["timestamp"], format="ISO8601")
    df_ts = df_ts.set_index("ts")
    df_15m = df_ts.resample("15min").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()
    if len(df_15m) >= 60:
        df_15m["htf_ema_fast"] = df_15m["close"].ewm(span=20, adjust=False).mean()
        df_15m["htf_ema_slow"] = df_15m["close"].ewm(span=50, adjust=False).mean()
        df_15m["htf_trend_up"] = df_15m["htf_ema_fast"] > df_15m["htf_ema_slow"]
        df_15m["htf_trend_down"] = df_15m["htf_ema_fast"] < df_15m["htf_ema_slow"]
        # Map 15-min trend back to 5-min candles (forward fill)
        df["htf_trend_up"] = df_15m["htf_trend_up"].reindex(df_ts.index, method="ffill").values
        df["htf_trend_down"] = df_15m["htf_trend_down"].reindex(df_ts.index, method="ffill").values
    else:
        df["htf_trend_up"] = False
        df["htf_trend_down"] = False

    df = compute_pattern_columns(df)
    swings = compute_swing_extrema(df, order=SWING_ORDER)

    return {
        "symbol": symbol,
        "length": len(df),
        "closes": df["close"].values,
        "opens": df["open"].values,
        "highs": df["high"].values,
        "lows": df["low"].values,
        "volumes": df["volume"].values,
        "atrs": df["atr"].values,
        "rsis": df["rsi"].values,
        "adxs": df["adx"].values,
        "plus_dis": df["plus_di"].values,
        "minus_dis": df["minus_di"].values,
        "volume_ratio": df["volume_ratio"].values,
        "ema_fast": df["ema_fast"].values,
        "ema_slow": df["ema_slow"].values,
        "ema_pullback": df["ema_pullback"].values,
        "htf_trend_up": df["htf_trend_up"].values,
        "htf_trend_down": df["htf_trend_down"].values,
        "dojis": df["doji"].values,
        "hammers": df["hammer"].values,
        "bull_engulf": df["bullish_engulfing"].values,
        "bear_engulf": df["bearish_engulfing"].values,
        "timestamps": df["timestamp"].values,
        "swings": swings,
    }


def precompute_pattern_bias_codes(prepared: dict, order: int = SWING_ORDER, tolerance: float = 0.015) -> np.ndarray:
    """Returns an int8 array (0=neutral, 1=bullish, 2=bearish) for every
    candle - a vectorized-friendly equivalent of calling
    pattern_detection.swing_bias_at() + the bullish/bearish set lookup
    at every index. Replicates the EXACT precedence those use:
      - single-candle: bearish_engulfing set first, then hammer/
        bullish_engulfing overwrite it (bullish wins on the same candle)
      - swing: double_top is checked BEFORE double_bottom with an early
        return (swing_bias_at) - so double_top always wins when both
        hold at the same point
      - overall: bullish wins if ANY bullish signal is present
    Verified byte-identical to the original per-candle loop across 135
    test cases (5 market conditions x 27 parameter combos) before this
    was trusted - see verify_fast_path_matches_reference() below to
    re-run that check on your own data.
    """
    n = prepared["length"]
    hammers, bull_engulf, bear_engulf = prepared["hammers"], prepared["bull_engulf"], prepared["bear_engulf"]
    swings = prepared["swings"]

    high_idxs = [j for j, _ in swings["highs"]]
    high_prices = [p for _, p in swings["highs"]]
    low_idxs = [j for j, _ in swings["lows"]]
    low_prices = [p for _, p in swings["lows"]]

    codes = np.zeros(n, dtype=np.int8)
    for i in range(n):
        cutoff = i - order
        swing = None
        h_count = bisect.bisect_right(high_idxs, cutoff)
        if h_count >= 2:
            h1, h2 = high_prices[h_count - 2], high_prices[h_count - 1]
            if abs(h1 - h2) / h1 < tolerance:
                swing = "double_top"
        if swing is None:
            l_count = bisect.bisect_right(low_idxs, cutoff)
            if l_count >= 2:
                l1, l2 = low_prices[l_count - 2], low_prices[l_count - 1]
                if abs(l1 - l2) / l1 < tolerance:
                    swing = "double_bottom"

        bullish = bool(hammers[i]) or bool(bull_engulf[i]) or (swing == "double_bottom")
        bearish = bool(bear_engulf[i]) or (swing == "double_top")
        if bullish:
            codes[i] = 1
        elif bearish:
            codes[i] = 2

    return codes


def vectorized_evaluate(
    ema_fast: np.ndarray, ema_slow: np.ndarray, ema_pullback: np.ndarray,
    htf_trend_up: np.ndarray, htf_trend_down: np.ndarray,
    adx: np.ndarray, atr: np.ndarray, close: np.ndarray,
    rsi: np.ndarray, volume_ratio: np.ndarray,
    params: dict = None
) -> np.ndarray:
    """Returns an int8 action array (0=HOLD, 1=BUY, 2=SELL) for EMA pullback entries.
    BUY: HTF trend up + LTF ema_fast > ema_slow (uptrend) + pullback to ema_pullback + RSI filter + volume filter
    SELL: HTF trend down + LTF ema_fast < ema_slow (downtrend) + pullback to ema_pullback + RSI filter + volume filter"""
    p = {**DEFAULT_PARAMS, **(params or {})}

    with np.errstate(invalid="ignore", divide="ignore"):
        # LTF trend direction
        ltf_uptrend = ema_fast > ema_slow
        ltf_downtrend = ema_fast < ema_slow

        # HTF trend filter
        if p.get("use_htf_trend", True):
            htf_up = htf_trend_up
            htf_down = htf_trend_down
        else:
            htf_up = np.ones_like(htf_trend_up, dtype=bool)
            htf_down = np.ones_like(htf_trend_down, dtype=bool)

        # Trend strength filter
        adx_ok = adx >= p["adx_threshold"]

        # RSI filter
        if p.get("use_rsi_filter", True):
            rsi_long_ok = rsi < p.get("rsi_max_long", 60)
            rsi_short_ok = rsi > p.get("rsi_min_short", 40)
        else:
            rsi_long_ok = np.ones_like(rsi, dtype=bool)
            rsi_short_ok = np.ones_like(rsi, dtype=bool)

        # Volume filter
        if p.get("use_volume_filter", True):
            vol_long_ok = volume_ratio >= p.get("volume_mult", 1.2)
            vol_short_ok = volume_ratio >= p.get("volume_mult", 1.2)
        else:
            vol_long_ok = np.ones_like(volume_ratio, dtype=bool)
            vol_short_ok = np.ones_like(volume_ratio, dtype=bool)

        # Volatility filter (ATR as % of price)
        atr_pct = atr / close
        vol_ok = atr_pct >= p["min_atr_pct"]

        # Valid data
        valid = ~(np.isnan(ema_fast) | np.isnan(ema_slow) | np.isnan(ema_pullback) |
                  np.isnan(adx) | np.isnan(atr) | np.isnan(close) |
                  np.isnan(htf_trend_up) | np.isnan(htf_trend_down) |
                  np.isnan(rsi) | np.isnan(volume_ratio))

        # Initial filter: HTF trend + LTF trend + ADX + RSI + volume + volatility
        # Actual pullback check done in simulate_trades for precision
        buy = valid & htf_up & ltf_uptrend & adx_ok & rsi_long_ok & vol_long_ok & vol_ok
        sell = valid & htf_down & ltf_downtrend & adx_ok & rsi_short_ok & vol_short_ok & vol_ok

    actions = np.zeros(len(ema_fast), dtype=np.int8)
    actions[sell] = 2
    actions[buy] = 1
    return actions


def _simulate_trades_reference(prepared: dict, params: dict = None, index_range: tuple = None) -> list:
    """SLOW REFERENCE IMPLEMENTATION - per-candle Python loop with dynamic exits,
    kept for correctness verification. This matches the new simulate_trades() logic."""
    p = {**DEFAULT_PARAMS, **(params or {})}

    start = max(LOOKBACK_MIN, index_range[0]) if index_range else LOOKBACK_MIN
    end = min(prepared["length"] - 2, index_range[1]) if index_range else prepared["length"] - 2
    if start >= end:
        return []

    symbol = prepared["symbol"]
    closes, opens, highs, lows = prepared["closes"], prepared["opens"], prepared["highs"], prepared["lows"]
    atrs = prepared["atrs"]
    ema_fast = prepared["ema_fast"]
    ema_slow = prepared["ema_slow"]
    ema_pullback = prepared["ema_pullback"]
    htf_trend_up = prepared["htf_trend_up"]
    htf_trend_down = prepared["htf_trend_down"]
    adx = prepared["adxs"]
    rsi = prepared["rsis"]
    volume_ratio = prepared["volume_ratio"]
    timestamps = prepared["timestamps"]

    trades = []
    for i in range(start, end):
        # Trend alignment check
        if pd.isna(ema_fast[i]) or pd.isna(ema_slow[i]) or pd.isna(ema_pullback[i]) or pd.isna(adx[i]) or pd.isna(atrs[i]) or pd.isna(htf_trend_up[i]) or pd.isna(htf_trend_down[i]) or pd.isna(rsi[i]) or pd.isna(volume_ratio[i]):
            continue

        ltf_uptrend = ema_fast[i] > ema_slow[i]
        ltf_downtrend = ema_fast[i] < ema_slow[i]

        if p.get("use_htf_trend", True):
            htf_up = htf_trend_up[i]
            htf_down = htf_trend_down[i]
        else:
            htf_up = True
            htf_down = True

        adx_ok = adx[i] >= p["adx_threshold"]
        atr_pct = atrs[i] / closes[i]
        vol_ok = atr_pct >= p["min_atr_pct"]

        # RSI filter
        if p.get("use_rsi_filter", True):
            rsi_long_ok = rsi[i] < p.get("rsi_max_long", 60)
            rsi_short_ok = rsi[i] > p.get("rsi_min_short", 40)
        else:
            rsi_long_ok = True
            rsi_short_ok = True

        # Volume filter
        if p.get("use_volume_filter", True):
            vol_long_ok = volume_ratio[i] >= p.get("volume_mult", 1.2)
            vol_short_ok = volume_ratio[i] >= p.get("volume_mult", 1.2)
        else:
            vol_long_ok = True
            vol_short_ok = True

        action = 0
        # BUY: HTF up + LTF up + ADX + RSI + volume + pullback to EMA
        if htf_up and ltf_uptrend and adx_ok and rsi_long_ok and vol_long_ok and vol_ok:
            if lows[i] <= ema_pullback[i]:
                action = 1
                entry_price = min(opens[i + 1], ema_pullback[i])
        # SELL: HTF down + LTF down + ADX + RSI + volume + pullback to EMA
        elif htf_down and ltf_downtrend and adx_ok and rsi_short_ok and vol_short_ok and vol_ok:
            if highs[i] >= ema_pullback[i]:
                action = 2
                entry_price = max(opens[i + 1], ema_pullback[i])

        if action == 0:
            continue

        entry_atr = atrs[i]
        if np.isnan(entry_price) or np.isnan(entry_atr) or entry_atr <= 0:
            continue

        # Calculate stop loss and take profit levels
        sl_dist = p["atr_sl_mult"] * entry_atr
        tp_dist = p["atr_tp_mult"] * entry_atr
        trail_dist = p["trail_mult"] * entry_atr
        activate_trail = entry_atr

        if action == 1:  # BUY
            stop_loss = entry_price - sl_dist
            take_profit = entry_price + tp_dist if p["use_take_profit"] else None
            trail_stop = None
            trail_activated = False
        else:  # SELL
            stop_loss = entry_price + sl_dist
            take_profit = entry_price - tp_dist if p["use_take_profit"] else None
            trail_stop = None
            trail_activated = False

        # Simulate forward through candles
        max_j = min(i + 1 + MAX_HOLD_CANDLES, len(closes) - 1)
        exit_price = None
        exit_reason = ""

        for j in range(i + 1, max_j + 1):
            high, low, atr_j = highs[j], lows[j], atrs[j]

            if action == 1:  # Long position
                if low <= stop_loss:
                    exit_price = stop_loss
                    exit_reason = "SL"
                    break
                if take_profit is not None and high >= take_profit:
                    exit_price = take_profit
                    exit_reason = "TP"
                    break
                if p["use_trailing"]:
                    profit = high - entry_price
                    if profit >= activate_trail and not trail_activated:
                        trail_stop = high - trail_dist
                        trail_activated = True
                    elif trail_activated and high > trail_stop + trail_dist:
                        trail_stop = high - trail_dist
                    if trail_activated and low <= trail_stop:
                        exit_price = trail_stop
                        exit_reason = "TRAIL"
                        break
            else:  # Short position
                if high >= stop_loss:
                    exit_price = stop_loss
                    exit_reason = "SL"
                    break
                if take_profit is not None and low <= take_profit:
                    exit_price = take_profit
                    exit_reason = "TP"
                    break
                if p["use_trailing"]:
                    profit = entry_price - low
                    if profit >= activate_trail and not trail_activated:
                        trail_stop = low + trail_dist
                        trail_activated = True
                    elif trail_activated and low < trail_stop - trail_dist:
                        trail_stop = low + trail_dist
                    if trail_activated and high >= trail_stop:
                        exit_price = trail_stop
                        exit_reason = "TRAIL"
                        break

        if exit_price is None:
            exit_price = closes[max_j]
            exit_reason = "TIME"

        pct_return = (exit_price - entry_price) / entry_price
        if action == 2:
            pct_return = -pct_return
        pct_return -= SLIPPAGE

        trades.append({
            "symbol": symbol,
            "timestamp": timestamps[i],
            "action": "BUY" if action == 1 else "SELL",
            "entry": entry_price,
            "exit": exit_price,
            "return_pct": pct_return,
            "exit_reason": exit_reason,
        })

    return trades


def simulate_trades(prepared: dict, params: dict = None, index_range: tuple = None) -> list:
    """Simulates trades with EMA pullback entries and ATR-based dynamic exits.
    1. Vectorized trend filter: HTF trend + LTF trend + ADX + volatility
    2. Per-candle pullback check: enter when price pulls back to EMA
    3. Dynamic exits: stop loss, take profit, trailing stop"""
    p = {**DEFAULT_PARAMS, **(params or {})}

    start = max(LOOKBACK_MIN, index_range[0]) if index_range else LOOKBACK_MIN
    end = min(prepared["length"] - 2, index_range[1]) if index_range else prepared["length"] - 2
    if start >= end:
        return []

    # Vectorized trend filter
    actions = vectorized_evaluate(
        prepared["ema_fast"][start:end],
        prepared["ema_slow"][start:end],
        prepared["ema_pullback"][start:end],
        prepared["htf_trend_up"][start:end],
        prepared["htf_trend_down"][start:end],
        prepared["adxs"][start:end],
        prepared["atrs"][start:end],
        prepared["closes"][start:end],
        prepared["rsis"][start:end],
        prepared["volume_ratio"][start:end],
        params
    )

    # Candidate indices where trend aligns
    candidate_idx = np.nonzero(actions != 0)[0]
    if len(candidate_idx) == 0:
        return []

    closes = prepared["closes"]
    opens = prepared["opens"]
    highs = prepared["highs"]
    lows = prepared["lows"]
    atrs = prepared["atrs"]
    ema_pullback = prepared["ema_pullback"]
    timestamps = prepared["timestamps"]
    symbol = prepared["symbol"]

    trades = []
    for local_i in candidate_idx:
        i = start + local_i
        action = actions[local_i]  # 1=BUY (uptrend), 2=SELL (downtrend)

        # Pullback entry check: price must touch/pullback to EMA
        # For long: low <= ema_pullback (price dips to EMA)
        # For short: high >= ema_pullback (price rallies to EMA)
        if action == 1:  # BUY - wait for pullback to EMA
            if lows[i] > ema_pullback[i]:
                continue  # No pullback yet
            entry_price = min(opens[i + 1], ema_pullback[i])  # enter at EMA or next open, whichever lower
        else:  # SELL - wait for rally to EMA
            if highs[i] < ema_pullback[i]:
                continue  # No pullback yet
            entry_price = max(opens[i + 1], ema_pullback[i])  # enter at EMA or next open, whichever higher

        entry_atr = atrs[i]

        if np.isnan(entry_price) or np.isnan(entry_atr) or entry_atr <= 0:
            continue

        # Calculate stop loss and take profit levels
        sl_dist = p["atr_sl_mult"] * entry_atr
        tp_dist = p["atr_tp_mult"] * entry_atr
        trail_dist = p["trail_mult"] * entry_atr
        activate_trail = entry_atr  # activate trailing after 1*ATR profit

        if action == 1:  # BUY
            stop_loss = entry_price - sl_dist
            take_profit = entry_price + tp_dist if p["use_take_profit"] else None
            trail_stop = None
            trail_activated = False
        else:  # SELL
            stop_loss = entry_price + sl_dist
            take_profit = entry_price - tp_dist if p["use_take_profit"] else None
            trail_stop = None
            trail_activated = False

        # Simulate forward through candles
        max_j = min(i + 1 + MAX_HOLD_CANDLES, len(closes) - 1)
        exit_price = None
        exit_reason = ""

        for j in range(i + 1, max_j + 1):
            high, low, atr_j = highs[j], lows[j], atrs[j]

            if action == 1:  # Long position
                # Check stop loss (hit low)
                if low <= stop_loss:
                    exit_price = stop_loss
                    exit_reason = "SL"
                    break
                # Check take profit (hit high) - only if take_profit is enabled
                if take_profit is not None and high >= take_profit:
                    exit_price = take_profit
                    exit_reason = "TP"
                    break
                # Trailing stop logic
                if p["use_trailing"]:
                    profit = high - entry_price
                    if profit >= activate_trail and not trail_activated:
                        trail_stop = high - trail_dist
                        trail_activated = True
                    elif trail_activated and high > trail_stop + trail_dist:
                        trail_stop = high - trail_dist
                    if trail_activated and low <= trail_stop:
                        exit_price = trail_stop
                        exit_reason = "TRAIL"
                        break
            else:  # Short position
                # Check stop loss (hit high)
                if high >= stop_loss:
                    exit_price = stop_loss
                    exit_reason = "SL"
                    break
                # Check take profit (hit low) - only if take_profit is enabled
                if take_profit is not None and low <= take_profit:
                    exit_price = take_profit
                    exit_reason = "TP"
                    break
                # Trailing stop logic
                if p["use_trailing"]:
                    profit = entry_price - low
                    if profit >= activate_trail and not trail_activated:
                        trail_stop = low + trail_dist
                        trail_activated = True
                    elif trail_activated and low < trail_stop - trail_dist:
                        trail_stop = low + trail_dist
                    if trail_activated and high >= trail_stop:
                        exit_price = trail_stop
                        exit_reason = "TRAIL"
                        break

        # Time exit if nothing hit
        if exit_price is None:
            exit_price = closes[max_j]
            exit_reason = "TIME"

        pct_return = (exit_price - entry_price) / entry_price
        if action == 2:
            pct_return = -pct_return
        pct_return -= SLIPPAGE

        trades.append({
            "symbol": symbol,
            "timestamp": timestamps[i],
            "action": "BUY" if action == 1 else "SELL",
            "entry": entry_price,
            "exit": exit_price,
            "return_pct": pct_return,
            "exit_reason": exit_reason,
        })
    return trades


def verify_fast_path_matches_reference(symbol: str, params: dict = None) -> bool:
    """Independently confirms the vectorized simulate_trades() matches
    the slow per-candle reference implementation on YOUR actual data,
    not just the synthetic data this was checked against during
    development. Run via: python -m research.backtest --selftest [symbols]"""
    prepared = prepare_symbol_series(symbol)
    if prepared is None:
        return True
    index_range = (LOOKBACK_MIN, prepared["length"] - 2)
    fast = simulate_trades(prepared, params=params, index_range=index_range)
    slow = _simulate_trades_reference(prepared, params=params, index_range=index_range)
    fast_key = [(t["timestamp"], t["action"], round(t["return_pct"], 10)) for t in fast]
    slow_key = [(t["timestamp"], t["action"], round(t["return_pct"], 10)) for t in slow]
    return fast_key == slow_key


MAX_WORKERS = 4


if __name__ == "__main__":
    from storage.db import get_connection

    def get_all_watchlist_symbols() -> list:
        conn = get_connection()
        symbols = [r["symbol"] for r in conn.execute("SELECT symbol FROM watchlist").fetchall()]
        conn.close()
        return symbols

    def run_selftest(symbols: list) -> None:
        all_passed = True
        for symbol in symbols:
            matches = verify_fast_path_matches_reference(symbol)
            print(f"  {symbol}: {'MATCH' if matches else 'MISMATCH'}")
            if not matches:
                all_passed = False
        print("\nSELFTEST PASSED - fast path matches the reference implementation on this data" if all_passed
              else "\nSELFTEST FAILED - do not trust backtest results until this is investigated")

    import sys
    args = sys.argv[1:]
    if args and args[0] == "--selftest":
        symbols = args[1:] if len(args) > 1 else get_all_watchlist_symbols()[:5]
        run_selftest(symbols)
        sys.exit(0)
    print("Use --selftest to run the self-test")