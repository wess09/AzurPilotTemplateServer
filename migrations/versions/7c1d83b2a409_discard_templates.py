"""Retire invalid templates while preserving source IDs and evidence."""

import sqlalchemy as sa
from alembic import op

revision = "7c1d83b2a409"
down_revision = "5a2bc901d4e8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("identities", sa.Column("discarded", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.create_index("ix_identities_discarded", "identities", ["discarded"])


def downgrade():
    op.drop_index("ix_identities_discarded", table_name="identities")
    op.drop_column("identities", "discarded")
