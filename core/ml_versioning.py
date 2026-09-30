"""
ML model versioning and drift detection.

Provides model artifact management with version tracking, metadata,
and data drift monitoring for production ML models.
"""
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import joblib
import numpy as np
import pandas as pd

from core.logging_config import get_logger
from paths import MODEL_DIR, ML_MODEL_PATH


logger = get_logger(__name__)


@dataclass
class ModelMetadata:
    """Metadata for a trained model version."""
    version: str
    trained_at: str
    training_symbols: list[str]
    training_samples: int
    feature_columns: list[str]
    model_type: str
    model_params: dict
    training_metrics: dict = field(default_factory=dict)
    data_hash: str = ""
    git_commit: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "trained_at": self.trained_at,
            "training_symbols": self.training_symbols,
            "training_samples": self.training_samples,
            "feature_columns": self.feature_columns,
            "model_type": self.model_type,
            "model_params": self.model_params,
            "training_metrics": self.training_metrics,
            "data_hash": self.data_hash,
            "git_commit": self.git_commit,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ModelMetadata":
        return cls(**data)


class ModelRegistry:
    """Registry for managing ML model versions."""

    def __init__(self, model_dir: Path = MODEL_DIR):
        self.model_dir = model_dir
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_file = self.model_dir / "model_registry.json"
        self._registry: dict[str, ModelMetadata] = {}
        self._load_registry()

    def _load_registry(self) -> None:
        """Load registry from disk."""
        if self.metadata_file.exists():
            try:
                with open(self.metadata_file) as f:
                    data = json.load(f)
                self._registry = {
                    k: ModelMetadata.from_dict(v) for k, v in data.items()
                }
            except Exception as e:
                logger.error("Failed to load model registry", extra={"error": str(e)})

    def _save_registry(self) -> None:
        """Save registry to disk."""
        try:
            data = {k: v.to_dict() for k, v in self._registry.items()}
            with open(self.metadata_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error("Failed to save model registry", extra={"error": str(e)})

    def register(
        self,
        model_path: Path,
        training_symbols: list[str],
        training_samples: int,
        feature_columns: list[str],
        model_params: dict,
        training_metrics: dict = None,
        notes: str = "",
    ) -> str:
        """Register a new model version.

        Args:
            model_path: Path to the model file
            training_symbols: Symbols used for training
            training_samples: Number of training samples
            feature_columns: Feature column names
            model_params: Model hyperparameters
            training_metrics: Training metrics (accuracy, etc.)
            notes: Optional notes

        Returns:
            Version string of the registered model
        """
        # Generate version from timestamp and content hash
        with open(model_path, "rb") as f:
            content_hash = hashlib.sha256(f.read()).hexdigest()[:12]

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        version = f"v{timestamp}_{content_hash}"

        # Get git commit if available
        git_commit = ""
        try:
            import subprocess
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, cwd=model_path.parent.parent
            )
            if result.returncode == 0:
                git_commit = result.stdout.strip()
        except Exception:
            pass

        # Compute data hash from training data (approximate)
        data_hash = hashlib.sha256(
            f"{sorted(training_symbols)}_{training_samples}_{timestamp}".encode()
        ).hexdigest()[:12]

        metadata = ModelMetadata(
            version=version,
            trained_at=datetime.now().isoformat(),
            training_symbols=training_symbols,
            training_samples=training_samples,
            feature_columns=feature_columns,
            model_type=type(joblib.load(model_path)["model"]).__name__,
            model_params=model_params,
            training_metrics=training_metrics or {},
            data_hash=data_hash,
            git_commit=git_commit,
            notes=notes,
        )

        # Save model with versioned name
        versioned_path = self.model_dir / f"ml_model_{version}.joblib"
        import shutil
        shutil.copy2(model_path, versioned_path)

        # Update registry
        self._registry[version] = metadata
        self._save_registry()

        logger.info(f"Registered model version {version}", extra={"version": version, "path": str(versioned_path)})
        return version

    def get_metadata(self, version: str) -> Optional[ModelMetadata]:
        """Get metadata for a specific version."""
        return self._registry.get(version)

    def get_latest_version(self) -> Optional[str]:
        """Get the latest model version."""
        if not self._registry:
            return None
        # Sort by trained_at timestamp
        sorted_versions = sorted(
            self._registry.keys(),
            key=lambda v: self._registry[v].trained_at,
            reverse=True,
        )
        return sorted_versions[0]

    def get_latest_metadata(self) -> Optional[ModelMetadata]:
        """Get metadata for the latest model version."""
        version = self.get_latest_version()
        return self.get_metadata(version) if version else None

    def list_versions(self) -> list[ModelMetadata]:
        """List all registered model versions, newest first."""
        sorted_versions = sorted(
            self._registry.keys(),
            key=lambda v: self._registry[v].trained_at,
            reverse=True,
        )
        return [self._registry[v] for v in sorted_versions]

    def cleanup_old_versions(self, keep: int = 5) -> list[str]:
        """Remove old model versions, keeping only the latest N.

        Args:
            keep: Number of recent versions to keep

        Returns:
            List of removed version strings
        """
        versions = self.list_versions()
        if len(versions) <= keep:
            return []

        removed = []
        for metadata in versions[keep:]:
            version = metadata.version
            model_path = self.model_dir / f"ml_model_{version}.joblib"
            if model_path.exists():
                model_path.unlink()
            del self._registry[version]
            removed.append(version)

        self._save_registry()
        logger.info(f"Cleaned up old model versions", extra={"removed": removed})
        return removed


