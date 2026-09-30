"""Provider-neutral email connector interface."""

from __future__ import annotations

import abc

from ..models import (
    AccountInfo,
    MessageDetail,
    MessagePage,
    OutgoingMessage,
    ProviderCapabilities,
    Provider,
    SearchCriteria,
    SendResult,
)

__all__ = ["EmailConnector"]


class EmailConnector(abc.ABC):
    """Uniform surface over Gmail, Microsoft Graph and IMAP/SMTP mailboxes.

    Implementations must:

    * normalize provider payloads into :mod:`omniport_agent.models` types;
    * translate provider failures into :mod:`omniport_agent.errors` types whose
      messages contain no credentials;
    * honour ``criteria.limit`` and expose paging through an opaque
      ``next_page_token``.
    """

    provider: Provider

    def __init__(self, account_id: str, email: str, display_name: str | None = None) -> None:
        self.account_id = account_id
        self.email = email
        self.display_name = display_name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def account_info(self) -> AccountInfo:
        return AccountInfo(
            account_id=self.account_id,
            provider=self.provider,
            email=self.email,
            display_name=self.display_name,
            capabilities=self.capabilities,
        )

    @abc.abstractmethod
    async def list_messages(self, criteria: SearchCriteria) -> MessagePage:
        """Search or list messages in the mailbox."""

    @abc.abstractmethod
    async def get_message(self, message_id: str) -> MessageDetail:
        """Read a single message, including its plain-text body."""

    @abc.abstractmethod
    async def send_message(self, message: OutgoingMessage) -> SendResult:
        """Send a new message. Callers must have obtained human approval."""

    @abc.abstractmethod
    async def reply_message(self, message_id: str, body_text: str, reply_all: bool = False) -> SendResult:
        """Reply to an existing message. Callers must have obtained approval."""

    async def aclose(self) -> None:
        """Release network resources. Safe to call more than once."""
        return None

    async def __aenter__(self) -> "EmailConnector":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
