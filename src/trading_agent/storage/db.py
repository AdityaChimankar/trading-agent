"""Database layer using SQLAlchemy 2.0."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TypeVar

from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class Base(DeclarativeBase):
    """Base class for all models."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class Watchlist(Base):
    """Watchlist symbols."""

    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(100))
    exchange: Mapped[str] = mapped_column(String(20), default="NSE")
    instrument_token: Mapped[int | None] = mapped_column(Integer)
    lot_size: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(default=True)
    sector: Mapped[str | None] = mapped_column(String(50))
    market_cap: Mapped[float | None] = mapped_column(Float)


class Candle(Base):
    """OHLCV candle data."""

    __tablename__ = "candles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    interval: Mapped[str] = mapped_column(String(20), nullable=False, default="5minute")
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    oi: Mapped[int | None] = mapped_column(Integer)  # Open interest


class Signal(Base):
    """Trading signals."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    interval: Mapped[str] = mapped_column(String(20), default="5minute")
    action: Mapped[str] = mapped_column(String(10), nullable=False)  # BUY, SELL, HOLD
    strategy: Mapped[str] = mapped_column(String(50), nullable=False)  # rule, llm, ml
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    rsi: Mapped[float | None] = mapped_column(Float)
    adx: Mapped[float | None] = mapped_column(Float)
    pattern_bias: Mapped[int] = mapped_column(Integer, default=0)
    signal_metadata: Mapped[str | None] = mapped_column(Text)  # JSON


class Position(Base):
    """Paper trading positions."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(10), nullable=False)  # BUY, SELL
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit: Mapped[float | None] = mapped_column(Float)
    strategy: Mapped[str] = mapped_column(String(50), default="rule")
    status: Mapped[str] = mapped_column(String(20), default="OPEN")  # OPEN, CLOSED, STOPPED
    exit_price: Mapped[float | None] = mapped_column(Float)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pnl: Mapped[float | None] = mapped_column(Float)
    position_metadata: Mapped[str | None] = mapped_column(Text)


class Opportunity(Base):
    """Trading opportunities."""

    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    rank: Mapped[int] = mapped_column(Integer)
    rule_signal: Mapped[str] = mapped_column(String(10))
    llm_signal: Mapped[str] = mapped_column(String(10))
    ml_signal: Mapped[str] = mapped_column(String(10))
    ml_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    risk_reward: Mapped[float | None] = mapped_column(Float)
    atr: Mapped[float | None] = mapped_column(Float)
    opportunity_metadata: Mapped[str | None] = mapped_column(Text)


class MLModel(Base):
    """ML model registry."""

    __tablename__ = "ml_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    model_type: Mapped[str] = mapped_column(String(50), nullable=False)
    feature_version: Mapped[str] = mapped_column(String(20))
    training_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    training_samples: Mapped[int] = mapped_column(Integer)
    features: Mapped[str] = mapped_column(Text)  # JSON
    hyperparameters: Mapped[str] = mapped_column(Text)  # JSON
    metrics: Mapped[str] = mapped_column(Text)  # JSON
    data_hash: Mapped[str] = mapped_column(String(64))
    file_path: Mapped[str] = mapped_column(String(500))
    tags: Mapped[str] = mapped_column(Text)  # JSON
    is_active: Mapped[bool] = mapped_column(default=True)


class Wallet(Base):
    """Paper trading wallet."""

    __tablename__ = "wallet"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    cash: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    invested: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    daily_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    total_pnl: Mapped[float] = mapped_column(Float, default=0.0)


class SystemEvent(Base):
    """System events and alerts."""

    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    level: Mapped[str] = mapped_column(String(20), nullable=False)  # INFO, WARNING, ERROR
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    event_metadata: Mapped[str | None] = mapped_column(Text)


