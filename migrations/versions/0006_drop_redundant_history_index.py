"""drop idx_location_history_device

It indexed (device_id, seen_at), which the primary key's autoindex already
covers, so every history insert updated two identical b-trees.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-20

"""

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_location_history_device")


def downgrade() -> None:
    op.execute("CREATE INDEX idx_location_history_device ON location_history (device_id, seen_at DESC)")
