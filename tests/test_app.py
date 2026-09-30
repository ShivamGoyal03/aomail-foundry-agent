"""HTTP surface: health probe and the identity middleware."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from omniport_agent.app import PrincipalMiddleware, _health
from omniport_agent.config import Settings
from omniport_agent.identity import current_principal, sign_principal

SIGNING_KEY = "unit-test-signing-key"


async def _whoami(_request) -> JSONResponse:
    return JSONResponse({"principal_id": current_principal().principal_id})


def _client(settings: Settings) -> TestClient:
    app = Starlette(
        routes=[
            Route("/health", _health, methods=["GET"]),
            Route("/readiness", _health, methods=["GET"]),
            Route("/responses", _whoami, methods=["GET"]),
        ]
    )
    app.add_middleware(PrincipalMiddleware, settings=settings)
    return TestClient(app)


def test_health_and_readiness_need_no_identity() -> None:
    client = _client(Settings())
    for path in ("/health", "/readiness"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_unauthenticated_requests_are_rejected() -> None:
    response = _client(Settings()).get("/responses")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_principal_header_is_bound_to_the_request_context() -> None:
    settings = Settings()
    response = _client(settings).get("/responses", headers={settings.principal_header: "user-7"})
    assert response.status_code == 200
    assert response.json() == {"principal_id": "user-7"}


def test_signed_principal_header_round_trip() -> None:
    settings = Settings(principal_signing_key=SIGNING_KEY)
    client = _client(settings)

    unsigned = client.get("/responses", headers={settings.principal_header: "user-7"})
    assert unsigned.status_code == 403

    signed = client.get(
        "/responses",
        headers={
            settings.principal_header: "user-7",
            settings.principal_signature_header: sign_principal("user-7", SIGNING_KEY),
        },
    )
    assert signed.status_code == 200
    assert signed.json() == {"principal_id": "user-7"}


def test_local_dev_mode_supplies_a_principal() -> None:
    settings = Settings(dev_principal_id="local-dev")
    response = _client(settings).get("/responses")
    assert response.status_code == 200
    assert response.json() == {"principal_id": "local-dev"}


@pytest.mark.parametrize("header_value", ["", "   "])
def test_blank_principal_header_is_not_accepted(header_value: str) -> None:
    settings = Settings()
    response = _client(settings).get("/responses", headers={settings.principal_header: header_value})
    assert response.status_code == 401
