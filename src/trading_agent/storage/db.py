"""Database layer using SQLAlchemy 2.0."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar

from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    event,
    select,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, synonym


@dataclass
class PositionRow:
    """Simple position row matching test database schema."""
    id: int
    symbol: str
    action: str
    entry_price: float
    position_size: int
    position_value: float
    risk_amount: float
    stop_loss: float
    take_profit: float | None
    opened_at: datetime
    status: str
    signal_source: str
    exit_price: float | None
    exit_time: datetime | None
    pnl: float | None
    position_metadata: str | None
    
    # Properties for compatibility with position_monitor code
    @property
    def quantity(self) -> int:
        return self.position_size
    
    @property
    def entry_time(self) -> datetime:
        return self.opened_at
    
    @property
    def strategy(self) -> str:
        return self.signal_source

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger

logger = get_logger(__name__)

# Database path constant for compatibility with tests and health checks
DB_PATH = get_settings().database.url.replace("sqlite:///", "").replace("sqlite://", "")

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

    __tablename__ = "open_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(10), nullable=False)  # BUY, SELL
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    position_size: Mapped[int] = mapped_column(Integer, nullable=False)
    position_value: Mapped[float] = mapped_column(Float, nullable=False)
    risk_amount: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit: Mapped[float | None] = mapped_column(Float)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open")
    signal_source: Mapped[str] = mapped_column(String(50), default="rule")
    exit_price: Mapped[float | None] = mapped_column(Float)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pnl: Mapped[float | None] = mapped_column(Float)
    position_metadata: Mapped[str | None] = mapped_column(Text)

    # Backwards-compatible Python names used by the API and risk layer. The
    # physical schema has one canonical set of names shared with init_db and
    # Alembic.
    quantity = synonym("position_size")
    entry_time = synonym("opened_at")
    strategy = synonym("signal_source")


class Opportunity(Base):
    """Trading opportunities."""

    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    rank: Mapped[int] = mapped_column(Integer)
    rule_signal: Mapped[str] = mapped_column(String(10))
    llm_signal: Mapped[str | None] = mapped_column(String(10))
    ml_signal: Mapped[str | None] = mapped_column(String(10))
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


class PositionAlert(Base):
    """Position monitoring alerts."""

    __tablename__ = "position_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # stop, target, reversal, stale, no_data
    recommendation: Mapped[str] = mapped_column(String(10), nullable=False)  # BUY, SELL, HOLD
    candle_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )


class Trade(Base):
    """Executed trades."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(10), nullable=False)  # BUY, SELL
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    gross_pnl: Mapped[float] = mapped_column(Float, nullable=False)
    charges: Mapped[float] = mapped_column(Float, nullable=False)
    net_pnl: Mapped[float] = mapped_column(Float, nullable=False)
    holding_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Order(Base):
    """Every order the router sends (paper or live), with its lifecycle state.

    PENDING -> SUBMITTED -> FILLED | REJECTED | CANCELLED. ``charges`` is the
    modelled statutory + brokerage cost of the fill; ``client_order_id`` makes
    retries idempotent and ties broker order ids back to this row.
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    position_id: Mapped[int | None] = mapped_column(Integer, index=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # BUY, SELL
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(10), nullable=False)  # entry, exit
    mode: Mapped[str] = mapped_column(String(10), nullable=False)  # paper, live
    product: Mapped[str] = mapped_column(String(10), nullable=False, default="MIS")
    reference_price: Mapped[float] = mapped_column(Float, nullable=False)
    fill_price: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(40))
    charges: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    slippage_bps: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reason: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(Text)


def apply_sqlite_pragmas(conn) -> None:
    """WAL lets the ticker writer, scheduler and API read/write concurrently
    without "database is locked"; busy_timeout absorbs short write contention."""
    cur = conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
    finally:
        cur.close()


def _sqlite_on_connect(dbapi_conn, _record) -> None:
    apply_sqlite_pragmas(dbapi_conn)


class Database:
    """Database manager."""

    def __init__(self, url: str | None = None, echo: bool = False):
        settings = get_settings()
        self.url = url or settings.database.url
        self.echo = echo or settings.database.echo

        async_url = self.url
        if async_url.startswith("sqlite://"):
            async_url = async_url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        elif async_url.startswith("postgresql://"):
            async_url = async_url.replace("postgresql://", "postgresql+asyncpg://", 1)

        # Async engine
        self.async_engine: AsyncEngine = create_async_engine(
            async_url,
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

        if self.url.startswith("sqlite"):
            event.listen(self.sync_engine, "connect", _sqlite_on_connect)
            event.listen(self.async_engine.sync_engine, "connect", _sqlite_on_connect)

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
        # Use the current DB_PATH (which may be monkeypatched in tests)
        if url is None:
            import sys
            module_globals = sys.modules[__name__].__dict__
            url = module_globals.get("DB_PATH", get_settings().database.url)
            # A monkeypatched filesystem path is used by tests. Preserve real
            # database URLs instead of accidentally wrapping PostgreSQL URLs
            # in a SQLite URL.
            if "://" not in str(url):
                url = f"sqlite:///{url}"
        _db = Database(url, echo)
    return _db


def reset_database() -> None:
    """Reset the global database instance (for testing)."""
    global _db
    if _db is not None:
        # Close the old engine
        import asyncio
        try:
            asyncio.run(_db.close())
        except Exception:
            pass
    _db = None


def init_db(db_path: str | None = None) -> None:
    """Initialize database tables synchronously (for testing)."""
    import sqlite3
    import sys
    
    # Use the provided path, or read the current DB_PATH from module globals
    # This allows monkeypatching to work correctly
    if db_path is None:
        # Read from module globals to pick up monkeypatched value
        module_globals = sys.modules[__name__].__dict__
        db_path = module_globals.get("DB_PATH", DB_PATH)
    
    # Ensure path is a string
    path = str(db_path)
    
    conn = sqlite3.connect(path)
    apply_sqlite_pragmas(conn)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT UNIQUE NOT NULL,
                name TEXT,
                exchange TEXT DEFAULT 'NSE',
                instrument_token INTEGER,
                lot_size INTEGER DEFAULT 1,
                is_active BOOLEAN DEFAULT 1,
                sector TEXT,
                market_cap REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS candles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                interval TEXT DEFAULT '5minute',
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume INTEGER DEFAULT 0,
                oi INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_candles_symbol_timestamp ON candles(symbol, timestamp);

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                interval TEXT DEFAULT '5minute',
                action TEXT NOT NULL,
                strategy TEXT NOT NULL DEFAULT 'rule',
                confidence REAL DEFAULT 0.0,
                rsi REAL,
                adx REAL,
                pattern_bias INTEGER DEFAULT 0,
                signal_metadata TEXT,
                rationale TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_signals_symbol_timestamp ON signals(symbol, timestamp);

            CREATE TABLE IF NOT EXISTS open_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                entry_price REAL NOT NULL,
                position_size INTEGER NOT NULL,
                position_value REAL NOT NULL,
                risk_amount REAL NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit REAL,
                opened_at TIMESTAMP NOT NULL,
                status TEXT DEFAULT 'open',
                signal_source TEXT DEFAULT 'rule',
                exit_price REAL,
                exit_time TIMESTAMP,
                pnl REAL,
                position_metadata TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                score REAL NOT NULL,
                rank INTEGER,
                rule_signal TEXT,
                llm_signal TEXT,
                ml_signal TEXT,
                ml_confidence REAL DEFAULT 0.0,
                risk_reward REAL,
                atr REAL,
                opportunity_metadata TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ml_models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT UNIQUE NOT NULL,
                symbol TEXT NOT NULL,
                model_type TEXT NOT NULL,
                feature_version TEXT,
                training_date TIMESTAMP NOT NULL,
                training_samples INTEGER,
                features TEXT,
                hyperparameters TEXT,
                metrics TEXT,
                data_hash TEXT,
                file_path TEXT,
                tags TEXT,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS wallet (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                cash REAL DEFAULT 0.0,
                invested REAL DEFAULT 0.0,
                total_value REAL DEFAULT 0.0,
                daily_pnl REAL DEFAULT 0.0,
                total_pnl REAL DEFAULT 0.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS system_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                level TEXT NOT NULL,
                source TEXT NOT NULL,
                message TEXT NOT NULL,
                event_metadata TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS position_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                kind TEXT NOT NULL,
                recommendation TEXT NOT NULL,
                candle_timestamp TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                gross_pnl REAL NOT NULL,
                charges REAL NOT NULL,
                net_pnl REAL NOT NULL,
                holding_minutes INTEGER NOT NULL,
                entry_time TIMESTAMP NOT NULL,
                exit_time TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
    finally:
        conn.close()


def get_connection():
    """Get a synchronous sqlite3 connection (for testing)."""
    import sqlite3
    import sys
    
    # Read current DB_PATH from module globals to support monkeypatching
    module_globals = sys.modules[__name__].__dict__
    path = str(module_globals.get("DB_PATH", DB_PATH))
    
    conn = sqlite3.connect(path)
    apply_sqlite_pragmas(conn)
    conn.row_factory = sqlite3.Row
    return conn


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

    async def get_open(self, symbol: str | None = None) -> list[PositionRow]:
        """Get open positions using raw SQL to match test schema."""
        from sqlalchemy import text
        
        sql = """
            SELECT id, symbol, action, entry_price, position_size, position_value,
                   risk_amount, stop_loss, take_profit, opened_at, status,
                   signal_source, exit_price, exit_time, pnl, position_metadata
            FROM open_positions
            WHERE lower(status) = 'open'
        """
        params = {}
        if symbol:
            sql += " AND symbol = :symbol"
            params["symbol"] = symbol
        
        result = await self.session.execute(text(sql), params)
        rows = result.fetchall()
        
        # Convert rows to PositionRow objects
        positions = []
        for row in rows:
            pos = PositionRow(
                id=row.id,
                symbol=row.symbol,
                action=row.action,
                entry_price=row.entry_price,
                position_size=row.position_size,
                position_value=row.position_value,
                risk_amount=row.risk_amount,
                stop_loss=row.stop_loss,
                take_profit=row.take_profit,
                opened_at=row.opened_at,
                status=row.status,
                signal_source=row.signal_source,
                exit_price=row.exit_price,
                exit_time=row.exit_time,
                pnl=row.pnl,
                position_metadata=row.position_metadata,
            )
            positions.append(pos)
        
        return positions

    async def get_by_status(self, status: str, symbol: str | None = None) -> list[Position]:
        """Get ORM positions by case-insensitive status."""
        from sqlalchemy import func

        query = select(Position).where(func.lower(Position.status) == status.lower())
        if symbol:
            query = query.where(Position.symbol == symbol.upper())
        result = await self.session.execute(query.order_by(Position.opened_at.desc()))
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
