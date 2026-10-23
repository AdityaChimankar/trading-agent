"""Align previously deployed revision 001 databases with the canonical schema.

Some installations have the original physical `metadata` column names and no
trades.updated_at. Fresh revision 001 databases already have the canonical names.
"""

from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None

METADATA_COLUMNS = {
    "open_positions": "position_metadata",
    "signals": "signal_metadata",
    "opportunities": "opportunity_metadata",
    "system_events": "event_metadata",
}


def upgrade() -> None:
    connection = op.get_bind()
    # Validate the whole repair before changing anything. Never discard an
    # ambiguous metadata column or silently manufacture missing business data.
    columns = {
        table: {column["name"] for column in sa.inspect(connection).get_columns(table)}
        for table in [*METADATA_COLUMNS, "trades"]
    }
    for table, canonical in METADATA_COLUMNS.items():
        present = columns[table]
        if ("metadata" in present) == (canonical in present):
            raise RuntimeError(f"Ambiguous or missing metadata column in {table}; inspect schema before migrating")
    if "created_at" not in columns["trades"]:
        raise RuntimeError("trades.created_at is required to backfill updated_at")

    for table, canonical in METADATA_COLUMNS.items():
        if "metadata" in columns[table]:
            op.alter_column(table, "metadata", new_column_name=canonical, existing_type=sa.Text())

    if "updated_at" not in columns["trades"]:
        op.add_column("trades", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
        op.execute(sa.text("UPDATE trades SET updated_at = created_at"))
        with op.batch_alter_table("trades") as batch:
            batch.alter_column("updated_at", existing_type=sa.DateTime(timezone=True), nullable=False)


def downgrade() -> None:
    # The canonical revision 001 schema already has these columns. Keep them,
    # including all metadata and timestamps, when downgrading the revision stamp.
    pass
