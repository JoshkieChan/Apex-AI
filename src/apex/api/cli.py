"""Run the local API server."""

from __future__ import annotations

import uvicorn

from apex.api.app import create_app
from apex.config import assert_runtime_is_allowed, load_settings


def main() -> None:
    """Start uvicorn. Development authentication is refused when the environment is production."""
    settings = load_settings()
    assert_runtime_is_allowed(settings)
    uvicorn.run(create_app, factory=True, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
