"""Autopilot: applications, payments, runtime settings, scraper and payment columns.

0001 builds the schema from the current models, so on a fresh database these objects
already exist. This revision only adds what an older database is missing.
"""
import sqlalchemy as sa
from alembic import op

import app.models  # noqa: F401
from app.db import Base

revision = "0002_autopilot"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

NEW_TABLES = ("applications", "payments", "app_settings")
NEW_COLUMNS = {
    "leads": [
        sa.Column("content_hash", sa.String(64)),
        sa.Column("analysis_attempts", sa.Integer, nullable=False, server_default="0"),
    ],
    "lead_analyses": [sa.Column("match_reason", sa.Text, nullable=False, server_default="")],
    "projects": [
        sa.Column("paid_amount", sa.Float, nullable=False, server_default="0"),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("report_status", sa.String(20)),
        sa.Column("report_path", sa.String(1000)),
    ],
}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table, columns in NEW_COLUMNS.items():
        existing = {column["name"] for column in inspector.get_columns(table)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column)
    if "ix_leads_content_hash" not in {index["name"] for index in inspector.get_indexes("leads")}:
        op.create_index("ix_leads_content_hash", "leads", ["content_hash"])
    Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables[name] for name in NEW_TABLES])


def downgrade():
    for name in reversed(NEW_TABLES):
        op.drop_table(name)
    op.drop_index("ix_leads_content_hash", table_name="leads")
    for table, columns in NEW_COLUMNS.items():
        for column in columns:
            op.drop_column(table, column.name)
