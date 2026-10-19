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
    )
    op.create_index('ix_watchlist_symbol', 'watchlist', ['symbol'], unique=True)

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
    op.create_index('ix_candles_symbol', 'candles', ['symbol'])
    op.create_index('ix_candles_timestamp', 'candles', ['timestamp'])

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
        sa.Column('signal_metadata', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_signals_symbol', 'signals', ['symbol'])
    op.create_index('ix_signals_timestamp', 'signals', ['timestamp'])

    # Positions (table name: open_positions to match model)
    op.create_table(
        'open_positions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('symbol', sa.String(20), nullable=False),
        sa.Column('action', sa.String(10), nullable=False),
        sa.Column('entry_price', sa.Float(), nullable=False),
        sa.Column('position_size', sa.Integer(), nullable=False),
        sa.Column('position_value', sa.Float(), nullable=False),
        sa.Column('risk_amount', sa.Float(), nullable=False),
        sa.Column('stop_loss', sa.Float(), nullable=False),
        sa.Column('take_profit', sa.Float(), nullable=True),
        sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='open'),
        sa.Column('signal_source', sa.String(50), nullable=False, server_default='rule'),
        sa.Column('exit_price', sa.Float(), nullable=True),
        sa.Column('exit_time', sa.DateTime(timezone=True), nullable=True),
        sa.Column('pnl', sa.Float(), nullable=True),
        sa.Column('position_metadata', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_open_positions_symbol', 'open_positions', ['symbol'])

    # Position Alerts
    op.create_table(
        'position_alerts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('position_id', sa.Integer(), nullable=False),
        sa.Column('symbol', sa.String(20), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('recommendation', sa.String(10), nullable=False),
        sa.Column('candle_timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_position_alerts_position_id', 'position_alerts', ['position_id'])
    op.create_index('ix_position_alerts_symbol', 'position_alerts', ['symbol'])

    # Trades
    op.create_table(
        'trades',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('position_id', sa.Integer(), nullable=False),
        sa.Column('symbol', sa.String(20), nullable=False),
        sa.Column('action', sa.String(10), nullable=False),
        sa.Column('entry_price', sa.Float(), nullable=False),
        sa.Column('exit_price', sa.Float(), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('gross_pnl', sa.Float(), nullable=False),
        sa.Column('charges', sa.Float(), nullable=False),
        sa.Column('net_pnl', sa.Float(), nullable=False),
        sa.Column('holding_minutes', sa.Integer(), nullable=False),
        sa.Column('entry_time', sa.DateTime(timezone=True), nullable=False),
        sa.Column('exit_time', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_trades_position_id', 'trades', ['position_id'])
    op.create_index('ix_trades_symbol', 'trades', ['symbol'])

    # Opportunities
    op.create_table(
        'opportunities',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('symbol', sa.String(20), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('score', sa.Float(), nullable=False),
        sa.Column('rank', sa.Integer(), nullable=False),
        sa.Column('rule_signal', sa.String(10), nullable=False),
        sa.Column('llm_signal', sa.String(10), nullable=True),
        sa.Column('ml_signal', sa.String(10), nullable=True),
        sa.Column('ml_confidence', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('risk_reward', sa.Float(), nullable=True),
        sa.Column('atr', sa.Float(), nullable=True),
        sa.Column('opportunity_metadata', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_opportunities_symbol', 'opportunities', ['symbol'])
    op.create_index('ix_opportunities_timestamp', 'opportunities', ['timestamp'])

    # ML Models
    op.create_table(
        'ml_models',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('version', sa.String(100), nullable=False),
        sa.Column('symbol', sa.String(20), nullable=False),
        sa.Column('model_type', sa.String(50), nullable=False),
        sa.Column('feature_version', sa.String(20), nullable=False),
        sa.Column('training_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('training_samples', sa.Integer(), nullable=False),
        sa.Column('features', sa.Text(), nullable=False),
        sa.Column('hyperparameters', sa.Text(), nullable=False),
        sa.Column('metrics', sa.Text(), nullable=False),
        sa.Column('data_hash', sa.String(64), nullable=False),
        sa.Column('file_path', sa.String(500), nullable=False),
        sa.Column('tags', sa.Text(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('version'),
    )
    op.create_index('ix_ml_models_symbol', 'ml_models', ['symbol'])

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

    # System Events
    op.create_table(
        'system_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('level', sa.String(20), nullable=False),
        sa.Column('source', sa.String(50), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('event_metadata', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('system_events')
    op.drop_table('wallet')
    op.drop_table('ml_models')
    op.drop_table('opportunities')
    op.drop_table('trades')
    op.drop_table('position_alerts')
    op.drop_table('open_positions')
    op.drop_table('signals')
    op.drop_table('candles')
    op.drop_table('watchlist')
