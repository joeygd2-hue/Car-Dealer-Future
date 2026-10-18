class IntegrationError(Exception):
    """A provider call failed. ``retryable`` tells the job runner whether to try again."""

    retryable = True


class CredentialError(IntegrationError):
    """Missing, invalid, revoked or expired credentials. Not retried until fixed."""

    retryable = False


class RateLimitedError(IntegrationError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class NotConfiguredError(CredentialError):
    """Required settings for a provider are absent."""


class PermanentError(IntegrationError):
    """Request was rejected (4xx other than auth/rate limit). Not retried."""

    retryable = False
