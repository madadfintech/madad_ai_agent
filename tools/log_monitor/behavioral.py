"""Behavioral rule engine for the Madad log monitor.

Stateful layer that detects DEVIATIONS FROM EXPECTED BEHAVIOR — not just
crashes. Catches the class of issues line-by-line regex can't:

* Same template fired N+ times to one identity in a window (UX spam).
* Same invoice tool called N+ times for one identity in a window
  (re-submission loops).
* A specific operation taking longer than its expected p95
  (creeping latency regressions).
* A template firing with too many empty/em-dash variables
  (template wiring bugs that render but mislead).

Each rule type runs side-by-side with the line-by-line regex engine;
when a rule's threshold is breached, the engine emits a synthetic
``issue`` entry (same shape as a regex match) that lands in the issues
log + ring buffer + notification webhook just like any other capture.

The engine is deliberately lossless — every observation is kept until
it ages out of the rule's window. Memory budget is bounded by
``max_events_per_bucket`` (default 1000) and the natural cardinality of
the group keys.
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

log = logging.getLogger("madad.log_monitor.behavioral")


# ---------------------------------------------------------------------------
# Rule implementations
# ---------------------------------------------------------------------------


@dataclass
class _Bucket:
    """Sliding window of timestamps for one group key."""
    timestamps: deque = field(default_factory=deque)
    last_alert_at: float | None = None


class CountRule:
    """Count occurrences of a pattern per group key.

    Alerts when count >= ``threshold`` within ``window_seconds``. A
    ``cooldown_seconds`` is applied per key after each alert so the
    monitor isn't flooded by a single rolling breach.

    Group keys come from the regex's NAMED capture groups (``?P<name>``)
    or positional groups in the matched ``trigger`` pattern. All groups
    are concatenated into the bucket key.
    """

    def __init__(
        self,
        *,
        name: str,
        severity: str,
        description: str,
        trigger: str,
        threshold: int,
        window_seconds: float,
        cooldown_seconds: float = 300.0,
        max_events_per_bucket: int = 1000,
    ) -> None:
        self.name = name
        self.severity = severity
        self.description = description
        self._trigger = re.compile(trigger)
        self.threshold = threshold
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self.max_events_per_bucket = max_events_per_bucket
        self._buckets: dict[tuple[str, ...], _Bucket] = {}

    def observe(self, line: str, now: float) -> dict[str, Any] | None:
        m = self._trigger.search(line)
        if not m:
            return None
        # Named groups win; fall back to positional. Convert all to str.
        if m.groupdict():
            key = tuple(
                (m.groupdict().get(k) or "_")
                for k in sorted(m.groupdict().keys())
            )
        else:
            key = tuple(m.groups() or ("_global_",))

        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket()
            self._buckets[key] = bucket

        # Drop events older than the window.
        cutoff = now - self.window_seconds
        while bucket.timestamps and bucket.timestamps[0] < cutoff:
            bucket.timestamps.popleft()

        bucket.timestamps.append(now)
        if len(bucket.timestamps) > self.max_events_per_bucket:
            bucket.timestamps.popleft()

        if len(bucket.timestamps) < self.threshold:
            return None

        # Cooldown: don't re-alert for the same bucket within the
        # cooldown window.
        if (
            bucket.last_alert_at is not None
            and (now - bucket.last_alert_at) < self.cooldown_seconds
        ):
            return None
        bucket.last_alert_at = now

        return {
            "rule": self.name,
            "severity": self.severity,
            "description": self.description,
            "group_key": _fmt_key(key, m),
            "count_in_window": len(bucket.timestamps),
            "window_seconds": self.window_seconds,
            "matched_line": line[:300],
        }


class ValueRule:
    """Single-line numeric threshold rule.

    Useful for "operation took >Ns" / "buffer above N items" style
    canaries. ``trigger`` selects the line; ``value_pattern`` extracts
    a single numeric capture group; alert fires whenever value >=
    ``threshold``. No grouping / no cooldown — every breaching line
    fires.
    """

    def __init__(
        self,
        *,
        name: str,
        severity: str,
        description: str,
        trigger: str,
        value_pattern: str,
        threshold: float,
    ) -> None:
        self.name = name
        self.severity = severity
        self.description = description
        self._trigger = re.compile(trigger)
        self._value_pattern = re.compile(value_pattern)
        self.threshold = threshold

    def observe(self, line: str, now: float) -> dict[str, Any] | None:
        if not self._trigger.search(line):
            return None
        m = self._value_pattern.search(line)
        if not m:
            return None
        try:
            value = float(m.group(1))
        except (ValueError, IndexError):
            return None
        if value < self.threshold:
            return None
        return {
            "rule": self.name,
            "severity": self.severity,
            "description": self.description,
            "value": value,
            "threshold": self.threshold,
            "matched_line": line[:300],
        }


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_behavioral_rules(spec: list[dict[str, Any]] | None) -> list:
    """Build rule instances from the ``behavioral_rules:`` YAML list."""
    rules: list = []
    for entry in spec or []:
        rtype = entry.get("type")
        try:
            if rtype == "count":
                rules.append(CountRule(
                    name=entry["name"],
                    severity=entry.get("severity", "warning"),
                    description=entry.get("description", ""),
                    trigger=entry["trigger"],
                    threshold=int(entry["threshold"]),
                    window_seconds=float(entry["window_seconds"]),
                    cooldown_seconds=float(entry.get("cooldown_seconds", 300)),
                ))
            elif rtype == "value":
                rules.append(ValueRule(
                    name=entry["name"],
                    severity=entry.get("severity", "warning"),
                    description=entry.get("description", ""),
                    trigger=entry["trigger"],
                    value_pattern=entry["value_pattern"],
                    threshold=float(entry["threshold"]),
                ))
            else:
                log.warning("skipping behavioral rule with unknown type: %s", entry)
        except (KeyError, ValueError, re.error) as exc:
            log.warning("invalid behavioral rule %s: %s", entry, exc)
    log.info("loaded %d behavioral rules", len(rules))
    return rules


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_key(key: tuple[str, ...], match: re.Match) -> str:
    """Render a group key for the issues log — readable, bounded."""
    if match.groupdict():
        kvs = match.groupdict()
        return ", ".join(f"{k}={v}" for k, v in sorted(kvs.items()) if v)
    return ", ".join(str(p) for p in key if p)


def evaluate_all(
    rules: list,
    line: str,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Run every rule against ``line``; return all triggered alerts."""
    if now is None:
        now = time.time()
    alerts: list[dict[str, Any]] = []
    for r in rules:
        try:
            hit = r.observe(line, now)
        except Exception as exc:  # noqa: BLE001 — never lose a tail on bug
            log.warning("rule %s raised: %s", getattr(r, "name", "?"), exc)
            continue
        if hit is not None:
            alerts.append(hit)
    return alerts
