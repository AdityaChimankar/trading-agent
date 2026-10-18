"""Initial migration - create all tables.

Revision ID: 001
Revises: 
Create Date: 2024-01-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Watchlist
    op.create_table(
        'watchlist',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('symbol', sa.String(20), nullable=False),
        sa.Column('name', sa.String(100), nullable=True),
        sa.Column('exchange', sa.String(20), nullable=False, server_default='NSE'),
        sa.Column('instrument_token', sa.Integer(), nullable=True),
        sa.Column('lot_size', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('sector', sa.String(50), nullable=True),
        sa.Column('market_cap', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('symbol'),
    )
    op.create_index('ix_watchlist_symbol', 'watchlist', ['symbol'])

    # Candles
    op.create_table(
        'candles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('symbol', sa.String(20), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('interval', sa.String(20), nullable=False, server_default='5minute'),
        sa.Column('open', sa.Float(), nullable=False),
        sa.Column('high', sa.Float(), nullable=False),
        sa.Column('low', sa.Float(), nullable=False),
        sa.Column('close', sa.Float(), nullable=False),
        sa.Column('volume', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('oi', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_candles_symbol_timestamp', 'candles', ['symbol', 'timestamp'])
    op.create_index('ix_candles_symbol_interval_timestamp', 'candles', ['symbol', 'interval', 'timestamp'])

    # Signals
    op.create_table(
        'signals',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('symbol', sa.String(20), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('interval', sa.String(20), nullable=False, server_default='5minute'),
        sa.Column('action', sa.String(10), nullable=False),
        sa.Column('strategy', sa.String(50), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('rsi', sa.Float(), nullable=True),
        sa.Column('adx', sa.Float(), nullable=True),
        sa.Column('pattern_bias', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('metadata', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_signals_symbol_timestamp', 'signals', ['symbol', 'timestamp'])

    # Positions
    op.create_table(
        'positions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('symbol', sa.String(20), nullable=False),
        sa.Column('action', sa.String(10), nullable=False),
        sa.Column('entry_price', sa.Float(), nullable=False),
        sa.Column('entry_time', sa.DateTime(timezone=True), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('stop_loss', sa.Float(), nullable=False),
        sa.Column('take_profit', sa.Float(), nullable=True),
        sa.Column('strategy', sa.String(50), nullable=False, server_default='rule'),
        sa.Column('status', sa.String(20), nullable=False, server_default='OPEN'),
        sa.Column('exit_price', sa.Float(), nullable=True),
        sa.Column('exit_time', sa.DateTime(timezone=True), nullable=True),
        sa.Column('pnl', sa.Float(), nullable=True),
        sa.Column('metadata', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_positions_symbol_status', 'positions', ['symbol', 'status'])

    # Opportunities
    op.create_table(
        'opportunities',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('symbol', sa.String(20), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('score', sa.Float(), nullable=False),
        sa.Column('rank', sa.Integer(), nullable=True),
        sa.Column('rule_signal', sa.String(10), nullable=True),
        sa.Column('llm_signal', sa.String(10), nullable=True),
        sa.Column('ml_signal', sa.String(10), nullable=True),
        sa.Column('ml_confidence', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('risk_reward', sa.Float(), nullable=True),
        sa.Column('atr', sa.Float(), nullable=True),
        sa.Column('metadata', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_opportunities_timestamp_rank', 'opportunities', ['timestamp', 'rank'])

    # ML Models
    op.create_table(
        'ml_models',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('version', sa.String(100), nullable=False),
        sa.Column('symbol', sa.String(20), nullable=False),
        sa.Column('model_type', sa.String(50), nullable=False),
        sa.Column('feature_version', sa.String(20), nullable=True),
        sa.Column('training_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('training_samples', sa.Integer(), nullable=True),
        sa.Column('features', sa.Text(), nullable=True),
        sa.Column('hyperparameters', sa.Text(), nullable=True),
        sa.Column('metrics', sa.Text(), nullable=True),
        sa.Column('data_hash', sa.String(64), nullable=True),
        sa.Column('file_path', sa.String(500), nullable=True),
        sa.Column('tags', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('version'),
    )
    op.create_index('ix_ml_models_symbol_type', 'ml_models', ['symbol', 'model_type'])

    # Wallet
    op.create_table(
        'wallet',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('cash', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('invested', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('total_value', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('daily_pnl', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('total_pnl', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_wallet_timestamp', 'wallet', ['timestamp'])

    # System Events
    op.create_table(
        'system_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('level', sa.String(20), nullable=False),
        sa.Column('source', sa.String(50), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('metadata', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_system_events_timestamp', 'system_events', ['timestamp'])


def downgrade() -> None:
    op.drop_table('system_events')
    op.drop_table('wallet')
    op.drop_table('ml_models')
    op.drop_table('opportunities')
    op.drop_table('positions')
    op.drop_table('signals')
    op.drop_table('candles')
    op.drop_table('watchlist')