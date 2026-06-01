"""Root application (aggregate health endpoint / default container target)."""

from __future__ import annotations

from app.core.app import create_service_app
from app.core.config import settings

app = create_service_app(title=settings.app_name, service=settings.app_name)
