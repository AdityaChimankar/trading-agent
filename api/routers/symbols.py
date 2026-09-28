"""Per-symbol endpoints: chart data, patterns, sizing, news, signal logs.

Each endpoint maps to one panel of the old Streamlit symbol view, kept
separate so the front end can poll them on different TTLs (the chart
refreshes fast, the news table doesn't need to).
"""
from fastapi import APIRouter, HTTPException, Query

from api.serializers import jsonable
from db import get_connection, get_watchlist_symbols
from quant_indicators import compute_adx, compute_atr, compute_rsi, load_candles
from pattern_detection import compute_pattern_columns, detect_patterns

router = APIRouter(prefix="/api/symbols", tags=["symbols"])


def _require_symbol(symbol: str) -> None:
    if symbol not in set(get_watchlist_symbols()):
        raise HTTPException(status_code=404, detail=f"{symbol} is not in the watchlist.")


@router.get("/{symbol}/chart")
def chart(symbol: str, limit: int = Query(200, ge=20, le=2000)):
    """Candles + indicators + pattern flags + historical BUY/SELL markers.

    Returns parallel arrays (one entry per candle) so the front end can
    build the four synced Plotly panels directly without reshaping.
    """
    _require_symbol(symbol)

    df = load_candles(symbol, limit=limit)
    if len(df) < 5:
        return jsonable({"symbol": symbol, "insufficient_data": True, "candles": None})

    df["rsi"] = compute_rsi(df)
    df["atr"] = compute_atr(df)
    df["adx"] = compute_adx(df)
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma50"] = df["close"].rolling(50).mean()
    pattern_df = compute_pattern_columns(df)

    conn = get_connection()
    markers = conn.execute(
        "SELECT timestamp, action FROM signals "
        "WHERE symbol = ? AND action != 'HOLD' AND timestamp >= ? ORDER BY timestamp",
        (symbol, df["timestamp"].min()),
    ).fetchall()
    conn.close()

    last = df.iloc[-1]
    return jsonable({
        "symbol": symbol,
        "insufficient_data": False,
        "candles": {
            "timestamp": df["timestamp"].tolist(),
            "open": df["open"].tolist(),
            "high": df["high"].tolist(),
            "low": df["low"].tolist(),
            "close": df["close"].tolist(),
            "volume": df["volume"].tolist(),
        },
        "indicators": {
            "rsi": df["rsi"].tolist(),
            "adx": df["adx"].tolist(),
            "atr": df["atr"].tolist(),
            "ma20": df["ma20"].tolist(),
            "ma50": df["ma50"].tolist(),
        },
        "patterns": {
            name: pattern_df[name].tolist()
            for name in ("doji", "hammer", "bullish_engulfing", "bearish_engulfing")
        },
        "signal_markers": [dict(m) for m in markers],
        "latest": {"close": last["close"], "rsi": last["rsi"], "adx": last["adx"], "atr": last["atr"]},
    })


@router.get("/{symbol}/patterns")
def patterns(symbol: str):
    _require_symbol(symbol)
    return jsonable(detect_patterns(symbol))


@router.get("/{symbol}/sizing")
def sizing(symbol: str, capital: float = Query(100000.0, gt=0)):
    """Portfolio-adjusted position plan for this symbol's latest rule signal."""
    _require_symbol(symbol)

    conn = get_connection()
    latest = conn.execute(
        "SELECT action FROM signals WHERE symbol = ? ORDER BY timestamp DESC LIMIT 1", (symbol,)
    ).fetchone()
    conn.close()

    if latest is None or latest["action"] == "HOLD":
        return jsonable({"symbol": symbol, "has_signal": False,
                         "action": latest["action"] if latest else None, "adjusted": None})

    from portfolio_risk import calculate_portfolio_adjusted_position

    adjusted = calculate_portfolio_adjusted_position(symbol, latest["action"], capital)
    return jsonable({"symbol": symbol, "has_signal": True, "action": latest["action"],
                     "adjusted": adjusted})


@router.get("/{symbol}/news")
def news(symbol: str, limit: int = Query(10, ge=1, le=50)):
    _require_symbol(symbol)

    conn = get_connection()
    rows = conn.execute(
        """SELECT n.headline, n.published_at, n.source, s.score, s.rationale
           FROM news n LEFT JOIN sentiment s ON n.id = s.news_id
           WHERE n.symbol = ? ORDER BY n.published_at DESC LIMIT ?""",
        (symbol, limit),
    ).fetchall()
    conn.close()
    return jsonable([dict(r) for r in rows])


@router.get("/{symbol}/signals")
def signals(symbol: str, limit: int = Query(20, ge=1, le=200)):
    _require_symbol(symbol)

    conn = get_connection()
    rows = conn.execute(
        """SELECT timestamp, action, rsi, adx, sentiment_score, rationale
           FROM signals WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?""",
        (symbol, limit),
    ).fetchall()
    conn.close()
    return jsonable([dict(r) for r in rows])


@router.get("/{symbol}/llm-signals")
def llm_signals(symbol: str, limit: int = Query(20, ge=1, le=200)):
    _require_symbol(symbol)

    conn = get_connection()
    rows = conn.execute(
        """SELECT timestamp, action AS llm_action, rule_based_action, confidence, rationale
           FROM llm_signals WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?""",
        (symbol, limit),
    ).fetchall()
    conn.close()

    records = [dict(r) for r in rows]
    agreement = None
    if records:
        agree = sum(1 for r in records if r["llm_action"] == r["rule_based_action"])
        agreement = round(agree / len(records) * 100)
    return jsonable({"agreement_pct": agreement, "rows": records})


@router.get("/{symbol}/ml-signals")
def ml_signals(symbol: str, limit: int = Query(20, ge=1, le=200)):
    _require_symbol(symbol)

    conn = get_connection()
    rows = conn.execute(
        """SELECT timestamp, action AS ml_action, rule_based_action, confidence
           FROM ml_signals WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?""",
        (symbol, limit),
    ).fetchall()
    conn.close()

    records = [dict(r) for r in rows]
    agreement = None
    if records:
        agree = sum(1 for r in records if r["ml_action"] == r["rule_based_action"])
        agreement = round(agree / len(records) * 100)
    return jsonable({"agreement_pct": agreement, "rows": records})
