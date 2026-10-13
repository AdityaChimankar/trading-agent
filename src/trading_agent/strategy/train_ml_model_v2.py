"""Enhanced ML model training with walk-forward validation and regime features."""

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
    precision_recall_curve,
)
from sklearn.model_selection import TimeSeriesSplit

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger
from trading_agent.core.ml_versioning import ModelRegistry, get_model_registry
from trading_agent.storage.parquet_store import get_parquet_store
from trading_agent.strategy.ml_features import (
    prepare_features,
    FeatureConfig,
    add_session_features,
    add_return_features,
    add_technical_features,
    add_pattern_features,
    add_lag_features,
    create_target,
)

logger = get_logger(__name__)


class EnhancedFeatureConfig(FeatureConfig):
    """Enhanced feature configuration for v2 models."""

    # Regime features
    include_regime: bool = True
    regime_lookback: int = 50
    regime_n_states: int = 3

    # Cost-aware labeling
    use_cost_aware_labels: bool = True
    transaction_cost: float = 0.0005  # 5 bps
    slippage: float = 0.0005

    # Per-symbol adaptation
    per_symbol_thresholds: bool = True
    min_confidence: float = 0.4
    max_confidence: float = 0.8

    # Advanced features
    include_volume_profile: bool = True
    include_microstructure: bool = False


DEFAULT_ENHANCED_CONFIG = EnhancedFeatureConfig()


def add_regime_features(df: pd.DataFrame, config: EnhancedFeatureConfig) -> pd.DataFrame:
    """Add market regime features using HMM or rolling statistics."""
    df = df.copy()

    if not config.include_regime:
        return df

    close = df["close"]
    returns = close.pct_change()

    # Rolling volatility regime
    vol_window = config.regime_lookback
    df["vol_regime"] = returns.rolling(vol_window).std()
    df["vol_regime_z"] = (df["vol_regime"] - df["vol_regime"].rolling(252).mean()) / df[
        "vol_regime"
    ].rolling(252).std()

    # Trend regime
    df["trend_regime"] = close.rolling(vol_window).apply(
        lambda x: np.polyfit(range(len(x)), x, 1)[0] if len(x) == vol_window else np.nan
    )
    df["trend_regime"] = df["trend_regime"] / close * 100  # As percentage

    # Momentum regime
    df["momentum_regime"] = close.pct_change(vol_window)

    # Volume regime
    df["volume_regime"] = df["volume"].rolling(vol_window).mean() / df["volume"].rolling(252).mean()

    # Discretize regimes
    for col in ["vol_regime_z", "trend_regime", "momentum_regime", "volume_regime"]:
        if col in df.columns:
            df[f"{col}_quantile"] = pd.qcut(
                df[col], q=config.regime_n_states, labels=False, duplicates="drop"
            )

    return df


def create_cost_aware_target(
    df: pd.DataFrame,
    horizon: int,
    transaction_cost: float = 0.0005,
    slippage: float = 0.0005,
) -> pd.Series:
    """Create target with transaction costs accounted for."""
    close = df["close"]
    future_close = close.shift(-horizon)

    # Gross return
    gross_return = future_close / close - 1

    # Net return after costs (round trip: entry + exit)
    total_cost = 2 * (transaction_cost + slippage)
    net_return = gross_return - total_cost

    # Binary target: profitable after costs
    target = (net_return > 0).astype(int)

    return target


def create_triple_barrier_target(
    df: pd.DataFrame,
    horizon: int,
    atr_multiplier: float = 1.5,
    take_profit_multiplier: float = 2.0,
) -> pd.Series:
    """Create triple barrier labels (simplified)."""
    # This is a simplified version - full implementation would need
    # path-dependent analysis of high/low during holding period
    close = df["close"]
    high = df["high"]
    low = df["low"]

    # For now, use simple return with ATR-based barriers
    future_high = high.shift(-horizon)
    future_low = low.shift(-horizon)

    atr_col = [c for c in df.columns if c.startswith("atr_")]
    if atr_col:
        atr = df[atr_col[0]]
        stop_dist = atr * atr_multiplier
        tp_dist = atr * take_profit_multiplier

        # Hit stop loss
        hit_stop = future_low < close - stop_dist
        # Hit take profit
        hit_tp = future_high > close + tp_dist
        # Neither hit - use close return
        neither = ~hit_stop & ~hit_tp

        target = pd.Series(0, index=df.index)
        target[hit_tp] = 1
        target[hit_stop] = -1
        target[neither] = np.sign(close.shift(-horizon)[neither] - close[neither])

        return target
    else:
        return np.sign(close.shift(-horizon) - close)


