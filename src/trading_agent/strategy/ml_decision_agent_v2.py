"""
Loads the enhanced ML model trained by train_ml_model_v2.py and predicts on the
LATEST candle for live/parallel use. Uses walk-forward validated model with
session/regime features and confidence thresholding.

Usage: python -m trading_agent.strategy.ml_decision_agent_v2
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from datetime import UTC, datetime

from trading_agent.core.logging_config import get_logger
from trading_agent.core.pattern_detection import compute_pattern_bias
from trading_agent.research.walk_forward_optimizer import precompute_pattern_bias_codes
from trading_agent.core.signals import prepare_symbol_data
from trading_agent.ingest.watchlist_resolved import get_watchlist_symbols
from trading_agent.paths import ML_MODEL_PATH
from trading_agent.storage.parquet_store import get_parquet_store
from trading_agent.strategy.candidates import CandidateGenerator
from trading_agent.strategy.session_features import build as build_session_features

logger = get_logger(__name__)

MODEL_PATH = ML_MODEL_PATH
LABEL_NAMES = {0: "HOLD", 1: "BUY", 2: "SELL"}

# Skip symbols whose latest candle is older than this many minutes
_STALE_MINUTES = 30


def _load_model():
    """Load the joblib-bundled sklearn model and metadata."""
    import joblib

    if not MODEL_PATH.exists():
        raise FileNotFoundError(
            f"No trained model found at {MODEL_PATH}. "
            f"Run: python -m trading_agent.strategy.train_ml_model_v2 --all"
        )
    bundle = joblib.load(MODEL_PATH)
    return (
        bundle["model"],
        bundle["feature_columns"],
        bundle.get("confidence_threshold", 0.55),
        bundle.get("per_symbol_thresholds", {}),
    )


def _is_stale(parquet, symbol: str) -> bool:
    """Return True if the symbol's latest candle is older than _STALE_MINUTES."""
    df = parquet.read_latest(symbol, interval="minute", n=1)
    if df is None or df.empty:
        return True
    try:
        latest = pd.to_datetime(df["timestamp"].iloc[-1], utc=True)
        age_min = (datetime.now(UTC) - latest).total_seconds() / 60
        return age_min > _STALE_MINUTES
    except Exception:
        return True


def build_latest_enhanced_features(symbol: str) -> dict | None:
    """Build enhanced features including session/regime features for the latest candle.

    Loads candles from the Parquet store, computes indicators with the current
    prepare_symbol_data API, and returns a flat feature dict for the newest bar.
    Returns None if data is insufficient or any required feature is NaN.
    """
    from trading_agent.core.quant_indicators import atr as compute_atr_arr

    parquet = get_parquet_store()
    raw_df = parquet.read_latest(symbol, interval="minute", n=20000)
    if raw_df is None or raw_df.empty or len(raw_df) < 50:
        return None

    # Prepare indicator arrays using the current package API
    prepared = prepare_symbol_data(raw_df)
    if prepared is None or prepared["length"] < 25:
        return None

    sf = build_session_features(prepared)
    pattern_codes = precompute_pattern_bias_codes(prepared)

    # Compute ATR directly from raw OHLC arrays
    h = raw_df["high"].to_numpy(dtype=float)
    l_ = raw_df["low"].to_numpy(dtype=float)
    c = raw_df["close"].to_numpy(dtype=float)
    atr_vals = compute_atr_arr(h, l_, c, period=14)

    i = prepared["length"] - 1
    closes = prepared["closes"]
    if i < 20 or np.isnan(prepared["rsis"][i]) or np.isnan(prepared["adxs"][i]):
        return None

    volume = raw_df["volume"].to_numpy(dtype=float)
    volume_ma20 = volume[max(0, i - 19): i + 1].mean() if i >= 19 else np.nan
    volume_ratio = volume[i] / volume_ma20 if volume_ma20 and volume_ma20 > 0 else np.nan

    ma20 = closes[max(0, i - 19): i + 1].mean() if i >= 19 else np.nan
    dist_from_ma20 = (closes[i] - ma20) / ma20 if ma20 and ma20 > 0 else np.nan

    features: dict = {
        "rsi": prepared["rsis"][i],
        "adx": prepared["adxs"][i],
        "atr_pct": atr_vals[i] / closes[i] if closes[i] else np.nan,
        "pattern_code": float(pattern_codes[i]),
        "return_1": (closes[i] - closes[i - 1]) / closes[i - 1] if i >= 1 else np.nan,
        "return_3": (closes[i] - closes[i - 3]) / closes[i - 3] if i >= 3 else np.nan,
        "return_6": (closes[i] - closes[i - 6]) / closes[i - 6] if i >= 6 else np.nan,
        "volume_ratio": volume_ratio,
        "dist_from_ma20": dist_from_ma20,
    }

    # Session/regime features — arrays aligned to prepared["length"]
    features["minute_of_day"] = sf["minute_of_day"][i]
    features["atr_pctile"] = sf["atr_pctile"][i]
    features["dist_vwap"] = sf["dist_vwap"][i]
    features["vwap_slope"] = sf["vwap_slope"][i]
    features["volume_z"] = sf["volume_z"][i]
    features["htf_fast_ret"] = sf["htf_fast_ret"][i]
    features["htf_slow_ret"] = sf["htf_slow_ret"][i]
    features["expected_move"] = sf["expected_move"][i]
    features["gap_pct"] = sf["gap_pct"][i] if np.isfinite(sf["gap_pct"][i]) else 0.0
    features["bars_left_in_day"] = sf["bars_left_in_day"][i]

    required = [
        "rsi", "adx", "atr_pct", "pattern_code",
        "return_1", "return_3", "return_6",
        "volume_ratio", "dist_from_ma20",
        "minute_of_day", "atr_pctile", "dist_vwap",
        "vwap_slope", "volume_z", "htf_fast_ret", "htf_slow_ret",
        "expected_move", "bars_left_in_day",
    ]
    if any(
        v is None or (isinstance(v, float) and np.isnan(v))
        for v in [features.get(k) for k in required]
    ):
        return None

    return features


