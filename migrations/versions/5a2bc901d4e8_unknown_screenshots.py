"""Store full reward screenshots and their template locations independently of releases."""

import sqlalchemy as sa
from alembic import op

revision = "5a2bc901d4e8"
down_revision = "03db42fed20b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "screenshots",
        sa.Column("sha256", sa.String(64), primary_key=True),
        sa.Column("png", sa.LargeBinary(), nullable=False),
        sa.Column("scene", sa.String(64), nullable=False),
        sa.Column("client", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "screenshot_references",
        sa.Column("screenshot_sha256", sa.String(64), sa.ForeignKey("screenshots.sha256"), primary_key=True),
        sa.Column("identity_id", sa.String(128), sa.ForeignKey("identities.id"), primary_key=True),
        *(sa.Column(name, sa.Integer(), primary_key=True) for name in ("x1", "y1", "x2", "y2")),
    )
    op.create_index("ix_screenshot_references_identity_id", "screenshot_references", ["identity_id"])


def downgrade():
    metadata = sa.MetaData()
    for name in ("screenshot_references", "screenshots"):
        sa.Table(name, metadata, autoload_with=op.get_bind()).drop(op.get_bind())
