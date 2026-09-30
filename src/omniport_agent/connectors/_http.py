"""Thin HTTP layer shared by the Gmail and Microsoft Graph connectors.

Responsibilities:

* one ``httpx.AsyncClient`` per connector, with an explicit timeout;
* OAuth2 access-token acquisition and in-memory caching, so refresh tokens are
  used at most once per expiry window;
* mapping HTTP failures onto :mod:`omniport_agent.errors` with scrubbed messages.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

import httpx

from ..errors import (
    AuthenticationError,
    NotFoundError,
    PermissionDeniedError,
    ProviderError,
    ProviderTimeoutError,
    scrub,
)

__all__ = ["OAuth2TokenCache", "raise_for_status", "request_json", "new_client"]


def new_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout))


class OAuth2TokenCache:
    """Caches a bearer token until shortly before it expires.

    The token itself is only ever returned to the caller that builds the
    ``Authorization`` header; it is never logged or placed in an error message.
    """

    _SKEW_SECONDS = 60.0

    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at: float = 0.0

    def get(self) -> str | None:
        if self._token and time.monotonic() < self._expires_at - self._SKEW_SECONDS:
            return self._token
        return None

    def set(self, token: str, expires_in: float) -> None:
        self._token = token
        self._expires_at = time.monotonic() + max(expires_in, 0.0)

    def clear(self) -> None:
        self._token = None
        self._expires_at = 0.0


def raise_for_status(response: httpx.Response, provider: str) -> None:
    """Translate an error response into a model-safe exception."""
    if response.is_success:
        return
    status = response.status_code
    # ``response.text`` may echo request material, so it is always scrubbed and
    # length-bounded before it can reach the model.
    detail = scrub(response.text, max_length=200)
    if status in (401, 403):
        if status == 401:
            raise AuthenticationError(
                f"{provider} rejected the stored credentials for this mailbox (HTTP 401). "
                "Re-authorize the account."
            )
        raise PermissionDeniedError(
            f"{provider} denied this operation (HTTP 403). The account may be missing a required scope."
        )
    if status == 404:
        raise NotFoundError(f"{provider} has no such message or folder (HTTP 404).")
    if status == 429:
        raise ProviderError(f"{provider} is rate limiting this mailbox (HTTP 429). Try again later.")
    raise ProviderError(f"{provider} request failed with HTTP {status}: {detail}")


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    provider: str,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    json: Any | None = None,
    data: Any | None = None,
    content: bytes | None = None,
) -> Any:
    """Perform a request and return the decoded JSON body (``None`` if empty)."""
    try:
        response = await client.request(
            method,
            url,
            headers=dict(headers or {}),
            params=dict(params or {}),
            json=json,
            data=data,
            content=content,
        )
    except httpx.TimeoutException:
        raise ProviderTimeoutError(f"{provider} did not respond within the configured timeout.") from None
    except httpx.HTTPError as exc:
        raise ProviderError(f"Could not reach {provider}: {type(exc).__name__}.") from None

    raise_for_status(response, provider)
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        raise ProviderError(f"{provider} returned a response that is not valid JSON.") from None
