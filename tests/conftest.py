"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from omniport_agent.config import AccountConfig, MockCredentials, Settings
from omniport_agent.drafts import DraftStore
from omniport_agent.identity import Principal, use_principal
from omniport_agent.registry import AccountRegistry
from omniport_agent.tools import EmailToolset

PRINCIPAL = Principal(principal_id="user-1")
OTHER_PRINCIPAL = Principal(principal_id="user-2")


@pytest.fixture
def settings() -> Settings:
    return Settings(max_body_characters=200, max_page_size=10)


@pytest.fixture
def accounts() -> tuple[AccountConfig, ...]:
    return (
        AccountConfig(
            account_id="mock-inbox",
            provider="mock",
            email="user@example.com",
            display_name="Mock Mailbox",
            principal_id=PRINCIPAL.principal_id,
            credentials=MockCredentials(),
        ),
        AccountConfig(
            account_id="someone-else",
            provider="mock",
            email="other@example.com",
            principal_id=OTHER_PRINCIPAL.principal_id,
            credentials=MockCredentials(),
        ),
    )


@pytest.fixture
def registry(accounts: tuple[AccountConfig, ...], settings: Settings) -> AccountRegistry:
    return AccountRegistry(accounts, settings)


@pytest.fixture
def toolset(registry: AccountRegistry, settings: Settings) -> EmailToolset:
    return EmailToolset(registry, drafts=DraftStore(), settings=settings)


@pytest.fixture
def as_user():
    """Run a block as the default principal."""
    return use_principal
