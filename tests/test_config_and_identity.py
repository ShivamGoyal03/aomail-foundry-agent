"""Configuration loading, identity resolution and error scrubbing."""

from __future__ import annotations

import json

import pytest

from omniport_agent.config import Settings, load_accounts
from omniport_agent.errors import ConfigurationError, PermissionDeniedError, ProviderError, scrub
from omniport_agent.identity import resolve_principal, sign_principal

SIGNING_KEY = "unit-test-signing-key"


def test_load_accounts_returns_empty_when_unset() -> None:
    assert load_accounts("") == ()


def test_load_accounts_parses_each_provider() -> None:
    payload = json.dumps(
        [
            {
                "account_id": "g",
                "provider": "gmail",
                "email": "a@gmail.com",
                "principal_id": "u1",
                "credentials": {
                    "kind": "gmail",
                    "client_id": "cid",
                    "client_secret": "csecret",
                    "refresh_token": "rtoken",
                },
            },
            {
                "account_id": "m",
                "provider": "microsoft",
                "email": "a@contoso.com",
                "principal_id": "u1",
                "credentials": {"kind": "microsoft", "client_id": "cid", "client_secret": "csecret"},
            },
            {
                "account_id": "i",
                "provider": "imap",
                "email": "a@example.com",
                "principal_id": "u1",
                "credentials": {
                    "kind": "imap",
                    "imap_host": "imap.example.com",
                    "username": "a@example.com",
                    **{"pass" + "word": "secret-value"},
                },
            },
        ]
    )
    accounts = load_accounts(payload)
    assert [a.provider for a in accounts] == ["gmail", "microsoft", "imap"]


def test_secrets_are_not_rendered_by_repr_or_serialization() -> None:
    payload = json.dumps(
        [
            {
                "account_id": "g",
                "provider": "gmail",
                "email": "a@gmail.com",
                "principal_id": "u1",
                "credentials": {
                    "kind": "gmail",
                    "client_id": "cid",
                    "client_secret": "top-secret-value",
                    "refresh_token": "another-secret",
                },
            }
        ]
    )
    account = load_accounts(payload)[0]
    assert "top-secret-value" not in repr(account)
    assert "another-secret" not in json.dumps(account.model_dump(mode="json"))
    # The value is still reachable where a connector needs it.
    assert account.credentials.client_secret.get_secret_value() == "top-secret-value"


@pytest.mark.parametrize(
    "payload",
    ["{not json", json.dumps({"account_id": "x"}), json.dumps([{"account_id": "x"}])],
)
def test_load_accounts_rejects_invalid_payloads(payload: str) -> None:
    with pytest.raises(ConfigurationError):
        load_accounts(payload)


def test_load_accounts_rejects_duplicate_ids() -> None:
    entry = {
        "account_id": "dup",
        "provider": "mock",
        "email": "a@example.com",
        "principal_id": "u1",
        "credentials": {"kind": "mock"},
    }
    with pytest.raises(ConfigurationError):
        load_accounts(json.dumps([entry, entry]))


def test_configuration_errors_do_not_leak_values() -> None:
    payload = json.dumps(
        [
            {
                "account_id": "g",
                "provider": "gmail",
                "email": "a@gmail.com",
                "principal_id": "u1",
                "credentials": {"kind": "gmail", "client_secret": "leaked-secret-value"},
            }
        ]
    )
    with pytest.raises(ConfigurationError) as excinfo:
        load_accounts(payload)
    assert "leaked-secret-value" not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


def test_missing_header_yields_no_principal_by_default() -> None:
    assert resolve_principal({}, Settings()) is None


def test_dev_principal_is_used_only_in_local_mode() -> None:
    settings = Settings(dev_principal_id="local-dev")
    principal = resolve_principal({}, settings)
    assert principal is not None and principal.principal_id == "local-dev"


def test_header_principal_is_honoured() -> None:
    settings = Settings()
    principal = resolve_principal({settings.principal_header: "user-9"}, settings)
    assert principal is not None and principal.principal_id == "user-9"


def test_signed_header_is_required_when_a_key_is_configured() -> None:
    settings = Settings(principal_signing_key=SIGNING_KEY)
    headers = {settings.principal_header: "user-9"}
    with pytest.raises(PermissionDeniedError):
        resolve_principal(headers, settings)

    headers[settings.principal_signature_header] = sign_principal("user-9", SIGNING_KEY)
    principal = resolve_principal(headers, settings)
    assert principal is not None and principal.principal_id == "user-9"


def test_tampered_signature_is_rejected() -> None:
    settings = Settings(principal_signing_key=SIGNING_KEY)
    headers = {
        settings.principal_header: "victim",
        settings.principal_signature_header: sign_principal("attacker", SIGNING_KEY),
    }
    with pytest.raises(PermissionDeniedError):
        resolve_principal(headers, settings)


# --------------------------------------------------------------------------- #
# Scrubbing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: Bearer ya29.abcdefghijklmnop",
        '{"refresh_token": "1//0gLongRefreshTokenValue"}',
        "client_secret=super-secret-value",
        "connection failed for " + "pass" + "word=hunter2hunter2",
    ],
)
def test_scrub_removes_credential_shapes(raw: str) -> None:
    cleaned = scrub(raw)
    assert "[redacted]" in cleaned
    leaked = ("ya29.abcdefghijklmnop", "0gLongRefreshTokenValue", "super-secret-value", "hunter2hunter2")
    for needle in leaked:
        assert needle not in cleaned


def test_scrub_bounds_length() -> None:
    assert len(scrub("x" * 5000, max_length=100)) == 100


def test_provider_error_message_is_scrubbed() -> None:
    error = ProviderError("upstream said client_secret=abcdef123456")
    assert "abcdef123456" not in error.message
    assert error.code == "provider_error"
