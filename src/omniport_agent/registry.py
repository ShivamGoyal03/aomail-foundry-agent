"""Account registry and connector factory.

The registry is the *only* place that maps a principal to mailboxes. Tools
never receive an account configuration object, only an ``account_id`` that the
registry has already confirmed belongs to the current principal.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .config import (
    AccountConfig,
    GmailCredentials,
    ImapCredentials,
    MicrosoftCredentials,
    MockCredentials,
    Settings,
    get_settings,
    load_accounts,
)
from .connectors.base import EmailConnector
from .connectors.gmail import GmailConnector
from .connectors.imap_smtp import ImapConnector
from .connectors.microsoft import MicrosoftConnector
from .connectors.mock import MockConnector
from .errors import AccountNotFoundError, ConfigurationError
from .identity import Principal

__all__ = ["AccountRegistry", "build_connector", "default_registry"]

logger = logging.getLogger("omniport_agent.registry")

_MOCK_ACCOUNT_ID = "mock-inbox"


def build_connector(account: AccountConfig, settings: Settings) -> EmailConnector:
    """Instantiate the connector implementing ``account``'s provider."""
    credentials = account.credentials
    common: dict[str, Any] = {
        "display_name": account.display_name,
        "timeout": settings.request_timeout_seconds,
        "max_body_characters": settings.max_body_characters,
    }
    if isinstance(credentials, GmailCredentials):
        return GmailConnector(account.account_id, account.email, credentials, **common)
    if isinstance(credentials, MicrosoftCredentials):
        return MicrosoftConnector(account.account_id, account.email, credentials, **common)
    if isinstance(credentials, ImapCredentials):
        return ImapConnector(account.account_id, account.email, credentials, **common)
    if isinstance(credentials, MockCredentials):
        return MockConnector(
            account.account_id,
            account.email,
            display_name=account.display_name,
            max_body_characters=settings.max_body_characters,
        )
    raise ConfigurationError(f"Unsupported provider for account '{account.account_id}'.")


class AccountRegistry:
    """Maps principals to the mailboxes they are allowed to use."""

    def __init__(self, accounts: tuple[AccountConfig, ...], settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._accounts = {account.account_id: account for account in accounts}
        self._connectors: dict[str, EmailConnector] = {}

    @classmethod
    def from_environment(cls, settings: Settings | None = None) -> AccountRegistry:
        settings = settings or get_settings()
        accounts = list(load_accounts())
        if _should_enable_mock(accounts, settings):
            accounts.append(
                AccountConfig(
                    account_id=_MOCK_ACCOUNT_ID,
                    provider="mock",
                    email="user@example.com",
                    display_name="Mock Mailbox",
                    principal_id=str(settings.dev_principal_id or "local-dev"),
                    credentials=MockCredentials(),
                )
            )
        return cls(tuple(accounts), settings)

    def accounts_for(self, principal: Principal) -> tuple[AccountConfig, ...]:
        return tuple(
            account for account in self._accounts.values() if account.principal_id == principal.principal_id
        )

    def resolve(self, principal: Principal, account_id: str) -> AccountConfig:
        """Return the account, enforcing the principal ownership boundary.

        A mailbox owned by a different principal raises exactly the same error
        as an unknown one, so the model cannot enumerate other users' accounts.
        The distinction is only recorded server-side, in the log.
        """
        account = self._accounts.get(account_id)
        if account is not None and account.principal_id != principal.principal_id:
            logger.warning(
                "Blocked cross-principal mailbox access attempt by principal %r", principal.principal_id
            )
            account = None
        if account is None:
            raise AccountNotFoundError(
                f"No mailbox with id '{account_id}' is available. "
                "Call list_accounts to see the ids you can use."
            )
        return account

    def connector(self, principal: Principal, account_id: str) -> EmailConnector:
        """Return a cached connector for an account owned by ``principal``."""
        account = self.resolve(principal, account_id)
        connector = self._connectors.get(account.account_id)
        if connector is None:
            connector = build_connector(account, self._settings)
            self._connectors[account.account_id] = connector
        return connector

    async def aclose(self) -> None:
        for connector in self._connectors.values():
            await connector.aclose()
        self._connectors.clear()


def _should_enable_mock(accounts: list[AccountConfig], settings: Settings) -> bool:
    flag = os.environ.get("OMNIPORT_ENABLE_MOCK_ACCOUNT", "").strip().lower()
    if flag in {"1", "true", "yes"}:
        return True
    if flag in {"0", "false", "no"}:
        return False
    # Default: only when nothing else is configured and we are in dev mode.
    return not accounts and settings.local_dev_mode


_DEFAULT: AccountRegistry | None = None


def default_registry() -> AccountRegistry:
    """Process-wide registry built from the environment on first use."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = AccountRegistry.from_environment()
    return _DEFAULT