def ml_decide(
    symbol: str,
    model,
    feature_columns: list[str],
    confidence_threshold: float,
) -> dict:
    """Run inference for a single symbol and return an action dict."""
    features = build_latest_enhanced_features(symbol)
    if features is None:
        return {"symbol": symbol, "action": "HOLD", "confidence": 0.0, "below_threshold": True}

    X = pd.DataFrame(
        [[features.get(col, np.nan) for col in feature_columns]],
        columns=feature_columns,
    )
    predicted_class = int(model.predict(X)[0])
    probabilities = model.predict_proba(X)[0]
    confidence = float(probabilities[predicted_class])

    if confidence < confidence_threshold:
        return {
            "symbol": symbol,
            "action": "HOLD",
            "confidence": round(confidence, 3),
            "below_threshold": True,
            "raw_action": LABEL_NAMES[predicted_class],
        }

    return {
        "symbol": symbol,
        "action": LABEL_NAMES[predicted_class],
        "confidence": round(confidence, 3),
        "below_threshold": False,
    }


async def run_ml_decision_cycle(symbols: list[str] | None = None) -> None:
    """Run the enhanced ML v2 decision cycle for all supplied symbols.

    Async to match the scheduler's ``asyncio.run()`` wrapper. Results are
    written to the ``signals`` table (ORM-managed) with ``strategy='ml_v2'``.
    Symbols with no candles or a stale feed are silently skipped.
    """
    from trading_agent.storage.db import Signal, get_database

    if symbols is None:
        symbols = get_watchlist_symbols()

    if not symbols:
        logger.warning("run_ml_decision_cycle: no symbols")
        return

    try:
        model, feature_columns, global_threshold, per_symbol_thresholds = _load_model()
    except FileNotFoundError as exc:
        logger.warning("ml_v2_model_not_found", error=str(exc))
        return

    logger.info("ml_v2_cycle_start", symbols=len(symbols), threshold=global_threshold)

    parquet = get_parquet_store()
    db = get_database()

    for symbol in symbols:
        try:
            if _is_stale(parquet, symbol):
                logger.debug("ml_v2_stale_skip", symbol=symbol)
                continue

            threshold = per_symbol_thresholds.get(symbol, global_threshold)
            ml_result = ml_decide(symbol, model, feature_columns, threshold)

            logger.info(
                "ml_v2_decision",
                symbol=symbol,
                action=ml_result["action"],
                confidence=ml_result.get("confidence"),
                below_threshold=ml_result.get("below_threshold"),
            )

            # Persist to the ORM-managed signals table
            async with db.session() as session:
                signal = Signal(
                    symbol=symbol,
                    timestamp=datetime.now(UTC),
                    interval="minute",
                    action=ml_result["action"],
                    strategy="ml_v2",
                    confidence=ml_result.get("confidence", 0.0),
                )
                session.add(signal)
                await session.commit()

        except Exception as exc:
            logger.error("ml_v2_decision_failed", symbol=symbol, error=str(exc))


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_ml_decision_cycle(get_watchlist_symbols()))
