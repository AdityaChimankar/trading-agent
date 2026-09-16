"""
Loads the model trained by train_ml_model.py and predicts on the
LATEST candle for live/parallel use - same pattern as
llm_decision_agent.py: writes to a SEPARATE ml_signals table, logs the
rule-based agent's call at the same moment for comparison, and never
feeds into decision_agent.py's actual decision. Only trust this as a
real decision-maker once its comparison track record justifies it.

Usage: python ml_decision_agent.py
"""
import joblib
import numpy as np
from pathlib import Path
from datetime import datetime
from backtest import prepare_symbol_series
from walk_forward_optimizer import precompute_pattern_bias_codes
from quant_indicators import compute_atr, load_candles
from decision_agent import decide as rule_based_decide
from db import get_connection, get_watchlist_symbols

MODEL_PATH = Path(__file__).parent / "models" / "ml_model.joblib"
LABEL_NAMES = {0: "HOLD", 1: "BUY", 2: "SELL"}


def _load_model():
    if not MODEL_PATH.exists():
        raise FileNotFoundError(
            f"No trained model found at {MODEL_PATH}. Run train_ml_model.py first, "
            f"e.g.: python train_ml_model.py --all"
        )
    bundle = joblib.load(MODEL_PATH)
    return bundle["model"], bundle["feature_columns"]


def build_latest_features(symbol: str) -> dict | None:
    """Builds the same FEATURE_COLUMNS as ml_features.py, but only for
    the most recent candle - for live prediction, not training."""
    prepared = prepare_symbol_series(symbol)
    if prepared is None or prepared["length"] < 25:
        return None

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

    features = {
        "rsi": prepared["rsis"][i], "adx": prepared["adxs"][i],
        "atr_pct": atr_vals[i] / closes[i] if closes[i] else np.nan,
        "pattern_code": pattern_codes[i],
        "return_1": (closes[i] - closes[i-1]) / closes[i-1] if i >= 1 else np.nan,
        "return_3": (closes[i] - closes[i-3]) / closes[i-3] if i >= 3 else np.nan,
        "return_6": (closes[i] - closes[i-6]) / closes[i-6] if i >= 6 else np.nan,
        "volume_ratio": volume_ratio, "dist_from_ma20": dist_from_ma20,
    }

    if any(v is None or (isinstance(v, float) and np.isnan(v)) for v in features.values()):
        return None
    return features


def ml_decide(symbol: str, model, feature_columns: list) -> dict:
    features = build_latest_features(symbol)
    if features is None:
        return {"symbol": symbol, "action": "HOLD", "confidence": 0.0}

    import pandas as pd
    X = pd.DataFrame([[features[col] for col in feature_columns]], columns=feature_columns)
    predicted_class = model.predict(X)[0]
    probabilities = model.predict_proba(X)[0]
    confidence = float(probabilities[predicted_class])

    return {"symbol": symbol, "action": LABEL_NAMES[predicted_class], "confidence": round(confidence, 3)}


def run_ml_decision_cycle(symbols: list):
    model, feature_columns = _load_model()
    timestamp = datetime.now().isoformat()

    for symbol in symbols:
        ml_result = ml_decide(symbol, model, feature_columns)
        rule_result = rule_based_decide(symbol)

        conn = get_connection()
        conn.execute(
            "INSERT INTO ml_signals (symbol, timestamp, action, confidence, rule_based_action) "
            "VALUES (?, ?, ?, ?, ?)",
            (symbol, timestamp, ml_result["action"], ml_result.get("confidence"), rule_result["action"]),
        )
        conn.commit()
        conn.close()

        agree = "AGREE" if ml_result["action"] == rule_result["action"] else "DIFFER"
        print(f"{symbol}: ML={ml_result['action']} (conf {ml_result.get('confidence')}) "
              f"| Rules={rule_result['action']} [{agree}]")


if __name__ == "__main__":
    run_ml_decision_cycle(get_watchlist_symbols())
