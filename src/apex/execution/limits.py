"""Run a pure adapter with a timeout. The adapter must not touch the database."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError

from apex.errors import DomainError


def run_bounded(
    adapter: Callable[[Mapping[str, object], float], dict[str, object]],
    payload: Mapping[str, object],
    timeout_seconds: float,
) -> dict[str, object]:
    """Execute an adapter and abandon the result if it exceeds the timeout."""
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="apex-adapter")
    future = pool.submit(adapter, payload, timeout_seconds)
    try:
        result = future.result(timeout=timeout_seconds)
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
