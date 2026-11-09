"""Normalized model-provider failures."""


class ProviderError(Exception):
    """A provider call failed. `code` is stable; `message` is safe to log after redaction."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)
