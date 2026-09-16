"""
Feature engineering for the ML signal layer. Reuses the SAME
precomputed RSI/ADX/pattern arrays from prepare_symbol_series() and
precompute_pattern_bias_codes() that backtest.py and
walk_forward_optimizer.py already use - one consistent set of
indicators across the whole project, not a separate parallel
computation that could quietly drift out of sync.

Every feature at row i uses ONLY data up to and including candle i -
this is what makes the eventual train/test split meaningful. The
label (what we're trying to predict) is the only thing that looks
forward, and it's kept completely separate from the feature columns.
"""
import numpy as np
import pandas as pd
from backtest import prepare_symbol_series, FORWARD_WINDOW
from walk_forward_optimizer import precompute_pattern_bias_codes

# Forward-return threshold that defines an "UP" or "DOWN" label vs FLAT.
# Intentionally not zero - a +0.01% move over 30 min is noise, not a
# real signal worth training the model to chase.
LABEL_THRESHOLD = 0.0015  # 0.15%

FEATURE_COLUMNS = [
    "rsi", "adx", "atr_pct", "pattern_code",
    "return_1", "return_3", "return_6",
    "volume_ratio", "dist_from_ma20",
]


def build_features(symbol: str, candle_limit: int = 20000) -> pd.DataFrame | None:
    """Returns a DataFrame with one row per candle: FEATURE_COLUMNS
    plus 'label' (0=FLAT/HOLD, 1=UP/BUY, 2=DOWN/SELL) and 'timestamp'.
    Rows where indicators or the label aren't yet defined (start/end
    of series) are dropped.
    """
    prepared = prepare_symbol_series(symbol, candle_limit)
    if prepared is None:
        return None

    n = prepared["length"]
    closes = prepared["closes"]
    rsis = prepared["rsis"]
    adxs = prepared["adxs"]
    timestamps = prepared["timestamps"]
    pattern_codes = precompute_pattern_bias_codes(prepared)

    # ATR as % of price, not absolute rupees - makes it comparable
    # across stocks at very different price levels (e.g. a Rs.50 stock
    # vs a Rs.5000 stock). prepare_symbol_series() doesn't retain
    # high/low arrays (only what backtest.py/walk_forward_optimizer.py
    # need), so ATR and volume are recomputed here from a fresh load
    # rather than threading new fields through that shared function and
    # risking breaking its existing, tested consumers.
    from quant_indicators import compute_atr, load_candles
    raw_df = load_candles(symbol, limit=candle_limit)
    atr_vals = compute_atr(raw_df).values
    atr_pct = atr_vals / closes

    return_1 = np.concatenate([[np.nan], (closes[1:] - closes[:-1]) / closes[:-1]])
    return_3 = np.full(n, np.nan)
    return_3[3:] = (closes[3:] - closes[:-3]) / closes[:-3]
    return_6 = np.full(n, np.nan)
    return_6[6:] = (closes[6:] - closes[:-6]) / closes[:-6]

    volume = raw_df["volume"].values.astype(float)
    volume_ma20 = pd.Series(volume).rolling(20).mean().values
    with np.errstate(divide="ignore", invalid="ignore"):
        volume_ratio = np.where(volume_ma20 > 0, volume / volume_ma20, np.nan)

    ma20 = pd.Series(closes).rolling(20).mean().values
    with np.errstate(divide="ignore", invalid="ignore"):
        dist_from_ma20 = np.where(ma20 > 0, (closes - ma20) / ma20, np.nan)

    # --- Label: forward return over FORWARD_WINDOW candles (same
    # horizon backtest.py uses) - this is the ONLY forward-looking
    # data in this whole function, and it never becomes a feature. ---
    forward_return = np.full(n, np.nan)
    forward_return[: n - FORWARD_WINDOW] = (closes[FORWARD_WINDOW:] - closes[: n - FORWARD_WINDOW]) / closes[: n - FORWARD_WINDOW]
    label = np.zeros(n, dtype=np.int8)
    label[forward_return > LABEL_THRESHOLD] = 1   # UP
    label[forward_return < -LABEL_THRESHOLD] = 2  # DOWN

    df = pd.DataFrame({
        "timestamp": timestamps, "rsi": rsis, "adx": adxs, "atr_pct": atr_pct,
        "pattern_code": pattern_codes, "return_1": return_1, "return_3": return_3,
        "return_6": return_6, "volume_ratio": volume_ratio, "dist_from_ma20": dist_from_ma20,
        "label": label, "forward_return": forward_return,
    })

    # Drop rows where any feature or the label isn't defined yet
    # (indicator warm-up period at the start, forward-return horizon
    # at the end) - these can't be used for training or fair evaluation.
    df = df.dropna(subset=FEATURE_COLUMNS + ["forward_return"]).reset_index(drop=True)
    return df


if __name__ == "__main__":
    import sys
    symbol = sys.argv[1] if len(sys.argv) > 1 else "RELIANCE"
    df = build_features(symbol)
    if df is None:
        print(f"Not enough data for {symbol}")
    else:
        print(f"{symbol}: {len(df)} usable rows")
        print(df[FEATURE_COLUMNS + ["label"]].describe())
        print()
        print("Label distribution:")
        print(df["label"].value_counts().rename({0: "FLAT", 1: "UP", 2: "DOWN"}))
