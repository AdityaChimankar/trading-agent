"""ML model training pipeline."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    precision_score,
    recall_score,
    f1_score,
    roc_auc_score,
    log_loss,
)
from sklearn.model_selection import TimeSeriesSplit

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger
from trading_agent.core.ml_versioning import ModelRegistry, get_model_registry
from trading_agent.storage.parquet_store import get_parquet_store
from trading_agent.strategy.ml_features import prepare_features, FeatureConfig, get_feature_names

logger = get_logger(__name__)


class MLTrainer:
    """Train ML models for trading signals."""

    def __init__(
        self,
        feature_config: FeatureConfig | None = None,
        registry: ModelRegistry | None = None,
        parquet_store=None,
    ):
        self.feature_config = feature_config or FeatureConfig()
        self.registry = registry or get_model_registry()
        self.parquet = parquet_store or get_parquet_store()

    def load_training_data(
        self,
        symbols: list[str],
        interval: str = "5minute",
        lookback_days: int = 60,
    ) -> tuple[pd.DataFrame, pd.Series]:
        """Load and prepare training data from multiple symbols."""
        all_X = []
        all_y = []

        for symbol in symbols:
            try:
                df = self.parquet.read_candles(symbol, interval)
                if df.empty or len(df) < 200:
                    logger.warning("insufficient_data", symbol=symbol, rows=len(df))
                    continue

                # Limit to lookback period
                cutoff = df["timestamp"].max() - pd.Timedelta(days=lookback_days)
                df = df[df["timestamp"] >= cutoff]

                if len(df) < 100:
                    continue

                X, y = prepare_features(df, self.feature_config)
                if len(X) == 0:
                    continue

                X["symbol"] = symbol  # Add symbol as feature
                all_X.append(X)
                all_y.append(y)

                logger.info("symbol_data_loaded", symbol=symbol, samples=len(X))

            except Exception as e:
                logger.error("symbol_load_failed", symbol=symbol, error=str(e))

        if not all_X:
            raise ValueError("No training data available")

        X_combined = pd.concat(all_X, ignore_index=True)
        y_combined = pd.concat(all_y, ignore_index=True)

        logger.info("training_data_combined", total_samples=len(X_combined), symbols=len(all_X))
        return X_combined, y_combined

    def train(
        self,
        symbols: list[str],
        interval: str = "5minute",
        lookback_days: int = 60,
        test_size: float = 0.2,
        params: dict | None = None,
        symbol: str | None = None,
        model_type: str = "lightgbm",
    ) -> dict:
        """Train model on historical data."""
        # Load data
        X, y = self.load_training_data(symbols, interval, lookback_days)

        # Time series split
        split_idx = int(len(X) * (1 - test_size))
        X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
        y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

        # Remove symbol column for training
        train_features = X_train.drop(columns=["symbol"], errors="ignore")
        test_features = X_test.drop(columns=["symbol"], errors="ignore")

        # Default LightGBM params
        default_params = {
            "objective": "binary",
            "metric": "auc",
            "boosting_type": "gbdt",
            "num_leaves": 63,
            "learning_rate": 0.01,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 5,
            "min_child_samples": 50,
            "reg_alpha": 0.1,
            "reg_lambda": 0.1,
            "verbosity": -1,
            "n_estimators": 2000,
            "early_stopping_rounds": 100,
        }

        if params:
            default_params.update(params)

        # Create datasets
        train_data = lgb.Dataset(train_features, label=y_train)
        test_data = lgb.Dataset(test_features, label=y_test, reference=train_data)

        # Train
        logger.info("training_started", train_samples=len(X_train), test_samples=len(X_test))
        model = lgb.train(
            default_params,
            train_data,
            valid_sets=[train_data, test_data],
            valid_names=["train", "valid"],
            callbacks=[lgb.log_evaluation(100)],
        )

        # Evaluate
        train_pred = model.predict(train_features, num_iteration=model.best_iteration)
        test_pred = model.predict(test_features, num_iteration=model.best_iteration)

        train_binary = (train_pred > 0.5).astype(int)
        test_binary = (test_pred > 0.5).astype(int)

        metrics = {
            "train_auc": roc_auc_score(y_train, train_pred),
            "test_auc": roc_auc_score(y_test, test_pred),
            "train_logloss": log_loss(y_train, train_pred),
            "test_logloss": log_loss(y_test, test_pred),
            "train_accuracy": accuracy_score(y_train, train_binary),
            "test_accuracy": accuracy_score(y_test, test_binary),
            "train_precision": precision_score(y_train, train_binary, zero_division=0),
            "test_precision": precision_score(y_test, test_binary, zero_division=0),
            "train_recall": recall_score(y_train, train_binary, zero_division=0),
            "test_recall": recall_score(y_test, test_binary, zero_division=0),
            "train_f1": f1_score(y_train, train_binary, zero_division=0),
            "test_f1": f1_score(y_test, test_binary, zero_division=0),
            "best_iteration": model.best_iteration,
            "n_features": len(train_features.columns),
        }

        logger.info("training_completed", **metrics)

        # Feature importance
        importance = pd.DataFrame(
            {
                "feature": train_features.columns,
                "importance": model.feature_importance(importance_type="gain"),
            }
        ).sort_values("importance", ascending=False)

        # Register model
        train_symbol = symbol or (symbols[0] if len(symbols) == 1 else "MULTI")
        metadata = self.registry.register(
            model=model,
            symbol=train_symbol,
            model_type=model_type,
            X=train_features,
            y=y_train,
            hyperparameters=default_params,
            metrics=metrics,
            features=list(train_features.columns),
            feature_version=self.feature_config.__class__.__name__,
            tags={"interval": interval, "lookback_days": str(lookback_days)},
        )

        return {
            "model": model,
            "metadata": metadata,
            "metrics": metrics,
            "feature_importance": importance.to_dict("records"),
        }

    def walk_forward_train(
        self,
        symbols: list[str],
        interval: str = "5minute",
        n_splits: int = 5,
        lookback_days: int = 60,
        params: dict | None = None,
    ) -> list[dict]:
        """Walk-forward training with time series cross-validation."""
        X, y = self.load_training_data(symbols, interval, lookback_days)

        # Remove symbol column
        features = X.drop(columns=["symbol"], errors="ignore")

        tscv = TimeSeriesSplit(n_splits=n_splits)
        results = []

        for fold, (train_idx, val_idx) in enumerate(tscv.split(features)):
            logger.info("walkforward_fold", fold=fold + 1, train=len(train_idx), val=len(val_idx))

            X_train, X_val = features.iloc[train_idx], features.iloc[val_idx]
            y_train, y_val = y.iloc[train_idx], y.iloc[val_idx]

            # Train
            fold_params = params or {
                "objective": "binary",
                "metric": "auc",
                "boosting_type": "gbdt",
                "num_leaves": 63,
                "learning_rate": 0.01,
                "feature_fraction": 0.8,
                "bagging_fraction": 0.8,
                "bagging_freq": 5,
                "min_child_samples": 50,
                "verbosity": -1,
                "n_estimators": 1000,
                "early_stopping_rounds": 50,
            }

            train_data = lgb.Dataset(X_train, label=y_train)
            val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)

            model = lgb.train(
                fold_params,
                train_data,
                valid_sets=[train_data, val_data],
                valid_names=["train", "valid"],
                callbacks=[lgb.log_evaluation(50)],
            )

            # Evaluate
            val_pred = model.predict(X_val, num_iteration=model.best_iteration)
            val_binary = (val_pred > 0.5).astype(int)

            fold_metrics = {
                "fold": fold,
                "val_auc": roc_auc_score(y_val, val_pred),
                "val_accuracy": accuracy_score(y_val, val_binary),
                "val_precision": precision_score(y_val, val_binary, zero_division=0),
                "val_recall": recall_score(y_val, val_binary, zero_division=0),
                "val_f1": f1_score(y_val, val_binary, zero_division=0),
                "best_iteration": model.best_iteration,
            }

            results.append(fold_metrics)
            logger.info("fold_completed", **fold_metrics)

        # Aggregate metrics
        avg_metrics = {
            k: np.mean([r[k] for r in results if k in r]) for k in results[0].keys() if k != "fold"
        }

        logger.info("walkforward_completed", **avg_metrics)
        return results


def train_model(
    symbols: list[str],
    interval: str = "5minute",
    lookback_days: int = 60,
    **kwargs,
) -> dict:
    """Convenience function to train model."""
    trainer = MLTrainer()
    return trainer.train(symbols, interval, lookback_days, **kwargs)