class Database:
    """Database manager."""

    def __init__(self, url: str | None = None, echo: bool = False):
        settings = get_settings()
        self.url = url or settings.database.url
        self.echo = echo or settings.database.echo

        # Async engine
        self.async_engine: AsyncEngine = create_async_engine(
            self.url.replace("sqlite://", "sqlite+aiosqlite://"),
            echo=self.echo,
            pool_pre_ping=True,
        )

        # Sync engine for migrations
        self.sync_engine = create_engine(
            self.url,
            echo=self.echo,
            pool_pre_ping=True,
        )

        # Session factories
        self.async_session_factory = async_sessionmaker(
            self.async_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        self.sync_session_factory = sessionmaker(bind=self.sync_engine)

    async def create_tables(self) -> None:
        """Create all tables."""
        async with self.async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("database_tables_created")

    async def drop_tables(self) -> None:
        """Drop all tables."""
        async with self.async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        logger.info("database_tables_dropped")

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """Get async session."""
        async with self.async_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    def get_sync_session(self):
        """Get sync session (for migrations/scripts)."""
        return self.sync_session_factory()

    async def close(self) -> None:
        """Close database connections."""
        await self.async_engine.dispose()
        self.sync_engine.dispose()
        logger.info("database_closed")


# Global database instance
_db: Database | None = None


def get_database(url: str | None = None, echo: bool = False) -> Database:
    """Get or create the global database instance."""
    global _db
    if _db is None:
        _db = Database(url, echo)
    return _db


# Repository base class
class BaseRepository:
    """Base repository with common CRUD operations."""

    def __init__(self, session: AsyncSession, model: type[T]):
        self.session = session
        self.model = model

    async def create(self, **kwargs) -> T:
        instance = self.model(**kwargs)
        self.session.add(instance)
        await self.session.flush()
        return instance

    async def get_by_id(self, id: int) -> T | None:
        return await self.session.get(self.model, id)

    async def get_all(self, limit: int = 100, offset: int = 0) -> list[T]:
        result = await self.session.execute(select(self.model).limit(limit).offset(offset))
        return list(result.scalars().all())

    async def update(self, id: int, **kwargs) -> T | None:
        instance = await self.get_by_id(id)
        if instance:
            for key, value in kwargs.items():
                setattr(instance, key, value)
            await self.session.flush()
        return instance

    async def delete(self, id: int) -> bool:
        instance = await self.get_by_id(id)
        if instance:
            await self.session.delete(instance)
            await self.session.flush()
            return True
        return False


# Specific repositories
class WatchlistRepository(BaseRepository):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Watchlist)

    async def get_by_symbol(self, symbol: str) -> Watchlist | None:
        result = await self.session.execute(select(Watchlist).where(Watchlist.symbol == symbol))
        return result.scalar_one_or_none()

    async def get_active(self) -> list[Watchlist]:
        result = await self.session.execute(select(Watchlist).where(Watchlist.is_active))
        return list(result.scalars().all())


class CandleRepository(BaseRepository):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Candle)

    async def get_latest(
        self, symbol: str, interval: str = "5minute", limit: int = 1
    ) -> list[Candle]:
        result = await self.session.execute(
            select(Candle)
            .where(Candle.symbol == symbol, Candle.interval == interval)
            .order_by(Candle.timestamp.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_range(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        interval: str = "5minute",
    ) -> list[Candle]:
        result = await self.session.execute(
            select(Candle)
            .where(
                Candle.symbol == symbol,
                Candle.interval == interval,
                Candle.timestamp >= start,
                Candle.timestamp <= end,
            )
            .order_by(Candle.timestamp.asc())
        )
        return list(result.scalars().all())


class SignalRepository(BaseRepository):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Signal)

    async def get_latest(self, symbol: str, strategy: str | None = None) -> Signal | None:
        query = select(Signal).where(Signal.symbol == symbol).order_by(Signal.timestamp.desc())
        if strategy:
            query = query.where(Signal.strategy == strategy)
        result = await self.session.execute(query.limit(1))
        return result.scalar_one_or_none()


class PositionRepository(BaseRepository):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Position)

    async def get_open(self, symbol: str | None = None) -> list[Position]:
        query = select(Position).where(Position.status == "OPEN")
        if symbol:
            query = query.where(Position.symbol == symbol)
        result = await self.session.execute(query)
        return list(result.scalars().all())


class OpportunityRepository(BaseRepository):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Opportunity)

    async def get_latest_opportunities(self, limit: int = 50) -> list[Opportunity]:
        result = await self.session.execute(
            select(Opportunity)
            .order_by(Opportunity.timestamp.desc(), Opportunity.rank.asc())
            .limit(limit)
        )
        return list(result.scalars().all())


class MLModelRepository(BaseRepository):
    def __init__(self, session: AsyncSession):
        super().__init__(session, MLModel)

    async def get_active(self, symbol: str, model_type: str) -> MLModel | None:
        result = await self.session.execute(
            select(MLModel)
            .where(
                MLModel.symbol == symbol,
                MLModel.model_type == model_type,
                MLModel.is_active,
            )
            .order_by(MLModel.training_date.desc())
        )
        return result.scalar_one_or_none()
