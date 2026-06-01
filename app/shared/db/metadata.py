"""Aggregates every ORM table module so ``Base.metadata`` is complete.

Imported by Alembic (for autogenerate) and by the test DB fixture (for
``create_all``). Importing a module registers its tables on the shared ``Base``.
"""

from __future__ import annotations

# Register all domain tables (import side effects populate Base.metadata).
from app.services.cms import db as _cms_db  # noqa: E402,F401
from app.services.communication import db as _comm_db  # noqa: E402,F401
from app.services.document import db as _doc_db  # noqa: E402,F401
from app.services.nudge import db as _nudge_db  # noqa: E402,F401
from app.services.visibility import db as _vis_db  # noqa: E402,F401
from app.shared.workflow.adapters import postgres_runstore as _wf_db  # noqa: E402,F401

from .base import Base

__all__ = ["Base"]
