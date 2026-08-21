"""add canonical decision snapshots

Revision ID: 20260821_01
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "20260821_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "decision_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("decision_id", sa.String(64), nullable=False),
        sa.Column("analysis_run_id", sa.Integer(), sa.ForeignKey("analysis_runs.id"), nullable=False),
        sa.Column("setup_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("decision_id", name="uq_decision_snapshot_id"),
    )


def downgrade() -> None:
    op.drop_table("decision_snapshots")
