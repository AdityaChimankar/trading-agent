"""Configuration management using Pydantic Settings."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Database configuration."""

    model_config = SettingsConfigDict(
        env_prefix="DB_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    url: str = Field(
        default="sqlite:///data/trading.db",
        description="SQLAlchemy database URL",
    )
    echo: bool = Field(default=False, description="Echo SQL queries")
    pool_size: int = Field(default=5, description="Connection pool size")
    max_overflow: int = Field(default=10, description="Max overflow connections")


class KiteSettings(BaseSettings):
    """Kite Connect API configuration."""

    model_config = SettingsConfigDict(
        env_prefix="KITE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    api_key: str = Field(default="", description="Kite API key")
    api_secret: str = Field(default="", description="Kite API secret")
    access_token: str = Field(default="", description="Kite access token")
    redirect_url: str = Field(default="http://localhost", description="OAuth redirect URL")
    user_id: str = Field(default="", description="Kite user ID")
    password: str = Field(default="", description="Kite password")
    totp_secret: str = Field(default="", description="TOTP secret for 2FA")
    login_headless: bool = Field(default=False, description="Run automated login headlessly")

    @property
    def is_configured(self) -> bool:
        """Check if all required credentials are set."""
        return all(
            [
                self.api_key,
                self.api_secret,
                self.access_token or self.totp_secret,
            ]
        )


class StorageSettings(BaseSettings):
    """Data storage configuration."""

    model_config = SettingsConfigDict(
        env_prefix="STORAGE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    parquet_root: Path = Field(
        default=Path("data/parquet"),
        description="Root directory for Parquet files",
    )
    candle_interval: Literal["minute", "1minute", "5minute", "15minute"] = Field(
        default="5minute",
        description="Default candle interval",
    )
    compression: Literal["snappy", "gzip", "zstd", "lz4"] = Field(
        default="snappy",
        description="Parquet compression codec",
    )


class APISettings(BaseSettings):
    """FastAPI server configuration."""

    model_config = SettingsConfigDict(
        env_prefix="API_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    host: str = Field(default="127.0.0.1", description="Server host")
    auth_token: str = Field(default="", repr=False, description="Optional bearer token for private API access")
    port: int = Field(default=8000, description="Server port")
    reload: bool = Field(default=False, description="Auto-reload on changes")
    workers: int = Field(default=1, description="Number of workers")
    cors_origins: list[str] = Field(
        default=["http://localhost:3000", "http://localhost:5173"],
        description="CORS allowed origins",
    )


class PaperRiskSettings(BaseSettings):
    """Limits for database-backed paper positions; never broker orders."""

    model_config = SettingsConfigDict(env_prefix="PAPER_", env_file=".env", extra="ignore")
    initial_capital: float = Field(default=1_000_000, gt=0, allow_inf_nan=False)
    entries_enabled: bool = True
    max_positions: int = Field(default=10, ge=1)
    max_single_position_pct: float = Field(default=0.10, gt=0, le=1)
    max_exposure_pct: float = Field(default=0.5, gt=0, le=1)
    max_risk_pct: float = Field(default=0.06, gt=0, le=1)
    max_trade_risk_pct: float = Field(default=0.02, gt=0, le=1)
    max_daily_loss_pct: float = Field(default=0.03, gt=0, le=1)
    max_drawdown_pct: float = Field(default=0.15, gt=0, le=1)


class MLSettings(BaseSettings):
    """Machine Learning configuration."""

    model_config = SettingsConfigDict(
        env_prefix="ML_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    model_dir: Path = Field(
        default=Path("models"),
        description="Directory for trained models",
    )
    feature_version: str = Field(default="v2", description="Feature engineering version")
    retrain_schedule: str = Field(default="0 2 * * 1", description="Cron for weekly retrain")
    min_train_samples: int = Field(default=1000, description="Minimum samples for training")
    confidence_threshold: float = Field(default=0.55, description="Default confidence threshold")


class LoggingSettings(BaseSettings):
    """Logging configuration."""

    model_config = SettingsConfigDict(
        env_prefix="LOG_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Log level",
    )
    format: Literal["json", "console"] = Field(
        default="console",
        description="Log format",
    )
    file: Path | None = Field(default=None, description="Log file path")


class LLMSettings(BaseSettings):
    """Optional OpenAI-compatible LLM configuration."""

    model_config = SettingsConfigDict(
        env_prefix="LLM_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    provider: Literal["openai", "anthropic", "mock"] = "mock"
    base_url: str = "https://api.openai.com"
    api_key: str = ""
    model: str = "gpt-4o-mini"


class Settings(BaseSettings):
    """Main application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Environment
    environment: Literal["development", "testing", "staging", "production"] = Field(
        default="development",
        description="Runtime environment",
    )
    debug: bool = Field(default=False, description="Debug mode")

    # Sub-settings
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    kite: KiteSettings = Field(default_factory=KiteSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    api: APISettings = Field(default_factory=APISettings)
    paper: PaperRiskSettings = Field(default_factory=PaperRiskSettings)
    ml: MLSettings = Field(default_factory=MLSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)

    # Paths
    data_dir: Path = Field(default=Path("data"), description="Data directory")
    cache_dir: Path = Field(default=Path(".cache"), description="Cache directory")

    @field_validator("data_dir", "cache_dir", mode="before")
    @classmethod
    def _resolve_path(cls, v: str | Path) -> Path:
        return Path(v).expanduser().resolve()

    @field_validator("debug", mode="before")
    @classmethod
    def _parse_debug(cls, value: object) -> object:
        """Tolerate common process-level DEBUG values such as ``release``.

        ``DEBUG`` is a generic environment variable used by many developer
        tools. Treating an unrelated value as a fatal application setting made
        even imports fail on otherwise valid machines.
        """
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"release", "production", "prod", "off", "no"}:
                return False
            if normalized in {"debug", "development", "dev", "on", "yes"}:
                return True
        return value

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Ensure directories exist
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.storage.parquet_root.mkdir(parents=True, exist_ok=True)
        self.ml.model_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
