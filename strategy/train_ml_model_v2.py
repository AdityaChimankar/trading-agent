"""
Improved ML Signal Model Training with Walk-Forward Validation.

Key improvements over train_ml_model.py:
1. Walk-forward validation (not just chronological split) - re-trains on rolling windows
2. Enhanced features from session_features.py (session window, VWAP, volatility regime, HTF agreement)
3. Better label definition using session-aware forward returns
4. Confidence thresholding - only act when model is confident
5. Class balancing with SMOTE or class weights
6. Per-symbol models with meta-learning across symbols
"""
import sys
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import accuracy_score, confusion_matrix, classification_report
from sklearn.model_selection import TimeSeriesSplit
from imblearn.over_sampling import SMOTE

from paths import ML_MODEL_PATH, MODEL_DIR
from storage.db import get_watchlist_symbols
from strategy.ml_features import build_features, FEATURE_COLUMNS
from strategy.session_features import features as session_features, build as build_session_features
from core.signals import FORWARD_WINDOW, SLIPPAGE, prepare_symbol_series

MODEL_PATH = ML_MODEL_PATH
WALK_FORWARD_TRAIN_CANDLES = 3750   # ~50 trading days
WALK_FORWARD_TEST_CANDLES = 750     # ~10 trading days
MIN_TRAIN_SAMPLES = 500

# Enhanced feature columns including session/regime features
# Note: gap_pct is only defined on first bar of day, so we don't require it
ENHANCED_FEATURE_COLUMNS = FEATURE_COLUMNS + [
    "minute_of_day", "atr_pctile", "dist_vwap", "vwap_slope",
    "volume_z", "htf_fast_ret", "htf_slow_ret", "expected_move",
    "gap_pct", "bars_left_in_day",
]

# Columns that must be non-NaN for training (gap_pct excluded - NaN means intraday, not a gap)
REQUIRED_FEATURE_COLUMNS = FEATURE_COLUMNS + [
    "minute_of_day", "atr_pctile", "dist_vwap", "vwap_slope",
    "volume_z", "htf_fast_ret", "htf_slow_ret", "expected_move",
    "bars_left_in_day",
]


def build_enhanced_features(symbol: str, candle_limit: int = 20000) -> pd.DataFrame | None:
    """Build enhanced features including session/regime features."""
    # Get raw prepared data first
    prepared = prepare_symbol_series(symbol, candle_limit)
    if prepared is None:
        return None

    # Build session features on full prepared data
    sf = build_session_features(prepared)

    # Build base features (which does its own NaN dropping)
    base_df = build_features(symbol, candle_limit)
    if base_df is None:
        return None

    # Align session features to base_df via timestamp merge
    # Create session feature DataFrame with timestamp
    sf_df = pd.DataFrame({
        "timestamp": prepared["timestamps"],
        "minute_of_day": sf["minute_of_day"],
        "atr_pctile": sf["atr_pctile"],
        "dist_vwap": sf["dist_vwap"],
        "vwap_slope": sf["vwap_slope"],
        "volume_z": sf["volume_z"],
        "htf_fast_ret": sf["htf_fast_ret"],
        "htf_slow_ret": sf["htf_slow_ret"],
        "expected_move": sf["expected_move"],
        "gap_pct": sf["gap_pct"],
        "bars_left_in_day": sf["bars_left_in_day"],
    })

    # Merge on timestamp
    base_df = base_df.merge(sf_df, on="timestamp", how="left")

    # Fill gap_pct NaN with 0 (no gap) - it's only defined on first bar of day
    if "gap_pct" in base_df.columns:
        base_df["gap_pct"] = base_df["gap_pct"].fillna(0)

    # Drop rows where any REQUIRED feature is NaN
    base_df = base_df.dropna(subset=REQUIRED_FEATURE_COLUMNS).reset_index(drop=True)

    return base_df


def create_labels_with_cost_awareness(df: pd.DataFrame, cost_multiple: float = 3.0) -> pd.DataFrame:
    """
    Create labels that account for trading costs.
    Only label as UP/DOWN if forward return exceeds cost_multiple * SLIPPAGE.
    """
    df = df.copy()
    
    # Dynamic threshold based on ATR (same as ml_features.py)
    dynamic_threshold = df["atr_pct"] * 0.5
    
    # Cost-aware threshold: must exceed both dynamic threshold AND cost multiple
    cost_threshold = cost_multiple * SLIPPAGE
    effective_threshold = np.maximum(dynamic_threshold, cost_threshold)
    
    fwd = df["forward_return"]
    label = np.zeros(len(df), dtype=np.int8)
    label[fwd > effective_threshold] = 1    # UP
    label[fwd < -effective_threshold] = 2   # DOWN
    
    df["label"] = label
    return df


