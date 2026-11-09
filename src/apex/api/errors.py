"""API errors. Messages are safe to return to clients."""


class ApiError(Exception):
    """A client-visible failure with an HTTP status and a stable code."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)
