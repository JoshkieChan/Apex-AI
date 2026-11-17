"""Run a pure adapter with a timeout. The adapter must not touch the database."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path

from apex.errors import DomainError


def run_bounded(
    adapter: Callable[[Mapping[str, object], float], dict[str, object]],
    payload: Mapping[str, object],
    timeout_seconds: float,
) -> dict[str, object]:
    """Execute an adapter and abandon the result if it exceeds the timeout."""
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="apex-adapter")
    future = pool.submit(_invoke, adapter, payload, timeout_seconds)
    wait = timeout_seconds
    if os.environ.get("APEX_ENVIRONMENT") == "test" and os.environ.get("APEX_TEST_HOLD_ADAPTER"):
        # The process test signals the worker while this thread is blocked.
        # The capability timeout must not classify the row before that signal.
        wait = max(timeout_seconds, 30.0)
    try:
        result = future.result(timeout=wait)
    except FuturesTimeoutError as exc:
        pool.shutdown(wait=False, cancel_futures=True)
        raise DomainError("timed_out", "The operation exceeded its time limit.") from exc
    except DomainError:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    except Exception as exc:
        pool.shutdown(wait=False, cancel_futures=True)
        raise DomainError("execution_failed", "The operation failed.") from exc
    else:
        pool.shutdown(wait=True)
    if not isinstance(result, dict):
        raise DomainError("execution_failed", "The operation returned an unexpected result.")
    return result


def _invoke(
    adapter: Callable[[Mapping[str, object], float], dict[str, object]],
    payload: Mapping[str, object],
    timeout_seconds: float,
) -> dict[str, object]:
    _hold_for_test()
    return adapter(payload, timeout_seconds)


def _hold_for_test() -> None:
    """Block one adapter thread so a process test can signal the worker.

    Production ignores the marker. Removing the file releases the thread.
    The hold does not cancel the thread; process exit abandons it.
    """
    if os.environ.get("APEX_ENVIRONMENT") != "test":
        return
    marker = os.environ.get("APEX_TEST_HOLD_ADAPTER", "")
    if not marker:
        return
    path = Path(marker)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("held", encoding="utf-8")
    deadline = time.monotonic() + 60
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
