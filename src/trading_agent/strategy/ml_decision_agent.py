"""ML decision agent for inference."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from trading_agent.core.logging_config import get_logger
from trading_agent.core.ml_versioning import ModelRegistry, get_model_registry
from trading_agent.strategy.candidates import Candidate
from trading_agent.strategy.ml_features import FeatureConfig

logger = get_logger(__name__)


@dataclass(frozen=True)
class MLDecision:
    """ML model decision output."""

    action: str  # BUY, SELL, HOLD
    confidence: float
    probability_up: float
    probability_down: float
    reasoning: str
    model_version: str
    features_used: list[str]


class MLDecisionAgent:
    """ML-based trading decision agent."""

    def __init__(
        self,
        model_path: str | Path | None = None,
        model_version: str | None = None,
        feature_config: FeatureConfig | None = None,
        confidence_threshold: float = 0.55,
        registry: ModelRegistry | None = None,
    ):
        self.feature_config = feature_config or FeatureConfig()
        self.confidence_threshold = confidence_threshold
        self.registry = registry or get_model_registry()

        self._model: lgb.Booster | None = None
        self._feature_names: list[str] = []
        self._model_version: str | None = None

        if model_path:
            self.load_model(model_path)
        elif model_version:
            self.load_model_version(model_version)

    def load_model(self, model_path: str | Path) -> None:
        """Load model from file."""
        path = Path(model_path)
        if not path.exists():
            raise FileNotFoundError(f"Model not found: {path}")

        self._model = lgb.Booster(model_file=str(path))
        self._feature_names = self._model.feature_name()
        self._model_version = path.stem
        logger.info(
            "ml_model_loaded",
            path=str(path),
            version=self._model_version,
            n_features=len(self._feature_names),
        )

    def load_model_version(self, version: str) -> None:
        """Load model from registry by version."""
        model, metadata = self.registry.load_model(version)
        self._model = model
        self._feature_names = metadata.features
        self._model_version = version
        logger.info(
            "ml_model_loaded_from_registry", version=version, n_features=len(self._feature_names)
        )

    def load_latest(self, symbol: str, model_type: str = "lightgbm") -> bool:
        """Load latest model for symbol."""
        metadata = self.registry.get_latest(symbol, model_type)
        if metadata:
            self.load_model_version(metadata.version)
            return True
        return False

    def load_best(self, symbol: str, model_type: str = "lightgbm", metric: str = "val_auc") -> bool:
        """Load best model for symbol by metric."""
        metadata = self.registry.get_best(symbol, model_type, metric)
        if metadata:
            self.load_model_version(metadata.version)
            return True
        return False

    def _prepare_candidate_features(self, candidate: Candidate) -> pd.DataFrame:
        """Prepare features for a single candidate."""
        # Create a minimal DataFrame with the candidate's data
        df = pd.DataFrame(
            [
                {
                    "timestamp": candidate.timestamp,
                    "open": candidate.close,  # Approximate
                    "high": candidate.close,
                    "low": candidate.close,
                    "close": candidate.close,
                    "volume": candidate.volume,
                }
            ]
        )

        # We need historical data for proper feature engineering
        # In practice, this would fetch recent candles
        # For now, return basic features
        return df

    def predict_proba(self, features: pd.DataFrame) -> np.ndarray:
        """Predict probabilities for features."""
        if self._model is None:
            raise RuntimeError("Model not loaded")

        # Align features
        missing = set(self._feature_names) - set(features.columns)
        if missing:
            # Add missing columns with zeros
            for col in missing:
                features[col] = 0.0

        # Reorder to match model
        features = features[self._feature_names]

        # Predict
        probas = self._model.predict(features, num_iteration=self._model.best_iteration)
        return probas

    def decide(self, candidate: Candidate, features: pd.DataFrame | None = None) -> MLDecision:
        """Make ML decision for a candidate."""
        if self._model is None:
            return MLDecision(
                action="HOLD",
                confidence=0.0,
                probability_up=0.5,
                probability_down=0.5,
                reasoning="Model not loaded",
                model_version=self._model_version or "unknown",
                features_used=[],
            )

        if features is None:
            features = self._prepare_candidate_features(candidate)

        try:
            probas = self.predict_proba(features)
            prob_up = float(probas[0]) if len(probas) > 0 else 0.5
            prob_down = 1.0 - prob_up

            # Determine action based on confidence threshold
            if prob_up >= self.confidence_threshold:
                action = "BUY"
                confidence = prob_up
            elif prob_down >= self.confidence_threshold:
                action = "SELL"
                confidence = prob_down
            else:
                action = "HOLD"
                confidence = max(prob_up, prob_down)

            reasoning = f"ML probability: UP={prob_up:.2%}, DOWN={prob_down:.2%}"

            return MLDecision(
                action=action,
                confidence=confidence,
                probability_up=prob_up,
                probability_down=prob_down,
                reasoning=reasoning,
                model_version=self._model_version or "unknown",
                features_used=self._feature_names,
            )
        except Exception as e:
            logger.error("ml_decision_failed", symbol=candidate.symbol, error=str(e))
            return MLDecision(
                action="HOLD",
                confidence=0.0,
                probability_up=0.5,
                probability_down=0.5,
                reasoning=f"ML error: {e}",
                model_version=self._model_version or "unknown",
                features_used=[],
            )

    def decide_batch(
        self,
        candidates: list[Candidate],
        features_list: list[pd.DataFrame] | None = None,
    ) -> list[MLDecision]:
        """Make decisions for multiple candidates."""
        if features_list is None:
            features_list = [None] * len(candidates)

        return [self.decide(c, f) for c, f in zip(candidates, features_list, strict=False)]

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def model_version(self) -> str | None:
        return self._model_version

    @property
    def feature_names(self) -> list[str]:
        return self._feature_names


async def run_ml_decision_cycle(symbols: list[str] | None = None, interval: str = "5minute"):
    """Run ML decision cycle for all symbols."""
    from trading_agent.ingest.watchlist_resolved import get_watchlist_symbols
    from trading_agent.storage.parquet_store import get_parquet_store
    from trading_agent.strategy.candidates import CandidateGenerator
    from trading_agent.strategy.ml_features import build_features
    
    if symbols is None:
        symbols = get_watchlist_symbols()
    
    if not symbols:
        logger.warning("run_ml_decision_cycle: no symbols in watchlist")
        return

    agent = MLDecisionAgent()
    # Try to load latest model for first symbol
    if symbols:
        loaded = agent.load_latest(symbols[0])
        if not loaded:
            logger.warning("ml_decision_cycle: no model loaded, skipping")
            return

    parquet = get_parquet_store()
    generator = CandidateGenerator(parquet_store=parquet)

    for symbol in symbols:
        try:
            # Load latest candles
            df = parquet.read_latest(symbol, interval, n=2000)
            if df is None or df.empty:
                logger.warning("no_data_for_symbol", symbol=symbol)
                continue

            # Build features
            features_df = build_features(df)
            if features_df is None or features_df.empty:
                logger.warning("no_features_built", symbol=symbol)
                continue

            # Get latest features
            latest_features = features_df.iloc[[-1]]
            
            candidate = generator.generate_from_data(df, symbol, interval)
            if candidate is None:
                logger.debug("no_candidate_generated", symbol=symbol)
                continue
            
            decision = agent.decide(candidate, latest_features)
            logger.info("ml_decision", symbol=symbol, action=decision.action, confidence=decision.confidence)
            
            # Store decision to database
            await _store_ml_signal(symbol, decision, interval)
                
        except Exception as e:
            logger.error("ml_decision_failed", symbol=symbol, error=str(e))


async def _store_ml_signal(symbol: str, decision, interval: str):
    """Store ML signal to database."""
    try:
        from trading_agent.storage.db import Signal, get_database
        from datetime import datetime, UTC
        
        db = get_database()
        async with db.session() as session:
            signal = Signal(
                symbol=symbol,
                timestamp=datetime.now(UTC),
                interval=interval,
                action=decision.action,
                strategy="ml",
                confidence=decision.confidence,
                rsi=0.0,
                adx=0.0,
                pattern_bias=0,
            )
            session.add(signal)
            await session.commit()
    except Exception as e:
        logger.error("store_ml_signal_failed", symbol=symbol, error=str(e))