def walk_forward_train(symbol: str, train_candles: int = WALK_FORWARD_TRAIN_CANDLES,
                       test_candles: int = WALK_FORWARD_TEST_CANDLES) -> dict:
    """
    Walk-forward training: re-train on rolling windows, test on next window.
    Returns aggregated results across all folds.
    """
    df = build_enhanced_features(symbol)
    if df is None or len(df) < train_candles + test_candles:
        return {"error": "not enough data"}

    n = len(df)
    fold_size = train_candles + test_candles
    n_folds = (n - train_candles) // test_candles
    
    if n_folds < 2:
        return {"error": "not enough folds"}

    all_predictions = []
    all_labels = []
    all_confidences = []
    fold_results = []

    for fold in range(n_folds):
        train_start = fold * test_candles
        train_end = train_start + train_candles
        test_start = train_end
        test_end = min(test_start + test_candles, n)

        train_df = df.iloc[train_start:train_end].copy()
        test_df = df.iloc[test_start:test_end].copy()

        if len(train_df) < MIN_TRAIN_SAMPLES or len(test_df) < 10:
            continue

        # Apply cost-aware labeling
        train_df = create_labels_with_cost_awareness(train_df)
        test_df = create_labels_with_cost_awareness(test_df)

        X_train = train_df[ENHANCED_FEATURE_COLUMNS]
        y_train = train_df["label"]
        X_test = test_df[ENHANCED_FEATURE_COLUMNS]
        y_test = test_df["label"]

        # Check class balance
        train_counts = y_train.value_counts()
        if len(train_counts) < 2:
            continue

        # Use class_weight='balanced' instead of SMOTE for time-series
        model = HistGradientBoostingClassifier(
            max_iter=300, max_depth=6, learning_rate=0.05,
            random_state=42, class_weight="balanced",
            early_stopping=True, validation_fraction=0.1,
        )
        model.fit(X_train, y_train)

        predictions = model.predict(X_test)
        probabilities = model.predict_proba(X_test)
        confidences = np.max(probabilities, axis=1)

        all_predictions.extend(predictions)
        all_labels.extend(y_test)
        all_confidences.extend(confidences)

        # Trade simulation for this fold
        fold_result = simulate_trades_from_predictions(test_df, predictions, confidences)
        fold_results.append(fold_result)

        print(f"  Fold {fold+1}/{n_folds}: train={len(train_df)}, test={len(test_df)}, "
              f"acc={accuracy_score(y_test, predictions):.3f}, trades={fold_result['trades']}")

    if not all_predictions:
        return {"error": "no valid folds"}

    # Aggregate results
    accuracy = accuracy_score(all_labels, all_predictions)
    cm = confusion_matrix(all_labels, all_predictions)
    cr = classification_report(all_labels, all_predictions, 
                               target_names=['FLAT', 'UP', 'DOWN'], zero_division=0)

    # Aggregate trade simulation
    agg_result = aggregate_trade_results(fold_results)

    return {
        "symbol": symbol,
        "n_folds": len(fold_results),
        "accuracy": accuracy,
        "confusion_matrix": cm,
        "classification_report": cr,
        "trade_simulation": agg_result,
        "avg_confidence": np.mean(all_confidences),
    }


