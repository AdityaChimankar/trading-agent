"""Deterministic primitives shared by everything above: indicators, patterns, sizing."""

from core.logging_config import get_logger, setup_logging, log_decision_cycle, log_error_with_context, log_timed_operation
from core.config import get_config, reload_config, Config
from core.shutdown import (
    register_shutdown_handler,
    unregister_shutdown_handler,
    setup_signal_handlers,
    wait_for_shutdown,
    is_shutting_down,
    GracefulShutdown,
)
from core.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    CircuitOpenError,
    get_kite_circuit,
    get_openrouter_circuit,
    call_with_kite_circuit,
    call_with_openrouter_circuit,
    get_circuit_status,
)
from core.ml_versioning import (
    ModelRegistry,
    ModelMetadata,
    DriftDetector,
    get_model_registry,
    get_current_model_version,
    load_model_with_version,
    detect_feature_drift,
)

__all__ = [
    "get_logger",
    "setup_logging",
    "log_decision_cycle",
    "log_error_with_context",
    "log_timed_operation",
    "get_config",
    "reload_config",
    "Config",
    "register_shutdown_handler",
    "unregister_shutdown_handler",
    "setup_signal_handlers",
    "wait_for_shutdown",
    "is_shutting_down",
    "GracefulShutdown",
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitState",
    "CircuitOpenError",
    "get_kite_circuit",
    "get_openrouter_circuit",
    "call_with_kite_circuit",
    "call_with_openrouter_circuit",
    "get_circuit_status",
    "ModelRegistry",
    "ModelMetadata",
    "DriftDetector",
    "get_model_registry",
    "get_current_model_version",
    "load_model_with_version",
    "detect_feature_drift",
]
