"""add project.orchestrator_instance_type

Explicit cloud instance/machine type for a skypilot orchestrator VM (e.g.
"n4-standard-2" / "m6i.large"). Empty = the cloud's default shaping. Mirrors the
SQLModel field and the runtime sqlite _migrate add-column so Postgres deployments
get the column too.

Revision ID: b7c1a2d3e4f5
Revises: f1369e2c2a66
Create Date: 2026-07-09 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # SQLModel column types (e.g. AutoString) render into revisions


# revision identifiers, used by Alembic.
revision: str = 'b7c1a2d3e4f5'
down_revision: Union[str, None] = 'f1369e2c2a66'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NOT NULL with a server_default of '' so existing rows backfill cleanly
    # (matches the other *_compute_backend columns' nullable=False shape).
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'orchestrator_instance_type',
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
            server_default='',
        ))


def downgrade() -> None:
    with op.batch_alter_table('project', schema=None) as batch_op:
        batch_op.drop_column('orchestrator_instance_type')
