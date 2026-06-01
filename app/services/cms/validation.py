"""Per-kind configuration validation.

Every write is validated before it is persisted, so MADAD operators cannot push a
structurally broken template/checklist/schedule that would then break a live
conversation. Validators are registered per :class:`ConfigKind` and can be
extended without touching the service.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .enums import ConfigKind
from .errors import ConfigValidationError

Validator = Callable[[dict[str, Any]], None]

_VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _require(condition: bool, message: str, **details: Any) -> None:
    if not condition:
        raise ConfigValidationError(message, details=details)


def validate_template(value: dict[str, Any]) -> None:
    body = value.get("body")
    _require(
        isinstance(body, str) and body.strip() != "",
        "Template 'body' must be a non-empty string",
    )
    assert isinstance(body, str)
    declared = value.get("variables")
    if declared is not None:
        _require(
            isinstance(declared, list) and all(isinstance(v, str) for v in declared),
            "Template 'variables' must be a list of strings",
        )
        used = set(_VAR_PATTERN.findall(body))
        unknown = used - set(declared)
        _require(
            not unknown,
            f"Template uses undeclared variables: {', '.join(sorted(unknown))}",
            unknown=sorted(unknown),
        )


def validate_checklist(value: dict[str, Any]) -> None:
    items = value.get("items")
    _require(isinstance(items, list), "Checklist 'items' must be a list")
    assert isinstance(items, list)
    seen: set[str] = set()
    for item in items:
        _require(isinstance(item, dict), "Each checklist item must be an object")
        code = item.get("code")
        _require(isinstance(code, str) and code != "", "Each checklist item needs a 'code'")
        _require(code not in seen, f"Duplicate checklist item code: {code}")
        seen.add(code)


def validate_nudge(value: dict[str, Any]) -> None:
    schedule = value.get("schedule")
    _require(
        isinstance(schedule, list) and len(schedule) > 0,
        "Nudge 'schedule' must be a non-empty list",
    )
    assert isinstance(schedule, list)
    for step in schedule:
        _require(isinstance(step, dict), "Each nudge schedule step must be an object")
        _require("offset" in step, "Each nudge schedule step needs an 'offset'")


def _validate_any(value: dict[str, Any]) -> None:
    _require(isinstance(value, dict), "Config value must be an object")


_VALIDATORS: dict[ConfigKind, Validator] = {
    ConfigKind.TEMPLATE: validate_template,
    ConfigKind.CHECKLIST: validate_checklist,
    ConfigKind.NUDGE: validate_nudge,
    ConfigKind.WORKFLOW: _validate_any,
    ConfigKind.SETTING: _validate_any,
}


def validate(kind: ConfigKind, value: dict[str, Any]) -> None:
    """Validate ``value`` for ``kind``; raises :class:`ConfigValidationError`."""

    _validate_any(value)
    _VALIDATORS.get(kind, _validate_any)(value)


def register_validator(kind: ConfigKind, validator: Validator) -> None:
    """Override/extend the validator for a kind (e.g. workflow-specific rules)."""

    _VALIDATORS[kind] = validator
