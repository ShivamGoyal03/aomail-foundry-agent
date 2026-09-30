"""Configuration and secret handling.

Secrets are loaded from the environment into :class:`pydantic.SecretStr` fields
and are only ever unwrapped inside a connector at the moment an outbound
provider request is built. Account configuration objects are *server-side only*
and are never returned from an agent tool.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic import ValidationError as _PydanticValidationError

from .errors import ConfigurationError
from .models import Provider

__all__ = [
    "AccountConfig",
    "GmailCredentials",
    "ImapCredentials",
    "MicrosoftCredentials",
    "Settings",
    "get_settings",
    "load_accounts",
]


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GmailCredentials(_Config):
    """OAuth2 installed/web-app refresh-token credentials for the Gmail API.

    Extension point: swap ``refresh_token`` for a workload-identity or
    Secret Manager lookup by subclassing and overriding the token exchange in
    :class:`omniport_agent.connectors.gmail.GmailConnector`.
    """

    kind: Literal["gmail"] = "gmail"
    client_id: str
    client_secret: SecretStr
    refresh_token: SecretStr
    token_uri: str = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a secret
    api_base_url: str = "https://gmail.googleapis.com/gmail/v1"


class MicrosoftCredentials(_Config):
    """Microsoft Entra ID credentials used against Microsoft Graph.

    Supports the delegated refresh-token flow (``refresh_token`` set) and the
    application/client-credentials flow (``refresh_token`` unset, which requires
    ``mailbox_upn`` so Graph knows which mailbox to act on).
    """

    kind: Literal["microsoft"] = "microsoft"
    tenant_id: str = "common"
    client_id: str
    client_secret: SecretStr
    refresh_token: SecretStr | None = None
    mailbox_upn: str | None = None
    scope: str = "https://graph.microsoft.com/.default"
    authority: str = "https://login.microsoftonline.com"
    api_base_url: str = "https://graph.microsoft.com/v1.0"


class ImapCredentials(_Config):
    """Generic IMAP-over-TLS read access plus SMTP submission.

    ``password`` is typically a provider-issued *app password*. Providers that
    mandate OAuth2 (XOAUTH2) should use the Gmail/Microsoft connectors instead;
    see the README for the XOAUTH2 extension point.
    """

    kind: Literal["imap"] = "imap"
    imap_host: str
    imap_port: int = 993
    imap_use_ssl: bool = True
    username: str
    password: SecretStr
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_starttls: bool = True
    default_folder: str = "INBOX"


class MockCredentials(_Config):
    """Marker for the in-memory connector used for local runs and tests."""

    kind: Literal["mock"] = "mock"


Credentials = Annotated[
    GmailCredentials | MicrosoftCredentials | ImapCredentials | MockCredentials,
    Field(discriminator="kind"),
]


class AccountConfig(_Config):
    """A mailbox linked to a principal. Never exposed to the model."""

    account_id: str
    provider: Provider
    email: str
    display_name: str | None = None
    #: Identifier of the application user that owns this mailbox. The model
    #: cannot choose this value; it is injected server-side from the request.
    principal_id: str
    credentials: Credentials

    def public_email(self) -> str:
        return self.email


class Settings(_Config):
    """Process-wide settings, all overridable through environment variables."""

    agent_name: str = "OmniPortEmailAgent"
    model_deployment: str | None = None
    foundry_project_endpoint: str | None = None

    host: str = "0.0.0.0"  # noqa: S104 - container listener, fronted by the host
    port: int = 8088

    #: Header carrying the authenticated principal, injected by the calling
    #: application (never by the model).
    principal_header: str = "x-omniport-principal-id"
    #: When set, incoming principal headers must be accompanied by a valid
    #: HMAC-SHA256 signature in ``principal_signature_header``.
    principal_signing_key: SecretStr | None = None
    principal_signature_header: str = "x-omniport-principal-signature"
    #: Used only when no principal header is present and the service runs in
    #: local development mode.
    dev_principal_id: str | None = None

    request_timeout_seconds: float = 30.0
    max_page_size: int = 50
    max_body_characters: int = 8000

    @property
    def local_dev_mode(self) -> bool:
        return self.dev_principal_id is not None


_ENV_PREFIX = "OMNIPORT_"


def _env_overrides() -> dict[str, Any]:
    fields = Settings.model_fields
    values: dict[str, Any] = {}
    for name in fields:
        raw = os.environ.get(_ENV_PREFIX + name.upper())
        if raw is not None and raw != "":
            values[name] = raw
    # Foundry injects these under its own names.
    if "foundry_project_endpoint" not in values and os.environ.get("FOUNDRY_PROJECT_ENDPOINT"):
        values["foundry_project_endpoint"] = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    if "model_deployment" not in values and os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME"):
        values["model_deployment"] = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
    if "port" not in values and os.environ.get("PORT"):
        values["port"] = os.environ["PORT"]
    return values


def _build_settings() -> Settings:
    try:
        return Settings(**_env_overrides())
    except _PydanticValidationError as exc:  # pragma: no cover - defensive
        raise ConfigurationError(f"Invalid settings: {exc.error_count()} invalid value(s)") from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached process settings."""
    return _build_settings()


def load_accounts(raw: str | None = None) -> tuple[AccountConfig, ...]:
    """Parse account configuration from ``OMNIPORT_ACCOUNTS`` (a JSON array).

    Returns an empty tuple when nothing is configured, which lets the service
    start with only the built-in mock mailbox.
    """
    payload = raw if raw is not None else os.environ.get("OMNIPORT_ACCOUNTS", "").strip()
    if not payload:
        return ()
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        raise ConfigurationError("OMNIPORT_ACCOUNTS is not valid JSON") from None
    if not isinstance(parsed, list):
        raise ConfigurationError("OMNIPORT_ACCOUNTS must be a JSON array of account objects")
    accounts: list[AccountConfig] = []
    for index, item in enumerate(parsed):
        try:
            accounts.append(AccountConfig.model_validate(item))
        except _PydanticValidationError as exc:
            fields = ", ".join(str(err["loc"][0]) for err in exc.errors() if err["loc"])
            raise ConfigurationError(
                f"OMNIPORT_ACCOUNTS[{index}] is invalid (problem fields: {fields or 'unknown'})"
            ) from None
    ids = [account.account_id for account in accounts]
    duplicates = {name for name in ids if ids.count(name) > 1}
    if duplicates:
        raise ConfigurationError(f"Duplicate account_id values in OMNIPORT_ACCOUNTS: {sorted(duplicates)}")
    return tuple(accounts)
