"""Add an optional first-party or OpenRouter model preference for new projects."""
from alembic import op
import sqlalchemy as sa


revision = "c43e8057bd12"
down_revision = "b7c1a2d3e4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user", sa.Column("default_launch_model", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_column("default_launch_model")
