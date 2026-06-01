"""Fixtures for CMS service tests (all in-memory)."""

from __future__ import annotations

import pytest

from app.services.cms import CmsService, build_cms_service


@pytest.fixture
def cms() -> CmsService:
    return build_cms_service()
