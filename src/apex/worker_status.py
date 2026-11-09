"""Worker liveness. A running process is not the same as a fresh successful poll."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class WorkerStatus:
    """Mutable worker diagnostics. The health server reads a snapshot."""

    instance_id: str
    freshness_seconds: float = 30.0
    phase: str = "starting"
    database: str = "unknown"
    last_poll_at: str | None = None
    last_success_at: str | None = None
    last_recovery_at: str | None = None
    eligible_goals: int = 0
    tasks_handled: int = 0
    tasks_failed: int = 0
    tasks_uncertain: int = 0
    claimed_goals: int = 0
    goals_succeeded: int = 0
    goals_failed: int = 0
    goals_uncertain: int = 0
    goals_deferred: int = 0
    recovery_successes: int = 0
    recovery_failures: int = 0
    poll_failures: int = 0
    shutting_down: bool = False
    health_port: int = 0
    _last_success_mono: float | None = field(default=None, repr=False)
    _lock: threading.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def mark_operational(
        self,
        *,
        when: datetime,
        eligible: int,
        tasks_handled: int = 0,
        tasks_succeeded: int = 0,
        tasks_failed: int = 0,
        tasks_uncertain: int = 0,
        goals_claimed: int = 0,
        goals_succeeded: int = 0,
        goals_failed: int = 0,
        goals_uncertain: int = 0,
        goals_deferred: int = 0,
    ) -> None:
        """Record one poll that reached the database. Claimed work is not a success."""
        with self._lock:
            self.phase = "operational"
            self.database = "ok"
            stamp = when.isoformat()
            self.last_poll_at = stamp
            self.last_success_at = stamp
            self._last_success_mono = time.monotonic()
            self.eligible_goals = eligible
            self.tasks_handled += tasks_handled
            self.tasks_failed += tasks_failed
            self.tasks_uncertain += tasks_uncertain
            self.claimed_goals = goals_claimed
            self.goals_succeeded += goals_succeeded
            self.goals_failed += goals_failed
            self.goals_uncertain += goals_uncertain
            self.goals_deferred += goals_deferred
            self.recovery_successes += goals_succeeded + tasks_succeeded
            self.recovery_failures += goals_failed + tasks_failed
            if goals_claimed or tasks_handled:
                self.last_recovery_at = stamp

    def mark_degraded(self, *, when: datetime) -> None:
        with self._lock:
            self.phase = "degraded"
            self.database = "unavailable"
            self.last_poll_at = when.isoformat()
            self.eligible_goals = 0
            self.claimed_goals = 0
            self.poll_failures += 1

    def mark_shutting_down(self) -> None:
        with self._lock:
            self.phase = "shutting_down"
            self.shutting_down = True

    def _stale_locked(self) -> bool:
        if self._last_success_mono is None:
            return True
        return (time.monotonic() - self._last_success_mono) > self.freshness_seconds

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            fresh = self.phase == "operational" and not self._stale_locked()
            status = self.phase
            if self.phase == "operational" and not fresh:
                status = "degraded"
            return {
                "status": status,
                "instance_id": self.instance_id,
                "database": self.database,
                "last_poll_at": self.last_poll_at,
                "last_success_at": self.last_success_at,
                "last_recovery_at": self.last_recovery_at,
                "eligible_goals": self.eligible_goals,
                "tasks_handled": self.tasks_handled,
                "tasks_failed": self.tasks_failed,
                "tasks_uncertain": self.tasks_uncertain,
                "claimed_goals": self.claimed_goals,
                "goals_succeeded": self.goals_succeeded,
                "goals_failed": self.goals_failed,
                "goals_uncertain": self.goals_uncertain,
                "goals_deferred": self.goals_deferred,
                "recovery_successes": self.recovery_successes,
                "recovery_failures": self.recovery_failures,
                "poll_failures": self.poll_failures,
                "poll_fresh": fresh,
                "shutting_down": self.shutting_down,
            }

    def healthy(self) -> bool:
        with self._lock:
            return (
                self.phase == "operational" and not self.shutting_down and not self._stale_locked()
            )


def start_health_server(port: int, status: WorkerStatus) -> tuple[ThreadingHTTPServer, int]:
    """Serve /live and /health. /health is 200 only after a fresh successful poll."""

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
