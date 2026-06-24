"""SSH tunnel manager.

Opens a single SSH tunnel from a random local port to the log_monitor's
loopback-bound port on staging. The tunnel comes up at FastAPI startup
and shuts down cleanly at shutdown.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from sshtunnel import SSHTunnelForwarder

from .config import (
    LOCAL_BIND_PORT,
    REMOTE_BIND,
    SSH_HOST,
    SSH_KEY_PATH,
    SSH_PORT,
    SSH_USER,
)

log = logging.getLogger("monitor_ui.tunnel")


class TunnelState:
    """Singleton holding the live tunnel + the local port it's bound to."""

    _instance: Optional["TunnelState"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.forwarder: SSHTunnelForwarder | None = None
        self.local_port: int | None = None
        self.error: str | None = None

    @classmethod
    def get(cls) -> "TunnelState":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def open(self) -> None:
        if self.forwarder is not None and self.forwarder.is_active:
            return
        # If we held a stale forwarder (process restart, network drop,
        # idle SSH timeout), tear it down before starting a fresh one —
        # sshtunnel can otherwise leak the underlying socket.
        if self.forwarder is not None:
            try:
                self.forwarder.stop()
            except Exception:  # noqa: BLE001
                pass
            self.forwarder = None
            self.local_port = None
        try:
            self.forwarder = SSHTunnelForwarder(
                (SSH_HOST, SSH_PORT),
                ssh_username=SSH_USER,
                ssh_pkey=SSH_KEY_PATH,
                remote_bind_address=REMOTE_BIND,
                local_bind_address=("127.0.0.1", LOCAL_BIND_PORT),
                # Quieter logs — sshtunnel's default INFO is noisy.
                logger=logging.getLogger("paramiko").getChild("tunnel"),
            )
            self.forwarder.start()
            self.local_port = self.forwarder.local_bind_port
            self.error = None
            log.info(
                "SSH tunnel open: 127.0.0.1:%d -> %s:%d (via %s@%s)",
                self.local_port, REMOTE_BIND[0], REMOTE_BIND[1],
                SSH_USER, SSH_HOST,
            )
        except Exception as exc:  # noqa: BLE001 — surface to UI cleanly
            self.error = f"{type(exc).__name__}: {exc}"
            self.forwarder = None
            self.local_port = None
            log.warning("SSH tunnel failed: %s", self.error)

    def ensure_open(self) -> bool:
        """Lazy reconnect — make sure the tunnel is live before a caller
        does HTTP through it. Used by the monitor proxy so the Refresh
        button (or any tab poll) can transparently rescue a dropped
        tunnel without the operator having to click the Settings reopen.
        Returns True on success, False if the tunnel couldn't be
        (re-)established (e.g. SSH host unreachable)."""
        if self.forwarder is not None and self.forwarder.is_active:
            return True
        self.open()
        return self.forwarder is not None and self.forwarder.is_active

    def close(self) -> None:
        if self.forwarder is not None:
            try:
                self.forwarder.stop()
                log.info("SSH tunnel closed")
            except Exception as exc:  # noqa: BLE001
                log.warning("tunnel close error: %s", exc)
            finally:
                self.forwarder = None
                self.local_port = None

    @property
    def base_url(self) -> str | None:
        if self.local_port is None:
            return None
        return f"http://127.0.0.1:{self.local_port}"

    @property
    def status(self) -> dict:
        return {
            "open": self.forwarder is not None and self.forwarder.is_active,
            "local_port": self.local_port,
            "remote": f"{REMOTE_BIND[0]}:{REMOTE_BIND[1]}",
            "ssh": f"{SSH_USER}@{SSH_HOST}:{SSH_PORT}",
            "error": self.error,
        }
