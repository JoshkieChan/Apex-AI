"""Domain errors with messages that are safe to return to API clients."""


class DomainError(Exception):
    """A handled failure with a stable code and a non-sensitive message."""

    def __init__(self, code: str, message: str, *, dispatched: bool | None = None) -> None:
        self.code = code
        self.message = message
        # None means this failure is not a transport classification. False means
        # the remote tool request was not sent. True means it may have been.
        self.dispatched = dispatched
        super().__init__(message)


class AdvanceYield(Exception):
    """Stop this worker because another owner holds the task. Do not fail it."""
