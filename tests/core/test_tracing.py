"""OpenTelemetry tracing seam — opt-in via settings, no-op otherwise.

The actual OTel SDK isn't installed in dev/tests by default; the module
must keep ruff/mypy/pytest green without it.
"""

from __future__ import annotations

from app.core.config import ObservabilitySettings, Settings
from app.core.tracing import init_tracing, instrument_app


def _settings(otel_endpoint: str | None = None) -> Settings:
    """Build a Settings instance with the tracing seam configured."""
    s = Settings()
    s.observability = ObservabilitySettings(
        metrics_enabled=True,
        otel_endpoint=otel_endpoint,
    )
    return s


def test_init_tracing_no_op_without_endpoint() -> None:
    """Default config (no otel_endpoint) → tracing stays off, no
    dependency on opentelemetry being importable."""
    enabled = init_tracing(_settings(otel_endpoint=None), service="workflow-test")
    assert enabled is False


def test_init_tracing_no_op_with_empty_endpoint() -> None:
    enabled = init_tracing(_settings(otel_endpoint=""), service="workflow-test-2")
    assert enabled is False


def test_init_tracing_degrades_when_sdk_missing(monkeypatch) -> None:
    """If the user sets an endpoint but the SDK isn't installed, the
    module logs a warning and returns False — never crashes."""
    import importlib

    # Force the OTel imports to fail.
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError(f"forced missing: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    enabled = init_tracing(
        _settings(otel_endpoint="http://otel:4318/v1/traces"),
        service="workflow-test-3",
    )
    assert enabled is False
    # Re-import is harmless.
    importlib.invalidate_caches()


def test_instrument_app_no_op_when_tracing_disabled() -> None:
    """Calling instrument_app for a service whose tracing is off must
    not blow up — the app factory does this unconditionally."""
    from fastapi import FastAPI

    app = FastAPI()
    # Service not in _initialized → silent no-op.
    instrument_app(app, "workflow-not-traced")
