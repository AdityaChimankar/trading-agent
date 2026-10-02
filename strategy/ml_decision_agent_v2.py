"""
Loads the enhanced ML model trained by train_ml_model_v2.py and predicts on the
LATEST candle for live/parallel use. Uses walk-forward validated model with
session/regime features and confidence thresholding.

Usage: python -m strategy.ml_decision_agent_v2
"""
import joblib
import numpy as np
import pandas as pd
from datetime import datetime

from core.freshness import split_stale, warn_stale
from core.quant_indicators import compute_atr, load_candles
from core.signals import prepare_symbol_series, precompute_pattern_bias_codes
from paths import ML_MODEL_PATH
from storage.db import get_connection, get_watchlist_symbols
from strategy.decision_agent import decide as rule_based_decide
from strategy.session_features import features as session_features, build as build_session_features

MODEL_PATH = ML_MODEL_PATH
LABEL_NAMES = {0: "HOLD", 1: "BUY", 2: "SELL"}


def _load_model():
    if not MODEL_PATH.exists():
        raise FileNotFoundError(
            f"No trained model found at {MODEL_PATH}. Run strategy.train_ml_model_v2 first, "
            f"e.g.: python -m strategy.train_ml_model_v2 --all"
        )
    bundle = joblib.load(MODEL_PATH)
    return (
        bundle["model"],
        bundle["feature_columns"],
        bundle.get("confidence_threshold", 0.55),
        bundle.get("per_symbol_thresholds", {})
    )


def build_latest_enhanced_features(symbol: str) -> dict | None:
    """Builds enhanced features including session/regime features for the latest candle."""
    prepared = prepare_symbol_series(symbol)
    if prepared is None or prepared["length"] < 25:
        return None

    # Build session features for the full prepared data
    sf = build_session_features(prepared)

    pattern_codes = precompute_pattern_bias_codes(prepared)
    raw_df = load_candles(symbol, limit=20000)
    atr_vals = compute_atr(raw_df).values

    i = prepared["length"] - 1
    closes = prepared["closes"]
    if i < 20 or np.isnan(prepared["rsis"][i]) or np.isnan(prepared["adxs"][i]):
        return None

    volume = raw_df["volume"].values.astype(float)
    volume_ma20 = volume[max(0, i - 19): i + 1].mean() if i >= 19 else np.nan
    volume_ratio = volume[i] / volume_ma20 if volume_ma20 and volume_ma20 > 0 else np.nan

    ma20 = closes[max(0, i - 19): i + 1].mean() if i >= 19 else np.nan
    dist_from_ma20 = (closes[i] - ma20) / ma20 if ma20 and ma20 > 0 else np.nan

    # Base features (same as ml_features.py)
    features = {
        "rsi": prepared["rsis"][i], "adx": prepared["adxs"][i],
        "atr_pct": atr_vals[i] / closes[i] if closes[i] else np.nan,
        "pattern_code": pattern_codes[i],
        "return_1": (closes[i] - closes[i-1]) / closes[i-1] if i >= 1 else np.nan,
        "return_3": (closes[i] - closes[i-3]) / closes[i-3] if i >= 3 else np.nan,
        "return_6": (closes[i] - closes[i-6]) / closes[i-6] if i >= 6 else np.nan,
        "volume_ratio": volume_ratio, "dist_from_ma20": dist_from_ma20,
    }

    # Session/regime features (from session_features.py)
    # These are arrays aligned to prepared["length"]
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

    # Check for NaN in required features
    required = [
        "rsi", "adx", "atr_pct", "pattern_code", "return_1", "return_3", "return_6",
        "volume_ratio", "dist_from_ma20", "minute_of_day", "atr_pctile", "dist_vwap",
        "vwap_slope", "volume_z", "htf_fast_ret", "htf_slow_ret", "expected_move",
        "bars_left_in_day"
    ]
    if any(v is None or (isinstance(v, float) and np.isnan(v)) for v in [features.get(k) for k in required]):
        return None

    return features


def ml_decide(symbol: str, model, feature_columns: list, confidence_threshold: float) -> dict:
    features = build_latest_enhanced_features(symbol)
    if features is None:
        return {"symbol": symbol, "action": "HOLD", "confidence": 0.0, "below_threshold": True}

    X = pd.DataFrame([[features[col] for col in feature_columns]], columns=feature_columns)
    predicted_class = model.predict(X)[0]
    probabilities = model.predict_proba(X)[0]
    confidence = float(probabilities[predicted_class])

    # Apply confidence threshold - only act if confident enough
    if confidence < confidence_threshold:
        return {
            "symbol": symbol, "action": "HOLD", "confidence": round(confidence, 3),
            "below_threshold": True, "raw_action": LABEL_NAMES[predicted_class]
        }

    return {"symbol": symbol, "action": LABEL_NAMES[predicted_class], "confidence": round(confidence, 3), "below_threshold": False}


def run_ml_decision_cycle(symbols: list):
    model, feature_columns, global_threshold, per_symbol_thresholds = _load_model()

    print(f"Using global confidence threshold: {global_threshold:.2f}")
    print(f"Per-symbol thresholds: {per_symbol_thresholds}")

    # Same freshness gate as the rule-based cycle (core/freshness.py)
    symbols, stale = split_stale(symbols)
    warn_stale(stale, "ML cycle")

    for symbol in symbols:
        threshold = per_symbol_thresholds.get(symbol, global_threshold)
        ml_result = ml_decide(symbol, model, feature_columns, threshold)
        rule_result = rule_based_decide(symbol)
        # Same candle-aligned timestamp reasoning as decision_agent.py
        timestamp = rule_result.get("signal_timestamp") or datetime.now().isoformat()

        conn = get_connection()
        conn.execute(
            "INSERT INTO ml_signals (symbol, timestamp, action, confidence, rule_based_action) "
            "VALUES (?, ?, ?, ?, ?)",
            (symbol, timestamp, ml_result["action"], ml_result.get("confidence"), rule_result["action"]),
        )
        conn.commit()
        conn.close()

        threshold_note = " [LOW CONF]" if ml_result.get("below_threshold") else ""
        raw_action = f" ({ml_result.get('raw_action', '')})" if ml_result.get("below_threshold") else ""
        agree = "AGREE" if ml_result["action"] == rule_result["action"] else "DIFFER"
        print(f"{symbol}: ML={ml_result['action']}{raw_action} (conf {ml_result.get('confidence')}){threshold_note} "
              f"| Rules={rule_result['action']} [{agree}] (thresh={threshold:.2f})")


if __name__ == "__main__":
    run_ml_decision_cycle(get_watchlist_symbols())