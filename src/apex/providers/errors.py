"""Normalized model-provider failures."""


class ProviderError(Exception):
    """A provider call failed. `code` is stable; `message` is safe to log after redaction."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        sent: bool = False,
        confirmed: bool = False,
    ) -> None:
        self.code = code
        self.message = message
        self.sent = sent
        self.confirmed = confirmed
        super().__init__(message)
