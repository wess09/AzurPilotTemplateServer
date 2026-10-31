"""Persist canonical item definitions and pending metadata edits."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from alembic import op

revision = "9de4a6c1802f"
down_revision = "7c1d83b2a409"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("mapping_drafts", sa.Column("item_metadata", JSONB(), nullable=True))
    op.create_table(
        "item_definitions",
        sa.Column("item_id", sa.String(128), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("item_metadata", JSONB(), nullable=False),
    )


def downgrade():
    op.drop_table("item_definitions")
    op.drop_column("mapping_drafts", "item_metadata")
