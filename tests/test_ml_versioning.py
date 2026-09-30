"""Unit tests for ML model versioning."""
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import joblib
import numpy as np

from core.ml_versioning import (
    ModelRegistry,
    ModelMetadata,
    DriftDetector,
    get_model_registry,
    get_current_model_version,
    load_model_with_version,
    detect_feature_drift,
)


@pytest.fixture
def temp_model_dir():
    """Create temporary model directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_model_bundle():
    """Create a sample model bundle."""
    from sklearn.ensemble import RandomForestClassifier

    model = RandomForestClassifier(n_estimators=10, random_state=42)
    # Train on dummy data
    X = np.random.randn(100, 5)
    y = np.random.randint(0, 3, 100)
    model.fit(X, y)

    return {
        "model": model,
        "feature_columns": [f"feat_{i}" for i in range(5)],
    }


def test_model_registry_register(temp_model_dir, sample_model_bundle):
    """Test registering a model version."""
    registry = ModelRegistry(temp_model_dir)

    model_path = temp_model_dir / "test_model.joblib"
    joblib.dump(sample_model_bundle, model_path)

    version = registry.register(
        model_path=model_path,
        training_symbols=["RELIANCE", "TCS"],
        training_samples=1000,
        feature_columns=sample_model_bundle["feature_columns"],
        model_params={"n_estimators": 10},
        training_metrics={"accuracy": 0.85},
        notes="Test model",
    )

    assert version.startswith("v")
    assert len(version) > 5  # vYYYYMMDD_HHMMSS_hash

    metadata = registry.get_metadata(version)
    assert metadata is not None
    assert metadata.version == version
    assert metadata.training_symbols == ["RELIANCE", "TCS"]
    assert metadata.training_samples == 1000


def test_model_registry_get_latest(temp_model_dir, sample_model_bundle):
    """Test getting latest model version."""
    registry = ModelRegistry(temp_model_dir)

    # Register two versions
    model_path = temp_model_dir / "test_model.joblib"
    joblib.dump(sample_model_bundle, model_path)

    version1 = registry.register(
        model_path=model_path,
        training_symbols=["RELIANCE"],
        training_samples=100,
        feature_columns=sample_model_bundle["feature_columns"],
        model_params={},
    )

    version2 = registry.register(
        model_path=model_path,
        training_symbols=["TCS"],
        training_samples=200,
        feature_columns=sample_model_bundle["feature_columns"],
        model_params={},
    )

    latest = registry.get_latest_version()
    assert latest == version2  # Should be the second one (newer timestamp)


def test_model_registry_list_versions(temp_model_dir, sample_model_bundle):
    """Test listing model versions."""
    registry = ModelRegistry(temp_model_dir)

    model_path = temp_model_dir / "test_model.joblib"
    joblib.dump(sample_model_bundle, model_path)

    registry.register(
        model_path=model_path,
        training_symbols=["RELIANCE"],
        training_samples=100,
        feature_columns=sample_model_bundle["feature_columns"],
        model_params={},
    )

    versions = registry.list_versions()
    assert len(versions) == 1
    assert isinstance(versions[0], ModelMetadata)


def test_model_registry_cleanup(temp_model_dir, sample_model_bundle):
    """Test cleaning up old versions."""
    import time
    registry = ModelRegistry(temp_model_dir)

    model_path = temp_model_dir / "test_model.joblib"
    joblib.dump(sample_model_bundle, model_path)

    # Register 3 versions with delays to ensure unique timestamps
    # (version uses second precision, so need >1 second between calls)
    versions = []
    for i in range(3):
        v = registry.register(
            model_path=model_path,
            training_symbols=[f"SYM{i}"],
            training_samples=100,
            feature_columns=sample_model_bundle["feature_columns"],
            model_params={},
        )
        versions.append(v)
        time.sleep(1.1)  # Ensure unique timestamps (version uses second precision)

    # Cleanup keeping 1
    removed = registry.cleanup_old_versions(keep=1)
    assert len(removed) == 2
    assert set(removed) == set(versions[:2])

    # Only latest should remain
    remaining = registry.list_versions()
    assert len(remaining) == 1
    assert remaining[0].version == versions[2]


def test_drift_detector():
    """Test DriftDetector."""
    import pandas as pd

    # Reference features
    ref_df = pd.DataFrame({
        "feat_1": np.random.normal(0, 1, 1000),
        "feat_2": np.random.normal(10, 2, 1000),
        "feat_3": np.random.normal(-5, 0.5, 1000),
    })

    detector = DriftDetector(ref_df, ["feat_1", "feat_2", "feat_3"])

    # Current features - same distribution (no drift)
    cur_df = pd.DataFrame({
        "feat_1": np.random.normal(0, 1, 100),
        "feat_2": np.random.normal(10, 2, 100),
        "feat_3": np.random.normal(-5, 0.5, 100),
    })

    report = detector.check_drift(cur_df, threshold=3.0)
    assert report["drift_detected"] is False
    assert len(report["drifted_features"]) == 0


def test_drift_detector_detects_drift():
    """Test DriftDetector detects drift."""
    import pandas as pd

    # Reference features
    ref_df = pd.DataFrame({
        "feat_1": np.random.normal(0, 1, 1000),
        "feat_2": np.random.normal(10, 2, 1000),
    })

    detector = DriftDetector(ref_df, ["feat_1", "feat_2"])

    # Current features - shifted mean (drift)
    cur_df = pd.DataFrame({
        "feat_1": np.random.normal(5, 1, 100),  # Mean shifted by 5 std
        "feat_2": np.random.normal(10, 2, 100),
    })

    report = detector.check_drift(cur_df, threshold=2.0)
    assert report["drift_detected"] is True
    assert "feat_1" in report["drifted_features"]
    assert "feat_2" not in report["drifted_features"]


def test_get_model_registry_singleton():
    """Test get_model_registry returns singleton."""
    registry1 = get_model_registry()
    registry2 = get_model_registry()
    assert registry1 is registry2