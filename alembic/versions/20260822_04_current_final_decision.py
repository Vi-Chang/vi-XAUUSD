"""single current final decision and superseded notification guard

Revision ID: 20260822_04
Revises: 20260821_03
"""
import sqlalchemy as sa

from alembic import op

revision = "20260822_04"
down_revision = "20260821_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "current_final_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False, unique=True),
        sa.Column("decision_id", sa.String(64), nullable=False, unique=True),
        sa.Column("decision_version", sa.Integer(), nullable=False),
        sa.Column("decision_signature", sa.String(64), nullable=False),
        sa.Column("scenario_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("scenario_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("lineage_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False, server_default="NEUTRAL"),
        sa.Column("source_candle_close_time", sa.String(64), nullable=False, server_default=""),
        sa.Column("source_data_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evaluated_at", sa.String(64), nullable=False, server_default=""),
        sa.Column("supersedes_decision_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "decision_conflict_audits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False, server_default="XAUUSD"),
        sa.Column("conflict_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(8), nullable=False, server_default="P1"),
        sa.Column("decision_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("telegram_notifications", sa.Column(
        "decision_id", sa.String(64), nullable=False, server_default=""))
    op.add_column("telegram_notifications", sa.Column(
        "decision_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("telegram_notifications", sa.Column(
        "cancellation_reason", sa.Text(), nullable=False, server_default=""))
    op.add_column("telegram_notifications", sa.Column(
        "decision_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade() -> None:
    op.drop_column("telegram_notifications", "decision_snapshot")
    op.drop_column("telegram_notifications", "cancellation_reason")
    op.drop_column("telegram_notifications", "decision_version")
    op.drop_column("telegram_notifications", "decision_id")
    op.drop_table("decision_conflict_audits")
    op.drop_table("current_final_decisions")
