"""Hosted-agent HTTP entrypoint.

``ResponsesHostServer`` from ``agent-framework-foundry-hosting`` is a Starlette
application exposing the Foundry Responses protocol on ``POST /responses`` and
a readiness probe on ``GET /readiness``. This module wraps it with:

* :class:`PrincipalMiddleware`, which binds the authenticated caller to the
  request context so tools cannot choose a user id themselves;
* an extra ``GET /health`` route for platforms that expect that path.

Tenant isolation: the only Foundry-specific surface is
:func:`omniport_agent.agent.build_chat_client`. If a tenant's deployment needs
the invocations protocol instead of the responses protocol, swap
``ResponsesHostServer`` for ``InvocationsHostServer`` here; nothing else in the
package changes.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import Settings, get_settings
from .errors import PermissionDeniedError
from .identity import resolve_principal, use_principal

__all__ = ["PrincipalMiddleware", "create_server", "main"]

logger = logging.getLogger("omniport_agent")


class PrincipalMiddleware(BaseHTTPMiddleware):
    """Bind the authenticated principal from request headers to the context.

    The identity must come from the calling application (an HTTP header that
    the model cannot influence), optionally HMAC-signed. Requests without a
    usable identity are rejected before the agent runs, except for the
    unauthenticated readiness/health probes.
    """

    _OPEN_PATHS = frozenset({"/readiness", "/health"})

    def __init__(self, app: Any, settings: Settings | None = None) -> None:
        super().__init__(app)
        self._settings = settings or get_settings()

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        if request.url.path in self._OPEN_PATHS:
            return await call_next(request)

        headers = {key.lower(): value for key, value in request.headers.items()}
        try:
            principal = resolve_principal(headers, self._settings)
        except PermissionDeniedError as exc:
            logger.warning("Rejected request with invalid identity header")
            return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=403)

        if principal is None:
            return JSONResponse(
                {
                    "error": {
                        "code": "unauthenticated",
                        "message": (
                            f"Missing '{self._settings.principal_header}' header. The calling "
                            "application must inject the authenticated user id."
                        ),
                    }
                },
                status_code=401,
            )

        with use_principal(principal):
            return await call_next(request)


async def _health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def create_server(*, agent: Any | None = None, settings: Settings | None = None) -> Any:
    """Create the configured :class:`ResponsesHostServer`."""
    from agent_framework.foundry import ResponsesHostServer

    from .agent import build_agent

    settings = settings or get_settings()
    server = ResponsesHostServer(
        agent=agent if agent is not None else build_agent(settings=settings),
        history_source="agent_server",
    )
    server.add_route("/health", _health, methods=["GET"])
    server.add_middleware(PrincipalMiddleware, settings=settings)
    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if settings.local_dev_mode:
        logger.warning(
            "Local development mode is on: unauthenticated requests are treated as principal '%s'. "
            "Never enable OMNIPORT_DEV_PRINCIPAL_ID in a deployed environment.",
            settings.dev_principal_id,
        )
    create_server(settings=settings).run(host=settings.host, port=settings.port)


if __name__ == "__main__":  # pragma: no cover
    main()