class DriftDetector:
    """Detect data drift in production features."""

    def __init__(self, reference_features: pd.DataFrame, feature_columns: list[str]):
        self.feature_columns = feature_columns
        self.reference_stats = self._compute_stats(reference_features)

    def _compute_stats(self, df: pd.DataFrame) -> dict:
        """Compute statistical summaries for drift detection."""
        stats = {}
        for col in self.feature_columns:
            if col in df.columns:
                vals = df[col].dropna()
                if len(vals) > 0:
                    stats[col] = {
                        "mean": float(vals.mean()),
                        "std": float(vals.std()),
                        "min": float(vals.min()),
                        "max": float(vals.max()),
                        "median": float(vals.median()),
                        "q25": float(vals.quantile(0.25)),
                        "q75": float(vals.quantile(0.75)),
                    }
        return stats

    def check_drift(self, current_features: pd.DataFrame, threshold: float = 2.0) -> dict:
        """Check for drift in current features compared to reference.

        Args:
            current_features: Current feature DataFrame
            threshold: Number of standard deviations for drift alert

        Returns:
            Dict with drift status per feature and overall
        """
        current_stats = self._compute_stats(current_features)
        drift_report = {"features": {}, "drift_detected": False, "drifted_features": []}

        for col in self.feature_columns:
            if col not in self.reference_stats or col not in current_stats:
                drift_report["features"][col] = {"status": "missing_data"}
                continue

            ref = self.reference_stats[col]
            cur = current_stats[col]

            # Check mean shift
            if ref["std"] > 0:
                z_score = abs(cur["mean"] - ref["mean"]) / ref["std"]
                drifted = z_score > threshold
            else:
                z_score = 0.0
                drifted = False

            drift_report["features"][col] = {
                "reference_mean": ref["mean"],
                "current_mean": cur["mean"],
                "z_score": z_score,
                "drifted": drifted,
            }

            if drifted:
                drift_report["drifted_features"].append(col)
                drift_report["drift_detected"] = True

        return drift_report


# Global registry instance
_registry: Optional[ModelRegistry] = None


def get_model_registry() -> ModelRegistry:
    """Get the global model registry instance."""
    global _registry
    if _registry is None:
        _registry = ModelRegistry()
    return _registry


def get_current_model_version() -> Optional[str]:
    """Get the currently deployed model version."""
    return get_model_registry().get_latest_version()


def load_model_with_version(version: Optional[str] = None) -> tuple:
    """Load model bundle with metadata.

    Args:
        version: Specific version to load, or None for latest

    Returns:
        Tuple of (model, feature_columns, metadata)
    """
    registry = get_model_registry()

    if version is None:
        version = registry.get_latest_version()

    if version is None:
        raise ValueError("No model versions available")

    metadata = registry.get_metadata(version)
    if metadata is None:
        raise ValueError(f"Model version {version} not found in registry")

    model_path = registry.model_dir / f"ml_model_{version}.joblib"
    if not model_path.exists():
        # Fallback to default path
        model_path = ML_MODEL_PATH

    bundle = joblib.load(model_path)
    return bundle["model"], bundle["feature_columns"], metadata


def detect_feature_drift(current_features: pd.DataFrame, reference_features: pd.DataFrame = None) -> dict:
    """Convenience function to detect drift in production features.

    Args:
        current_features: Current feature DataFrame
        reference_features: Optional reference DataFrame (loads from training data if None)

    Returns:
        Drift detection report
    """
    registry = get_model_registry()
    metadata = registry.get_latest_metadata()

    if metadata is None:
        return {"error": "No model metadata available"}

    if reference_features is None:
        # Load reference features from training data
        # This would need to be implemented based on your data pipeline
        return {"error": "Reference features not provided and no default available"}

    detector = DriftDetector(reference_features, metadata.feature_columns)
    return detector.check_drift(current_features)