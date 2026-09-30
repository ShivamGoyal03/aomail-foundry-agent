"""Request-scoped identity boundary.

The model must never be able to pick which user's mailbox it operates on. The
calling application authenticates the end user and injects the principal id as
a (optionally HMAC-signed) HTTP header; this module turns that header into a
:class:`Principal` stored in a :class:`contextvars.ContextVar` for the lifetime
of the request. Tools then resolve accounts *only* through the current
principal.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256

from .config import Settings, get_settings
from .errors import PermissionDeniedError

__all__ = ["Principal", "current_principal", "resolve_principal", "sign_principal", "use_principal"]


@dataclass(frozen=True)
class Principal:
    """The authenticated application user on whose behalf the agent acts."""

    principal_id: str


_current: ContextVar[Principal | None] = ContextVar("omniport_current_principal", default=None)


def current_principal() -> Principal:
    """Return the principal bound to the current request.

    Raises:
        PermissionDeniedError: if no principal was established, which means the
            request was not authenticated by the hosting application.
    """
    principal = _current.get()
    if principal is None:
        raise PermissionDeniedError(
            "No authenticated user is bound to this request, so no mailbox can be accessed."
        )
    return principal


@contextmanager
def use_principal(principal: Principal) -> Iterator[Principal]:
    """Bind ``principal`` for the duration of the ``with`` block."""
    token = _current.set(principal)
    try:
        yield principal
    finally:
        _current.reset(token)


def sign_principal(principal_id: str, key: str) -> str:
    """Return the HMAC-SHA256 hex signature expected for ``principal_id``."""
    return hmac.new(key.encode("utf-8"), principal_id.encode("utf-8"), sha256).hexdigest()


def resolve_principal(headers: dict[str, str], settings: Settings | None = None) -> Principal | None:
    """Derive the principal from request headers.

    ``headers`` must be lower-cased. Returns ``None`` when the request carries
    no usable identity; callers decide whether that is fatal.
    """
    settings = settings or get_settings()
    principal_id = (headers.get(settings.principal_header) or "").strip()

    if not principal_id:
        if settings.local_dev_mode:
            return Principal(principal_id=str(settings.dev_principal_id))
        return None

    signing_key = settings.principal_signing_key
    if signing_key is not None:
        provided = (headers.get(settings.principal_signature_header) or "").strip()
        expected = sign_principal(principal_id, signing_key.get_secret_value())
        if not provided or not hmac.compare_digest(provided, expected):
            raise PermissionDeniedError("The caller identity header failed signature validation.")

    return Principal(principal_id=principal_id)
