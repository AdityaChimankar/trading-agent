"""ML model versioning and registry."""

from __future__ import annotations

import hashlib
import json
import pickle
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class ModelMetadata:
    """Metadata for a trained model."""

    version: str
    feature_version: str
    symbol: str
    model_type: str
    training_date: str
    training_samples: int
    features: list[str]
    hyperparameters: dict[str, Any]
    metrics: dict[str, float]
    data_hash: str
    file_path: str
    tags: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ModelMetadata:
        return cls(**data)


class ModelRegistry:
    """Registry for managing ML model versions."""

    def __init__(self, model_dir: Path | None = None):
        settings = get_settings()
        self.model_dir = model_dir or settings.ml.model_dir
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.registry_file = self.model_dir / "registry.json"
        self._registry: dict[str, ModelMetadata] = {}
        self._load_registry()

    def _load_registry(self) -> None:
        """Load registry from disk."""
        if self.registry_file.exists():
            try:
                with open(self.registry_file) as f:
                    data = json.load(f)
                self._registry = {k: ModelMetadata.from_dict(v) for k, v in data.items()}
                logger.info("model_registry_loaded", count=len(self._registry))
            except Exception as e:
                logger.error("model_registry_load_failed", error=str(e))
                self._registry = {}

    def _save_registry(self) -> None:
        """Save registry to disk."""
        try:
            data = {k: v.to_dict() for k, v in self._registry.items()}
            with open(self.registry_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error("model_registry_save_failed", error=str(e))

    def _compute_data_hash(self, X: pd.DataFrame, y: pd.Series) -> str:
        """Compute hash of training data for reproducibility."""
        combined = pd.concat([X, y.rename("target")], axis=1)
        return hashlib.sha256(pickle.dumps(combined)).hexdigest()[:16]

    def _generate_version(self, symbol: str, model_type: str) -> str:
        """Generate version string."""
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        return f"{symbol}_{model_type}_{timestamp}"

    def register(
        self,
        model: Any,
        symbol: str,
        model_type: str,
        X: pd.DataFrame,
        y: pd.Series,
        hyperparameters: dict[str, Any],
        metrics: dict[str, float],
        features: list[str],
        feature_version: str,
        tags: dict[str, str] | None = None,
    ) -> ModelMetadata:
        """Register a new model version."""
        version = self._generate_version(symbol, model_type)
        data_hash = self._compute_data_hash(X, y)

        file_path = self.model_dir / f"{version}.pkl"
        with open(file_path, "wb") as f:
            pickle.dump(model, f)

        metadata = ModelMetadata(
            version=version,
            feature_version=feature_version,
            symbol=symbol,
            model_type=model_type,
            training_date=datetime.now(UTC).isoformat(),
            training_samples=len(X),
            features=features,
            hyperparameters=hyperparameters,
            metrics=metrics,
            data_hash=data_hash,
            file_path=str(file_path),
            tags=tags or {},
        )

        self._registry[version] = metadata
        self._save_registry()

        logger.info("model_registered", version=version, symbol=symbol, metrics=metrics)
        return metadata

    def load_model(self, version: str) -> tuple[Any, ModelMetadata]:
        """Load a model by version."""
        if version not in self._registry:
            raise ValueError(f"Model version not found: {version}")

        metadata = self._registry[version]
        with open(metadata.file_path, "rb") as f:
            model = pickle.load(f)

        return model, metadata

    def get_latest(self, symbol: str, model_type: str) -> ModelMetadata | None:
        """Get latest model for a symbol and type."""
        candidates = [
            m for m in self._registry.values() if m.symbol == symbol and m.model_type == model_type
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda m: m.training_date)

    def get_best(
        self, symbol: str, model_type: str, metric: str = "val_auc"
    ) -> ModelMetadata | None:
        """Get best model by metric."""
        candidates = [
            m
            for m in self._registry.values()
            if m.symbol == symbol and m.model_type == model_type and metric in m.metrics
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda m: m.metrics.get(metric, -float("inf")))

    def list_models(
        self,
        symbol: str | None = None,
        model_type: str | None = None,
    ) -> list[ModelMetadata]:
        """List models with optional filters."""
        models = list(self._registry.values())
        if symbol:
            models = [m for m in models if m.symbol == symbol]
        if model_type:
            models = [m for m in models if m.model_type == model_type]
        return sorted(models, key=lambda m: m.training_date, reverse=True)

    def delete(self, version: str) -> bool:
        """Delete a model version."""
        if version not in self._registry:
            return False

        metadata = self._registry[version]
        try:
            Path(metadata.file_path).unlink(missing_ok=True)
        except Exception as e:
            logger.warning("model_file_delete_failed", version=version, error=str(e))

        del self._registry[version]
        self._save_registry()
        logger.info("model_deleted", version=version)
        return True

    def cleanup_old(self, keep_per_symbol: int = 5) -> int:
        """Delete old models, keeping N per symbol/type."""
        deleted = 0
        for symbol in {m.symbol for m in self._registry.values()}:
            for model_type in {m.model_type for m in self._registry.values()}:
                models = self.list_models(symbol, model_type)
                for old_model in models[keep_per_symbol:]:
                    if self.delete(old_model.version):
                        deleted += 1
        return deleted


# Global registry instance
_registry: ModelRegistry | None = None


def get_model_registry(model_dir: Path | None = None) -> ModelRegistry:
    """Get or create the global model registry."""
    global _registry
    if _registry is None:
        _registry = ModelRegistry(model_dir)
    return _registry
