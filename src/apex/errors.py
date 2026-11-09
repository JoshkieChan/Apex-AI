"""Domain errors with messages that are safe to return to API clients."""


class DomainError(Exception):
    """A handled failure with a stable code and a non-sensitive message."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class AdvanceYield(Exception):
    """Stop this worker because another owner holds the task. Do not fail it."""
