"""initial baseline — create per-domain schemas + all tables

Revision ID: 0001
Revises:
Create Date: 2026-05-22

The baseline creates the logical schemas and every table from the shared
metadata. Subsequent migrations should use ``alembic revision --autogenerate``.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import op
from app.shared.db.engine import SCHEMAS
from app.shared.db.metadata import Base

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    for schema in SCHEMAS:
        op.execute(CreateSchema(schema, if_not_exists=True))
    Base.metadata.create_all(bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind)
    for schema in SCHEMAS:
        op.execute(DropSchema(schema, cascade=True, if_exists=True))
