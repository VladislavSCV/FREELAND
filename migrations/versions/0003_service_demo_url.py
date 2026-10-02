"""Service demo links for proposals.

0001 builds the schema from the current models, so the column may already exist.
"""
import sqlalchemy as sa
from alembic import op

revision = "0003_service_demo_url"
down_revision = "0002_autopilot"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("services")}
    if "demo_url" not in columns:
        op.add_column("services", sa.Column("demo_url", sa.String(1000)))


def downgrade():
    op.drop_column("services", "demo_url")
