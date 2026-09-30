"""Error types for the agent service.

Every error raised from this package is expected to carry a *model-safe*
message: it must never embed access tokens, refresh tokens, passwords, client
secrets or raw ``Authorization`` headers, because tool errors are surfaced back
to the language model as tool results.
"""

from __future__ import annotations

import re

__all__ = [
    "OmniPortAgentError",
    "ConfigurationError",
    "AuthenticationError",
    "AccountNotFoundError",
    "PermissionDeniedError",
    "NotFoundError",
    "ProviderError",
    "ProviderTimeoutError",
    "ValidationError",
    "ApprovalRequiredError",
    "scrub",
]

# Patterns for values that must never reach the model or the logs.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(ya29|1//|gho_|ghp_|eyJ)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\"?(access_token|refresh_token|client_secret|id_token|password|"
        r"app_password|api[-_]?key|authorization)\"?\s*[:=]\s*\"?[^\s\"',}]+"
    ),
)

_REDACTED = "[redacted]"


def scrub(text: object, *, max_length: int = 500) -> str:
    """Remove credential-shaped substrings from ``text`` and bound its length."""
    value = str(text)
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(_REDACTED, value)
    if len(value) > max_length:
        value = value[: max_length - 3] + "..."
    return value


class OmniPortAgentError(Exception):
    """Base class for all errors that are safe to show to the model."""

    #: Short machine-readable code included in tool error payloads.
    code = "agent_error"

    def __init__(self, message: str) -> None:
        super().__init__(scrub(message))

    @property
    def message(self) -> str:
        return str(self)


class ConfigurationError(OmniPortAgentError):
    """The service is misconfigured (missing/invalid environment values)."""

    code = "configuration_error"


class AuthenticationError(OmniPortAgentError):
    """Provider credentials are missing, expired or were rejected."""

    code = "authentication_error"


class AccountNotFoundError(OmniPortAgentError):
    """The requested account id is unknown."""

    code = "account_not_found"


class PermissionDeniedError(OmniPortAgentError):
    """The caller is not allowed to use the requested account or operation."""

    code = "permission_denied"


class NotFoundError(OmniPortAgentError):
    """A message, thread or folder identifier does not exist."""

    code = "not_found"


class ProviderError(OmniPortAgentError):
    """The upstream email provider returned an error."""

    code = "provider_error"


class ProviderTimeoutError(ProviderError):
    """The upstream email provider did not answer in time."""

    code = "provider_timeout"


class ValidationError(OmniPortAgentError):
    """Tool arguments were invalid."""

    code = "validation_error"


class ApprovalRequiredError(OmniPortAgentError):
    """A write operation was attempted without a human-approved draft."""

    code = "approval_required"