def simulate_trades_from_predictions(test_df: pd.DataFrame, predictions: np.ndarray,
                                      confidences: np.ndarray, confidence_threshold: float = 0.55) -> dict:
    """
    Convert predictions to trades with confidence thresholding.
    Only trade when confidence > threshold.
    """
    # Apply confidence threshold
    trade_mask = (predictions != 0) & (confidences >= confidence_threshold)
    
    if trade_mask.sum() == 0:
        return {"trades": 0}

    forward_returns = test_df["forward_return"].values[trade_mask]
    preds = predictions[trade_mask]
    confs = confidences[trade_mask]

    # UP prediction (1) profits if price rose; DOWN prediction (2) profits if price fell
    pnl = np.where(preds == 1, forward_returns, -forward_returns)

    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    win_rate = len(wins) / len(pnl) * 100 if len(pnl) > 0 else 0
    cumulative = (1 + pd.Series(pnl)).cumprod()

    return {
        "trades": int(trade_mask.sum()),
        "win_rate": round(win_rate, 1),
        "avg_win_pct": round(wins.mean() * 100, 3) if len(wins) else 0.0,
        "avg_loss_pct": round(losses.mean() * 100, 3) if len(losses) else 0.0,
        "total_return_pct": round((cumulative.iloc[-1] - 1) * 100, 2),
        "avg_confidence": round(np.mean(confs), 3) if len(confs) > 0 else 0,
    }


def aggregate_trade_results(fold_results: list) -> dict:
    """Aggregate trade results across folds."""
    if not fold_results:
        return {"trades": 0}
    
    all_pnl = []
    for fr in fold_results:
        if fr.get("trades", 0) > 0:
            # We don't have per-trade PnL, so approximate from stats
            pass
    
    # Simple aggregation of metrics
    total_trades = sum(fr.get("trades", 0) for fr in fold_results)
    if total_trades == 0:
        return {"trades": 0}
    
    avg_win_rate = np.mean([fr.get("win_rate", 0) for fr in fold_results])
    avg_win = np.mean([fr.get("avg_win_pct", 0) for fr in fold_results])
    avg_loss = np.mean([fr.get("avg_loss_pct", 0) for fr in fold_results])
    total_return = sum(fr.get("total_return_pct", 0) for fr in fold_results)
    avg_conf = np.mean([fr.get("avg_confidence", 0) for fr in fold_results])
    
    return {
        "trades": total_trades,
        "win_rate": round(avg_win_rate, 1),
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(avg_loss, 3),
        "total_return_pct": round(total_return, 2),
        "avg_confidence": round(avg_conf, 3),
    }


def train_meta_model(symbols: list) -> dict:
    """
    Train a meta-model on pooled walk-forward results from multiple symbols.
    This learns which features generalize across symbols.
    """
    print(f"\n--- Training meta-model on {len(symbols)} symbols ---")
    
    all_train_dfs = []
    for symbol in symbols:
        df = build_enhanced_features(symbol)
        if df is not None and len(df) > 1000:
            df = create_labels_with_cost_awareness(df)
            all_train_dfs.append(df)
            print(f"  {symbol}: {len(df)} rows")
    
    if not all_train_dfs:
        return {"error": "no data"}
    
    pooled_df = pd.concat(all_train_dfs, ignore_index=True)
    print(f"Pooled: {len(pooled_df)} rows")
    print(f"Label distribution:\n{pooled_df['label'].value_counts().rename({0:'FLAT',1:'UP',2:'DOWN'})}")
    
    # Time-series split for final validation
    tscv = TimeSeriesSplit(n_splits=5)
    fold_accuracies = []
    
    for fold, (train_idx, test_idx) in enumerate(tscv.split(pooled_df)):
        train_df = pooled_df.iloc[train_idx]
        test_df = pooled_df.iloc[test_idx]
        
        X_train = train_df[ENHANCED_FEATURE_COLUMNS]
        y_train = train_df["label"]
        X_test = test_df[ENHANCED_FEATURE_COLUMNS]
        y_test = test_df["label"]
        
        model = HistGradientBoostingClassifier(
            max_iter=300, max_depth=6, learning_rate=0.05,
            random_state=42, class_weight="balanced",
            early_stopping=True, validation_fraction=0.1,
        )
        model.fit(X_train, y_train)
        
        predictions = model.predict(X_test)
        accuracy = accuracy_score(y_test, predictions)
        fold_accuracies.append(accuracy)
        print(f"  Fold {fold+1}: accuracy={accuracy:.3f}")
    
    # Train final model on all data
    X_all = pooled_df[ENHANCED_FEATURE_COLUMNS]
    y_all = pooled_df["label"]
    
    final_model = HistGradientBoostingClassifier(
        max_iter=300, max_depth=6, learning_rate=0.05,
        random_state=42, class_weight="balanced",
    )
    final_model.fit(X_all, y_all)
    
    # Feature importance using permutation importance (HistGradientBoostingClassifier doesn't have feature_importances_)
    from sklearn.inspection import permutation_importance
    perm_importance = permutation_importance(final_model, X_all, y_all, n_repeats=5, random_state=42)
    feature_importance = pd.DataFrame({
        "feature": ENHANCED_FEATURE_COLUMNS,
        "importance": perm_importance.importances_mean
    }).sort_values("importance", ascending=False)
    
    print(f"\nFeature Importance (top 15):")
    print(feature_importance.head(15).to_string(index=False))
    
    return {
        "model": final_model,
        "feature_importance": feature_importance,
        "cv_accuracies": fold_accuracies,
        "mean_cv_accuracy": np.mean(fold_accuracies),
    }


