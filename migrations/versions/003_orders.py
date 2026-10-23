"""Add the orders table used by the execution router."""

from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("client_order_id", sa.String(32), nullable=False, unique=True),
        sa.Column("position_id", sa.Integer(), index=True),
        sa.Column("symbol", sa.String(20), nullable=False, index=True),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(10), nullable=False),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("product", sa.String(10), nullable=False),
        sa.Column("reference_price", sa.Float(), nullable=False),
        sa.Column("fill_price", sa.Float()),
        sa.Column("status", sa.String(12), nullable=False, index=True),
        sa.Column("broker_order_id", sa.String(40)),
        sa.Column("charges", sa.Float(), nullable=False),
        sa.Column("slippage_bps", sa.Float(), nullable=False),
        sa.Column("reason", sa.String(200)),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("orders")
