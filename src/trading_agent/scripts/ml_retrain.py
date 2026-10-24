"""Scheduled ML retraining using the current model and storage APIs."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from trading_agent.core.logging_config import get_logger, setup_logging
from trading_agent.core.ml_versioning import get_model_registry
from trading_agent.ingest.watchlist_resolved import get_watchlist_symbols
from trading_agent.storage.parquet_store import get_parquet_store
from trading_agent.strategy.ml_features import prepare_features
from trading_agent.strategy.train_ml_model_v2 import train_enhanced_model

logger = get_logger(__name__)

DRIFT_Z_THRESHOLD = 2.5
MIN_RETRAIN_INTERVAL_DAYS = 7
MIN_TRAINING_ROWS = 2_000
MAX_TRAINING_SYMBOLS = 100


def get_training_symbols(min_symbols: int = 1) -> list[str]:
    """Return watchlist symbols with enough locally stored candle history."""
    parquet = get_parquet_store()
    valid: list[str] = []
    for symbol in get_watchlist_symbols():
        try:
            if len(parquet.read_latest(symbol, "5minute", n=MIN_TRAINING_ROWS)) >= MIN_TRAINING_ROWS:
                valid.append(symbol)
        except Exception as exc:
            logger.warning("training_symbol_check_failed", symbol=symbol, error=str(exc))

    if len(valid) < min_symbols:
        logger.warning("insufficient_training_symbols", found=len(valid), required=min_symbols)
    return valid[:MAX_TRAINING_SYMBOLS]


def check_drift(symbols: list[str] | None = None) -> dict:
    """Compare recent feature means with an older local reference window."""
    parquet = get_parquet_store()
    checked = symbols or get_training_symbols()
    drifted: dict[str, list[str]] = {}

    for symbol in checked[:20]:
        candles = parquet.read_latest(symbol, "5minute", n=4_000)
        if len(candles) < MIN_TRAINING_ROWS:
            continue
        features, _ = prepare_features(candles)
        if len(features) < 200:
            continue

        midpoint = len(features) // 2
        reference = features.iloc[:midpoint]
        current = features.iloc[midpoint:]
        scale = reference.std().replace(0, np.nan)
        z_scores = ((current.mean() - reference.mean()) / scale).abs().dropna()
        changed = z_scores[z_scores > DRIFT_Z_THRESHOLD].index.tolist()
        if changed:
            drifted[symbol] = changed

    return {
        "drift_detected": bool(drifted),
        "symbols_checked": len(checked[:20]),
        "drifted_features": drifted,
        "threshold": DRIFT_Z_THRESHOLD,
    }


def retrain_model(symbols: list[str] | None = None) -> dict:
    """Train and register enhanced per-symbol models."""
    selected = symbols or get_training_symbols()
    if not selected:
        return {"status": "skipped", "reason": "no symbols have enough data"}

    results = train_enhanced_model(selected)
    succeeded = [symbol for symbol, result in results.items() if "error" not in result]
    failed = {
        symbol: result["error"]
        for symbol, result in results.items()
        if "error" in result
    }
    return {
        "status": "success" if succeeded else "failed",
        "trained": succeeded,
        "failed": failed,
    }


def _days_since_latest_model() -> int | None:
    models = get_model_registry().list_models()
    if not models:
        return None
    trained_at = datetime.fromisoformat(models[0].training_date)
    if trained_at.tzinfo is None:
        trained_at = trained_at.replace(tzinfo=UTC)
    return (datetime.now(UTC) - trained_at).days


def run_scheduled_job() -> dict:
    """Retrain weekly, or earlier when the feature drift check triggers."""
    days_since = _days_since_latest_model()
    drift = check_drift()
    if (
        days_since is not None
        and days_since < MIN_RETRAIN_INTERVAL_DAYS
        and not drift["drift_detected"]
    ):
        return {
            "status": "skipped",
            "reason": f"latest model is {days_since} days old and no drift was detected",
            "drift": drift,
        }

    result = retrain_model()
    result["drift"] = drift
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-drift", action="store_true")
    parser.add_argument("--retrain", action="store_true")
    parser.add_argument("--schedule", action="store_true")
    parser.add_argument("--symbols", help="Comma-separated symbol list")
    parser.add_argument("--list-versions", action="store_true")
    args = parser.parse_args()
    setup_logging()

    symbols = args.symbols.split(",") if args.symbols else None
    if args.list_versions:
        for metadata in get_model_registry().list_models():
            print(
                f"{metadata.version}: {metadata.symbol}, "
                f"trained={metadata.training_date}, metrics={metadata.metrics}"
            )
    elif args.check_drift:
        print(check_drift(symbols))
    elif args.retrain:
        print(retrain_model(symbols))
    elif args.schedule:
        print(run_scheduled_job())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
