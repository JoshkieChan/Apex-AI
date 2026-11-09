"""Worker liveness. A running process is not the same as a worker that can recover work."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class WorkerStatus:
    """Mutable worker diagnostics. The health server reads a snapshot."""

    instance_id: str
    phase: str = "starting"
    database: str = "unknown"
    last_poll_at: str | None = None
    last_recovery_at: str | None = None
    eligible_goals: int = 0
    claimed_goals: int = 0
    recovery_successes: int = 0
    recovery_failures: int = 0
    shutting_down: bool = False
    health_port: int = 0
    _lock: threading.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def mark_operational(self, *, when: datetime, eligible: int, claimed: int) -> None:
        with self._lock:
            self.phase = "operational"
            self.database = "ok"
            self.last_poll_at = when.isoformat()
            self.eligible_goals = eligible
            self.claimed_goals = claimed
            if claimed:
                self.last_recovery_at = when.isoformat()
                self.recovery_successes += claimed

    def mark_degraded(self, *, when: datetime) -> None:
        with self._lock:
            self.phase = "degraded"
            self.database = "unavailable"
            self.last_poll_at = when.isoformat()
            self.eligible_goals = 0
            self.claimed_goals = 0
            self.recovery_failures += 1

    def mark_shutting_down(self) -> None:
        with self._lock:
            self.phase = "shutting_down"
            self.shutting_down = True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self.phase,
                "instance_id": self.instance_id,
                "database": self.database,
                "last_poll_at": self.last_poll_at,
                "last_recovery_at": self.last_recovery_at,
                "eligible_goals": self.eligible_goals,
                "claimed_goals": self.claimed_goals,
                "recovery_successes": self.recovery_successes,
                "recovery_failures": self.recovery_failures,
                "shutting_down": self.shutting_down,
            }

    def healthy(self) -> bool:
        with self._lock:
            return self.phase == "operational" and not self.shutting_down


def start_health_server(port: int, status: WorkerStatus) -> tuple[ThreadingHTTPServer, int]:
    """Serve /live and /health. /health is 200 only after a successful poll."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/live":
                body = {"status": "alive", "instance_id": status.instance_id}
                _write(self, 200, body)
                return
            if self.path == "/health":
                body = status.snapshot()
                _write(self, 200 if status.healthy() else 503, body)
                return
            _write(self, 404, {"status": "not_found"})

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104
    thread = threading.Thread(target=server.serve_forever, name="apex-worker-health", daemon=True)
    thread.start()
    bound = int(server.server_address[1])
    return server, bound


def _write(handler: BaseHTTPRequestHandler, code: int, body: dict[str, Any]) -> None:
    payload = json.dumps(body).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)