def find_per_symbol_thresholds(model, feature_columns: list, symbols: list) -> dict:
    """Find optimal confidence threshold per symbol using recent validation data."""
    thresholds = {}
    
    for symbol in symbols:
        df = build_enhanced_features(symbol)
        if df is None or len(df) < 1000:
            continue
        
        # Use last 20% as validation
        split = int(len(df) * 0.8)
        val_df = df.iloc[split:].copy()
        val_df = create_labels_with_cost_awareness(val_df)
        
        X_val = val_df[feature_columns]
        y_val = val_df["label"]
        
        probs = model.predict_proba(X_val)
        predictions = model.predict(X_val)
        confidences = np.max(probs, axis=1)
        
        best_threshold = 0.5
        best_score = -np.inf
        
        for threshold in np.arange(0.4, 0.85, 0.05):
            mask = (predictions != 0) & (confidences >= threshold)
            if mask.sum() < 10:
                continue
            
            fwd = val_df["forward_return"].values[mask]
            preds = predictions[mask]
            pnl = np.where(preds == 1, fwd, -fwd)
            
            if len(pnl) > 0:
                total_return = (1 + pd.Series(pnl)).cumprod().iloc[-1] - 1
                if total_return > best_score:
                    best_score = total_return
                    best_threshold = threshold
        
        thresholds[symbol] = best_threshold
        print(f"  {symbol}: optimal threshold = {best_threshold:.2f} (score: {best_score*100:.2f}%)")
    
    return thresholds


def train(symbols: list):
    print(f"Building enhanced features for {len(symbols)} symbol(s) (walk-forward)...")
    
    # First, run walk-forward on each symbol to validate
    wf_results = {}
    for symbol in symbols:
        print(f"\n{symbol}:")
        result = walk_forward_train(symbol)
        if "error" not in result:
            wf_results[symbol] = result
            print(f"  WF Accuracy: {result['accuracy']:.3f}")
            print(f"  Trades: {result['trade_simulation']['trades']}, "
                  f"Win%: {result['trade_simulation']['win_rate']}, "
                  f"Return: {result['trade_simulation']['total_return_pct']}%")
    
    # Train meta-model on pooled data
    meta_result = train_meta_model(symbols)
    
    if "error" in meta_result:
        print("Meta-model training failed")
        return
    
    # Find optimal confidence thresholds per symbol
    print("\n--- Finding per-symbol confidence thresholds ---")
    per_symbol_thresholds = find_per_symbol_thresholds(
        meta_result["model"], ENHANCED_FEATURE_COLUMNS, symbols
    )
    
    # Use global default for symbols not in thresholds
    global_threshold = 0.55
    
    # Save model with metadata
    MODEL_PATH.parent.mkdir(exist_ok=True)
    bundle = {
        "model": meta_result["model"],
        "feature_columns": ENHANCED_FEATURE_COLUMNS,
        "confidence_threshold": global_threshold,
        "per_symbol_thresholds": per_symbol_thresholds,
        "meta_cv_accuracy": meta_result["mean_cv_accuracy"],
        "feature_importance": meta_result["feature_importance"].to_dict("records"),
        "walk_forward_results": wf_results,
    }
    joblib.dump(bundle, MODEL_PATH)
    print(f"\nEnhanced model saved to {MODEL_PATH}")
    print(f"Global confidence threshold: {global_threshold:.2f}")
    print(f"Per-symbol thresholds: {per_symbol_thresholds}")
    print(f"CV Accuracy: {meta_result['mean_cv_accuracy']:.3f}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        symbols = ["RELIANCE", "TCS", "INFY"]
    elif args[0] == "--all":
        symbols = get_watchlist_symbols()
    else:
        symbols = args
    train(symbols)