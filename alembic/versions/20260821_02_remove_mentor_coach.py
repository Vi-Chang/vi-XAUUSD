"""permanently remove mentor and trading-coach data

Revision ID: 20260821_02
Revises: 20260821_01
"""
from alembic import op

revision = "20260821_02"
down_revision = "20260821_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE positions SET account_id = NULL WHERE account_id IN "
               "(SELECT id FROM accounts WHERE strategy_source = 'TEACHER')")
    op.execute("DELETE FROM accounts WHERE strategy_source = 'TEACHER'")
    op.execute("DROP TABLE IF EXISTS mentor_signals")
    op.execute("DROP TABLE IF EXISTS behavior_flags")


def downgrade() -> None:
    # Intentional one-way deletion: removed private history is not reconstructed.
    pass
