"""final decision replay

Revision ID: 20260821_03
Revises: 20260821_02
"""
import sqlalchemy as sa

from alembic import op

revision = "20260821_03"
down_revision = "20260821_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "decision_replays",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("decision_id", sa.String(64), nullable=False, unique=True),
        sa.Column("symbol", sa.String(32), nullable=False, server_default="XAUUSD"),
        sa.Column("decision_version", sa.Integer(), nullable=False),
        sa.Column("final_action", sa.String(32), nullable=False),
        sa.Column("scenario_type", sa.String(40), nullable=False, server_default="OTHER"),
        sa.Column("raw_score", sa.Integer(), nullable=True),
        sa.Column("calibrated_probability", sa.Float(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("outcome", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("decision_replays")
