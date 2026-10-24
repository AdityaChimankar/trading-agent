"""Guard writable log paths in normal and isolated Docker startup."""

from pathlib import Path, PurePosixPath

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("service", ["api", "scheduler", "ticker", "tools"])
def test_compose_logs_use_persistent_data_mount(service):
    config = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    app = config["services"][service]
    environment = app["environment"]
    log_path = PurePosixPath(environment["LOG_FILE"])
    assert log_path.is_absolute()
    assert log_path.is_relative_to(PurePosixPath(environment["DATA_DIR"]))
    assert "./data:/app/data" in app["volumes"]


def test_smoke_api_exercises_file_logging():
    config = yaml.safe_load(
        (ROOT / "docker-compose.smoke.yml").read_text(encoding="utf-8")
    )
    environment = config["services"]["api"]["environment"]
    log_path = PurePosixPath(environment["LOG_FILE"])
    assert log_path.is_absolute()
    assert log_path.is_relative_to(PurePosixPath(environment["DATA_DIR"]))
