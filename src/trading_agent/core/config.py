"""Configuration management using Pydantic Settings."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Database configuration."""

    model_config = SettingsConfigDict(env_prefix="DB_")

    url: str = Field(
        default="sqlite:///data/trading.db",
        description="SQLAlchemy database URL",
    )
    echo: bool = Field(default=False, description="Echo SQL queries")
    pool_size: int = Field(default=5, description="Connection pool size")
    max_overflow: int = Field(default=10, description="Max overflow connections")


class KiteSettings(BaseSettings):
    """Kite Connect API configuration."""

    model_config = SettingsConfigDict(env_prefix="KITE_")

    api_key: str = Field(default="", description="Kite API key")
    api_secret: str = Field(default="", description="Kite API secret")
    access_token: str = Field(default="", description="Kite access token")
    redirect_url: str = Field(default="http://localhost", description="OAuth redirect URL")
    user_id: str = Field(default="", description="Kite user ID")
    password: str = Field(default="", description="Kite password")
    totp_secret: str = Field(default="", description="TOTP secret for 2FA")

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

    model_config = SettingsConfigDict(env_prefix="STORAGE_")

    parquet_root: Path = Field(
        default=Path("data/parquet"),
        description="Root directory for Parquet files",
    )
    candle_interval: Literal["1minute", "5minute", "15minute"] = Field(
        default="5minute",
        description="Default candle interval",
    )
    compression: Literal["snappy", "gzip", "zstd", "lz4"] = Field(
        default="snappy",
        description="Parquet compression codec",
    )


class APISettings(BaseSettings):
    """FastAPI server configuration."""

    model_config = SettingsConfigDict(env_prefix="API_")

    host: str = Field(default="0.0.0.0", description="Server host")
    port: int = Field(default=8000, description="Server port")
    reload: bool = Field(default=False, description="Auto-reload on changes")
    workers: int = Field(default=1, description="Number of workers")
    cors_origins: list[str] = Field(
        default=["http://localhost:3000", "http://localhost:5173"],
        description="CORS allowed origins",
    )


class MLSettings(BaseSettings):
    """Machine Learning configuration."""

    model_config = SettingsConfigDict(env_prefix="ML_")

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

    model_config = SettingsConfigDict(env_prefix="LOG_")

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Log level",
    )
    format: Literal["json", "console"] = Field(
        default="console",
        description="Log format",
    )
    file: Path | None = Field(default=None, description="Log file path")


class Settings(BaseSettings):
    """Main application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Environment
    environment: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Runtime environment",
    )
    debug: bool = Field(default=False, description="Debug mode")

    # Sub-settings
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    kite: KiteSettings = Field(default_factory=KiteSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    api: APISettings = Field(default_factory=APISettings)
    ml: MLSettings = Field(default_factory=MLSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    # Paths
    data_dir: Path = Field(default=Path("data"), description="Data directory")
    cache_dir: Path = Field(default=Path(".cache"), description="Cache directory")

    @field_validator("data_dir", "cache_dir", mode="before")
    @classmethod
    def _resolve_path(cls, v: str | Path) -> Path:
        return Path(v).expanduser().resolve()

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
