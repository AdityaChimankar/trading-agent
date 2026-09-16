"""
Trains the ML signal model. CRITICAL: the train/test split is
CHRONOLOGICAL per symbol (first 80% of each symbol's history for
training, last 20% for testing) - never a random shuffle. Shuffling
time-series data before splitting would let the model train on rows
that come AFTER its own test rows, which is a lookahead leak just as
real as looking at future prices directly.

Reports both classification metrics (accuracy, confusion matrix) AND
an actual trade simulation on the held-out test data, converting
predictions into BUY/SELL/HOLD and computing win rate / total return
- classification accuracy alone doesn't tell you whether the model is
profitable; the trade simulation does, and is directly comparable to
backtest.py's numbers since it uses the same trade-outcome logic.

Usage:
  python train_ml_model.py RELIANCE TCS INFY   # train on named symbols (pooled)
  python train_ml_model.py --all                # train on the whole watchlist
"""
import sys
import joblib
import numpy as np
import pandas as pd
from pathlib import Path
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import accuracy_score, confusion_matrix, classification_report
from ml_features import build_features, FEATURE_COLUMNS
from db import get_watchlist_symbols

MODEL_PATH = Path(__file__).parent / "models" / "ml_model.joblib"
TRAIN_FRACTION = 0.8  # chronological - first 80% of EACH symbol's history


def load_pooled_data(symbols: list) -> tuple:
    """Builds features for each symbol independently, splits each
    symbol's own timeline chronologically, then pools across symbols.
    Splitting per-symbol before pooling (rather than pooling then
    splitting by row count) keeps each symbol's test data genuinely
    after its own train data - not just after some rows from other
    symbols, which wouldn't guarantee temporal separation for that
    symbol's own signal."""
    train_frames, test_frames = [], []
    for symbol in symbols:
        df = build_features(symbol)
        if df is None or len(df) < 100:
            print(f"  {symbol}: skipped (not enough data)")
            continue
        split_idx = int(len(df) * TRAIN_FRACTION)
        train_frames.append(df.iloc[:split_idx])
        test_frames.append(df.iloc[split_idx:])
        print(f"  {symbol}: {split_idx} train rows, {len(df) - split_idx} test rows")

    if not train_frames:
        return None, None

    train_df = pd.concat(train_frames, ignore_index=True)
    test_df = pd.concat(test_frames, ignore_index=True)
    return train_df, test_df


def simulate_trades_from_predictions(test_df: pd.DataFrame, predictions: np.ndarray) -> dict:
    """Converts model predictions into simulated trades and computes
    the same win-rate/return metrics backtest.py reports, using the
    ALREADY-COMPUTED forward_return column (the real outcome, known
    only because this is historical test data) - so this is an honest
    read of what following the model's calls would have earned,
    directly comparable to the rule-based backtest's numbers."""
    trade_mask = predictions != 0  # 0 = FLAT/HOLD, skip
    if trade_mask.sum() == 0:
        return {"trades": 0}

    forward_returns = test_df["forward_return"].values[trade_mask]
    preds = predictions[trade_mask]
    # UP prediction (1) profits if price rose; DOWN prediction (2)
    # profits if price fell - so DOWN trades need their return sign flipped
    pnl = np.where(preds == 1, forward_returns, -forward_returns)

    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    win_rate = len(wins) / len(pnl) * 100
    cumulative = (1 + pd.Series(pnl)).cumprod()

    return {
        "trades": int(trade_mask.sum()),
        "win_rate": round(win_rate, 1),
        "avg_win_pct": round(wins.mean() * 100, 3) if len(wins) else 0.0,
        "avg_loss_pct": round(losses.mean() * 100, 3) if len(losses) else 0.0,
        "total_return_pct": round((cumulative.iloc[-1] - 1) * 100, 2),
    }


def train(symbols: list):
    print(f"Building features for {len(symbols)} symbol(s) (chronological per-symbol split)...")
    train_df, test_df = load_pooled_data(symbols)
    if train_df is None:
        print("No usable data - nothing to train on.")
        return

    print(f"\nPooled: {len(train_df)} train rows, {len(test_df)} test rows")
    print(f"Train label distribution:\n{train_df['label'].value_counts().rename({0:'FLAT',1:'UP',2:'DOWN'})}")

    X_train, y_train = train_df[FEATURE_COLUMNS], train_df["label"]
    X_test, y_test = test_df[FEATURE_COLUMNS], test_df["label"]

    model = HistGradientBoostingClassifier(
        max_iter=200, max_depth=6, learning_rate=0.05,
        random_state=42, class_weight="balanced",
    )
    model.fit(X_train, y_train)

    predictions = model.predict(X_test)
    accuracy = accuracy_score(y_test, predictions)

    print(f"\n--- Classification metrics (held-out, chronologically after train data) ---")
    print(f"Accuracy: {accuracy:.3f}")
    print(f"\nConfusion matrix (rows=actual, cols=predicted; order FLAT/UP/DOWN):")
    print(confusion_matrix(y_test, predictions))
    print(f"\n{classification_report(y_test, predictions, target_names=['FLAT', 'UP', 'DOWN'], zero_division=0)}")

    print(f"--- Trade simulation on the SAME held-out data ---")
    sim_result = simulate_trades_from_predictions(test_df, predictions)
    for k, v in sim_result.items():
        print(f"  {k}: {v}")
    print(f"\nCompare this total_return_pct against backtest.py's rule-based numbers on the "
          f"same symbols/period to see whether the ML model actually adds anything.")

    MODEL_PATH.parent.mkdir(exist_ok=True)
    joblib.dump({"model": model, "feature_columns": FEATURE_COLUMNS}, MODEL_PATH)
    print(f"\nModel saved to {MODEL_PATH}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        symbols = ["RELIANCE"]
    elif args[0] == "--all":
        symbols = get_watchlist_symbols()
    else:
        symbols = args
    train(symbols)
