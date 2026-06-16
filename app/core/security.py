"""Admin authentication for the operator-facing services (CMS, Ops/Visibility).

Three auth seams, all dev-open until a secret is configured (so tests/local dev
need no credentials) and enforced once set in staging/prod:

* ``require_admin`` — bearer token for the operator panels (CMS, Visibility).
* ``require_api_auth`` — service JWT for the public API (api.ai.madadfintech.com).
* ``verify_webhook_signature`` — HMAC signature on Madad-platform callbacks.

Each is a deliberate seam — it can be swapped (OIDC, mTLS, a different signature
scheme) without touching the route handlers. The ops probes (``/health``,
``/ready``, ``/metrics``) are always open.
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import Request

from app.core.config import SecuritySettings, settings
from app.core.exceptions import AppError

# Probe endpoints that must stay reachable without auth.
_PUBLIC_PATHS = ("/health", "/ready", "/metrics")
# Webhook/callback paths use HMAC signatures, not the API JWT. Keep
# ``/madad/status/`` for backward compat with any legacy Phase 2 callers;
# the Phase 4 chokepoint lives under ``/madad/events/`` and is the path
# Madad's backend actually posts to.
_WEBHOOK_MARKERS = ("/webhooks/", "/madad/status/", "/madad/events/")


class UnauthorizedError(AppError):
    code = "unauthorized"
    http_status = 401


def _is_public_path(path: str) -> bool:
    return path.rstrip("/").endswith(_PUBLIC_PATHS)


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    return header[7:] if header.startswith("Bearer ") else None


async def require_admin(request: Request) -> None:
    """FastAPI dependency that enforces the admin bearer token (except probes).

    UAT 2026-06-16: also accepts any bearer in ``settings.admin_api_token_extras``
    so Madad's reset tooling (which uses its own ``dbwipe``-style helper
    token) can call ``/workflow/admin/forget-session`` without us having
    to share the canonical 48-char prod token. The primary token still
    works; extras are additive."""

    primary = settings.admin_api_token
    extras = settings.admin_api_token_extras
    if not primary and not extras:
        return  # dev mode: no token configured at all
    if _is_public_path(request.url.path):
        return
    presented = _bearer_token(request)
    if presented is None:
        raise UnauthorizedError("Admin authentication required")
    if primary and presented == primary:
        return
    if presented in extras:
        return
    raise UnauthorizedError("Admin authentication required")


async def require_api_auth(request: Request) -> None:
    """Enforce a service JWT on the public API (except probes + webhooks).

    Webhook/callback paths authenticate by HMAC signature
    (:func:`verify_webhook_signature`), so they are skipped here.
    """

    sec = settings.security
    if not sec.jwt_secret:
        return  # dev mode: no JWT secret configured
    path = request.url.path
    if _is_public_path(path) or any(marker in path for marker in _WEBHOOK_MARKERS):
        return
    token = _bearer_token(request)
    if token is None:
        raise UnauthorizedError("API authentication required")
    _decode_jwt(token, sec)


def _decode_jwt(token: str, sec: SecuritySettings) -> dict[str, object]:
    import jwt

    # Caller (``require_api_auth``) guarantees ``sec.jwt_secret`` is set before
    # we get here. Narrow it for the typed jwt.decode signature.
    assert sec.jwt_secret is not None  # noqa: S101 — invariant from caller
    # Verify the audience only when one is configured; issuer is enforced only
    # when ``jwt_issuer`` is set (PyJWT skips it when None).
    try:
        decoded: dict[str, object] = jwt.decode(
            token,
            sec.jwt_secret,
            algorithms=[sec.jwt_algorithm],
            audience=sec.jwt_audience,
            issuer=sec.jwt_issuer,
            options={"verify_aud": sec.jwt_audience is not None},
        )
        return decoded
    except jwt.PyJWTError as exc:
        raise UnauthorizedError("Invalid or expired token") from exc


async def verify_webhook_signature(request: Request) -> None:
    """Verify the HMAC-SHA256 signature on a Madad-platform callback body.

    Dev-open until ``security.webhook_secret`` is set. The signature header
    (default ``X-Madad-Signature``) carries a hex digest of the raw request body.
    """

    sec = settings.security
    if not sec.webhook_secret:
        return  # dev mode: no webhook secret configured
    provided = request.headers.get(sec.webhook_signature_header, "")
    body = await request.body()
    expected = hmac.new(sec.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    if not provided or not hmac.compare_digest(provided, expected):
        raise UnauthorizedError("Invalid webhook signature")
