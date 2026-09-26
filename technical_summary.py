"""
Aggregates multiple technical indicators into a Bearish/Neutral/Bullish
vote count and an overall verdict - the same style as Groww's
"Technicals" tab ("Based on technicals, this stock is Moderately
bullish", with a Bearish/Neutral/Bullish indicator count).

IMPORTANT - what this is and isn't: this is a SCANNABLE SUMMARY, not a
validated trading signal. It's an equal-weighted vote across common
indicators with no backtesting behind the specific thresholds or
weights - unlike decision_agent.py, which IS backtested and
walk-forward validated. Treat this the same way llm_decision_agent.py
and ml_decision_agent.py are treated: a comparison view that sits
alongside the tested signal, never something that overrides it.

Two DIFFERENT conventions are deliberately mixed here, both genuinely
common in retail technical analysis - see each indicator's docstring
in quant_indicators.py:
  - REVERSAL framing (RSI, Stochastic, Williams %R, Bollinger position):
    an extreme reading suggests a reversal is due - oversold = bullish,
    overbought = bearish.
  - MOMENTUM/CONTINUATION framing (CCI, MACD, +DI/-DI, moving average
    position): an extreme or directional reading suggests the move
    continues - strong up momentum = bullish.
This isn't inconsistency to "fix" - both conventions are standard and
this mirrors how platforms like Groww present them side by side.
"""
from dataclasses import dataclass
import pandas as pd
from quant_indicators import (
    load_candles, compute_rsi, compute_macd, compute_stochastic,
    compute_williams_r, compute_cci, compute_di, compute_adx,
)

# Verdict thresholds, as (min_net_score, label) - net_score = bullish_count - bearish_count.
# Tunable: widen/narrow these bands to change how easily a "Strongly"
# label triggers relative to your indicator count.
VERDICT_BANDS = [
    (5, "Strongly bullish"), (2, "Moderately bullish"),
    (-1, "Neutral"),
    (-4, "Moderately bearish"), (float("-inf"), "Strongly bearish"),
]


@dataclass
class IndicatorReading:
    name: str
    value: float
    verdict: str  # "Bullish", "Bearish", or "Neutral"


def _verdict_label(net_score: int) -> str:
    for threshold, label in VERDICT_BANDS:
        if net_score >= threshold:
            return label
    return "Neutral"  # unreachable given the -inf band, kept as a safe fallback


def get_technical_summary(symbol: str, candle_limit: int = 200) -> dict | None:
    df = load_candles(symbol, limit=candle_limit)
    if len(df) < 30:
        return None

    readings = []

    # --- RSI - reversal framing ---
    rsi = compute_rsi(df).iloc[-1]
    if pd.notna(rsi):
        verdict = "Bullish" if rsi < 30 else "Bearish" if rsi > 70 else "Neutral"
        readings.append(IndicatorReading("RSI (14)", round(rsi, 2), verdict))

    # --- MACD histogram - momentum framing ---
    _, _, hist = compute_macd(df)
    hist_val = hist.iloc[-1]
    if pd.notna(hist_val):
        verdict = "Bullish" if hist_val > 0 else "Bearish" if hist_val < 0 else "Neutral"
        readings.append(IndicatorReading("MACD (12,26,9)", round(hist_val, 2), verdict))

    # --- Stochastic %K - reversal framing ---
    k, _ = compute_stochastic(df)
    k_val = k.iloc[-1]
    if pd.notna(k_val):
        verdict = "Bullish" if k_val < 20 else "Bearish" if k_val > 80 else "Neutral"
        readings.append(IndicatorReading("Stochastic %K", round(k_val, 2), verdict))

    # --- Williams %R - reversal framing ---
    wr = compute_williams_r(df).iloc[-1]
    if pd.notna(wr):
        verdict = "Bullish" if wr < -80 else "Bearish" if wr > -20 else "Neutral"
        readings.append(IndicatorReading("Williams %R", round(wr, 2), verdict))

    # --- CCI - momentum framing ---
    cci = compute_cci(df).iloc[-1]
    if pd.notna(cci):
        verdict = "Bullish" if cci > 100 else "Bearish" if cci < -100 else "Neutral"
        readings.append(IndicatorReading("CCI (20)", round(cci, 2), verdict))

    # --- +DI/-DI direction, gated by ADX trend strength - momentum framing ---
    plus_di, minus_di = compute_di(df)
    adx = compute_adx(df).iloc[-1]
    plus_val, minus_val = plus_di.iloc[-1], minus_di.iloc[-1]
    if pd.notna(plus_val) and pd.notna(minus_val) and pd.notna(adx):
        if adx < 20:
            verdict = "Neutral"  # no confirmed trend - direction isn't trustworthy
        else:
            verdict = "Bullish" if plus_val > minus_val else "Bearish"
        readings.append(IndicatorReading("ADX/DI (14)", round(adx, 2), verdict))

    # --- Moving average position - momentum framing ---
    close = df["close"]
    for period in (20, 50):
        ma = close.rolling(period).mean().iloc[-1]
        if pd.notna(ma):
            verdict = "Bullish" if close.iloc[-1] > ma else "Bearish"
            readings.append(IndicatorReading(f"Price vs MA{period}", round(ma, 2), verdict))

    ma20 = close.rolling(20).mean().iloc[-1]
    ma50 = close.rolling(50).mean().iloc[-1]
    if pd.notna(ma20) and pd.notna(ma50):
        verdict = "Bullish" if ma20 > ma50 else "Bearish"
        readings.append(IndicatorReading("MA20 vs MA50", round(ma20 - ma50, 2), verdict))

    if not readings:
        return None

    bullish_count = sum(1 for r in readings if r.verdict == "Bullish")
    bearish_count = sum(1 for r in readings if r.verdict == "Bearish")
    neutral_count = sum(1 for r in readings if r.verdict == "Neutral")
    net_score = bullish_count - bearish_count

    return {
        "symbol": symbol, "readings": readings,
        "bullish_count": bullish_count, "bearish_count": bearish_count,
        "neutral_count": neutral_count, "total": len(readings),
        "verdict": _verdict_label(net_score),
    }


if __name__ == "__main__":
    import sys
    symbol = sys.argv[1] if len(sys.argv) > 1 else "RELIANCE"
    summary = get_technical_summary(symbol)
    if summary is None:
        print(f"Not enough data for {symbol}")
    else:
        print(f"{symbol}: {summary['verdict']} "
              f"(Bearish {summary['bearish_count']} / Neutral {summary['neutral_count']} / Bullish {summary['bullish_count']})")
        for r in summary["readings"]:
            print(f"  {r.name}: {r.value} -> {r.verdict}")
