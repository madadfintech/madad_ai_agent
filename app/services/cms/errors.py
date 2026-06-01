"""CMS service exception hierarchy."""

from __future__ import annotations

from app.core.exceptions import AppError


class CmsError(AppError):
    code = "cms_error"


class ConfigNotFoundError(CmsError):
    code = "config_not_found"
    http_status = 404


class ConfigVersionNotFoundError(CmsError):
    code = "config_version_not_found"
    http_status = 404


class ConfigValidationError(CmsError):
    """A config payload failed its kind's validation rules."""

    code = "config_validation_error"
    http_status = 422