class EnhancedMLTrainer:
    """Enhanced ML trainer with walk-forward validation and regime features."""

    def __init__(
        self,
        feature_config: EnhancedFeatureConfig | None = None,
        registry: ModelRegistry | None = None,
        parquet_store=None,
    ):
        self.feature_config = feature_config or DEFAULT_ENHANCED_CONFIG
        self.registry = registry or get_model_registry()
        self.parquet = parquet_store or get_parquet_store()

    def prepare_enhanced_features(
        self,
        df: pd.DataFrame,
    ) -> pd.DataFrame:
        """Prepare enhanced features including regime."""
        df = df.copy()

        if "timestamp" in df.columns:
            df = df.sort_values("timestamp").reset_index(drop=True)

        # Base features
        df = add_return_features(df, self.feature_config.return_periods)
        df = add_technical_features(df, self.feature_config)
        df = add_pattern_features(df, self.feature_config)
        df = add_session_features(df)
        df = add_regime_features(df, self.feature_config)

        return df

    def create_targets(self, df: pd.DataFrame) -> dict[str, pd.Series]:
        """Create multiple target types."""
        targets = {}

        # Standard return target
        targets["return"] = create_target(df, self.feature_config.target_horizon, "return")

        # Direction target
        targets["direction"] = create_target(df, self.feature_config.target_horizon, "direction")

        # Cost-aware target
        if self.feature_config.use_cost_aware_labels:
            targets["cost_aware"] = create_cost_aware_target(
                df,
                self.feature_config.target_horizon,
                self.feature_config.transaction_cost,
                self.feature_config.slippage,
            )

        # Triple barrier target
        targets["triple_barrier"] = create_triple_barrier_target(
            df,
            self.feature_config.target_horizon,
        )

        return targets

    def train_per_symbol(
        self,
        symbols: list[str],
        interval: str = "5minute",
        lookback_days: int = 60,
        target_type: str = "cost_aware",
        params: dict | None = None,
    ) -> dict[str, dict]:
        """Train separate model for each symbol."""
        results = {}

        for symbol in symbols:
            try:
                logger.info("training_symbol", symbol=symbol)
                result = self._train_single_symbol(
                    symbol, interval, lookback_days, target_type, params
                )
                results[symbol] = result
            except Exception as e:
                logger.error("symbol_training_failed", symbol=symbol, error=str(e))
                results[symbol] = {"error": str(e)}

        return results

    def _train_single_symbol(
        self,
        symbol: str,
        interval: str,
        lookback_days: int,
        target_type: str,
        params: dict | None,
    ) -> dict:
        """Train model for a single symbol."""
        # Load data
        df = self.parquet.read_candles(symbol, interval)
        if df.empty or len(df) < 300:
            raise ValueError(f"Insufficient data for {symbol}: {len(df)} rows")

        cutoff = df["timestamp"].max() - pd.Timedelta(days=lookback_days)
        df = df[df["timestamp"] >= cutoff].copy()

        # Prepare features
        df = self.prepare_enhanced_features(df)

        # Create targets
        targets = self.create_targets(df)
        if target_type not in targets:
            raise ValueError(f"Unknown target type: {target_type}")

        y = targets[target_type]

        # Select features
        exclude = {"timestamp", "open", "high", "low", "close", "volume", "oi"}
        exclude.update([c for c in df.columns if c.startswith("target")])
        feature_cols = [c for c in df.columns if c not in exclude]

        X = df[feature_cols].copy()

        # Add lags
        key_indicators = [
            c
            for c in feature_cols
            if any(
                c.startswith(p)
                for p in [
                    "rsi_",
                    "macd",
                    "adx_",
                    "atr_",
                    "ema_",
                    "bb_",
                    "stoch",
                    "donchian",
                    "vwap",
                    "return_",
                    "vol_",
                    "trend_",
                    "momentum_",
                ]
            )
        ]
        X = add_lag_features(X, key_indicators, self.feature_config.lag_periods)

        # Drop NaN
        valid = X.notna().all(axis=1) & y.notna()
        X, y = X[valid], y[valid]

        if len(X) < 100:
            raise ValueError(f"Insufficient valid samples after cleaning: {len(X)}")

        # Time series split
        split_idx = int(len(X) * 0.8)
        X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
        y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

        # Default params optimized for financial data
        default_params = {
            "objective": "binary",
            "metric": "auc",
            "boosting_type": "gbdt",
            "num_leaves": 31,
            "learning_rate": 0.005,
            "feature_fraction": 0.7,
            "bagging_fraction": 0.7,
            "bagging_freq": 5,
            "min_child_samples": 100,
            "min_child_weight": 1e-3,
            "reg_alpha": 0.5,
            "reg_lambda": 0.5,
            "verbosity": -1,
            "n_estimators": 3000,
            "early_stopping_rounds": 150,
            "class_weight": "balanced",
        }

        if params:
            default_params.update(params)

        # Train
        train_data = lgb.Dataset(X_train, label=y_train)
        test_data = lgb.Dataset(X_test, label=y_test, reference=train_data)

        model = lgb.train(
            default_params,
            train_data,
            valid_sets=[train_data, test_data],
            valid_names=["train", "valid"],
            callbacks=[lgb.log_evaluation(200)],
        )

        # Evaluate
        train_pred = model.predict(X_train, num_iteration=model.best_iteration)
        test_pred = model.predict(X_test, num_iteration=model.best_iteration)

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
            "n_features": len(X_train.columns),
            "n_train": len(X_train),
            "n_test": len(X_test),
            "positive_rate_train": float(y_train.mean()),
            "positive_rate_test": float(y_test.mean()),
        }

        # Calculate optimal confidence threshold
        test_precision, test_recall, thresholds = precision_recall_curve(y_test, test_pred)
        f1_scores = 2 * test_precision * test_recall / (test_precision + test_recall + 1e-10)
        best_idx = np.argmax(f1_scores)
        optimal_threshold = float(thresholds[best_idx]) if best_idx < len(thresholds) else 0.5
        optimal_threshold = np.clip(
            optimal_threshold,
            self.feature_config.min_confidence,
            self.feature_config.max_confidence,
        )

        metrics["optimal_threshold"] = optimal_threshold

        logger.info("symbol_training_completed", symbol=symbol, **metrics)

        # Feature importance
        importance = pd.DataFrame(
            {
                "feature": X_train.columns,
                "importance": model.feature_importance(importance_type="gain"),
            }
        ).sort_values("importance", ascending=False)

        # Register
        metadata = self.registry.register(
            model=model,
            symbol=symbol,
            model_type="lightgbm_v2",
            X=X_train,
            y=y_train,
            hyperparameters=default_params,
            metrics=metrics,
            features=list(X_train.columns),
            feature_version="v2_enhanced",
            tags={
                "interval": interval,
                "lookback_days": str(lookback_days),
                "target_type": target_type,
                "optimal_threshold": str(optimal_threshold),
            },
        )

        return {
            "model": model,
            "metadata": metadata,
            "metrics": metrics,
            "feature_importance": importance.head(20).to_dict("records"),
            "optimal_threshold": optimal_threshold,
        }

    def walk_forward_validate(
        self,
        symbols: list[str],
        interval: str = "5minute",
        n_splits: int = 5,
        lookback_days: int = 60,
        target_type: str = "cost_aware",
    ) -> dict[str, list[dict]]:
        """Walk-forward validation per symbol."""
        all_results = {}

        for symbol in symbols:
            try:
                df = self.parquet.read_candles(symbol, interval)
                if df.empty or len(df) < 500:
                    continue

                cutoff = df["timestamp"].max() - pd.Timedelta(days=lookback_days)
                df = df[df["timestamp"] >= cutoff].copy()

                df = self.prepare_enhanced_features(df)
                targets = self.create_targets(df)
                y = targets[target_type]

                exclude = {"timestamp", "open", "high", "low", "close", "volume", "oi"}
                exclude.update([c for c in df.columns if c.startswith("target")])
                feature_cols = [c for c in df.columns if c not in exclude]
                X = df[feature_cols].copy()

                key_indicators = [
                    c
                    for c in feature_cols
                    if any(
                        c.startswith(p)
                        for p in [
                            "rsi_",
                            "macd",
                            "adx_",
                            "atr_",
                            "ema_",
                            "bb_",
                            "stoch",
                            "donchian",
                            "vwap",
                            "return_",
                            "vol_",
                            "trend_",
                            "momentum_",
                        ]
                    )
                ]
                X = add_lag_features(X, key_indicators, self.feature_config.lag_periods)

                valid = X.notna().all(axis=1) & y.notna()
                X, y = X[valid], y[valid]

                if len(X) < 200:
                    continue

                tscv = TimeSeriesSplit(n_splits=n_splits)
                symbol_results = []

                for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
                    X_train, X_val = X.iloc[train_idx], X.iloc[val_idx]
                    y_train, y_val = y.iloc[train_idx], y.iloc[val_idx]

                    params = {
                        "objective": "binary",
                        "metric": "auc",
                        "boosting_type": "gbdt",
                        "num_leaves": 31,
                        "learning_rate": 0.005,
                        "feature_fraction": 0.7,
                        "bagging_fraction": 0.7,
                        "bagging_freq": 5,
                        "min_child_samples": 100,
                        "reg_alpha": 0.5,
                        "reg_lambda": 0.5,
                        "verbosity": -1,
                        "n_estimators": 2000,
                        "early_stopping_rounds": 100,
                        "class_weight": "balanced",
                    }

                    train_data = lgb.Dataset(X_train, label=y_train)
                    val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)

                    model = lgb.train(
                        params,
                        train_data,
                        valid_sets=[train_data, val_data],
                        valid_names=["train", "valid"],
                        callbacks=[lgb.log_evaluation(100)],
                    )

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

                    symbol_results.append(fold_metrics)

                all_results[symbol] = symbol_results
                logger.info("wfv_completed", symbol=symbol, folds=len(symbol_results))

            except Exception as e:
                logger.error("wfv_failed", symbol=symbol, error=str(e))

        return all_results


def train_enhanced_model(
    symbols: list[str],
    interval: str = "5minute",
    lookback_days: int = 60,
    target_type: str = "cost_aware",
    **kwargs,
) -> dict:
    """Convenience function to train enhanced model."""
    trainer = EnhancedMLTrainer()
    return trainer.train_per_symbol(symbols, interval, lookback_days, target_type, **kwargs)
