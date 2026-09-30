"""In-memory connector so the service is runnable without provider credentials.

It is used by the tests and by ``OMNIPORT_ENABLE_MOCK_ACCOUNT=1`` local runs.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

from ..errors import NotFoundError, ValidationError
from ..models import (
    EmailAddress,
    MessageDetail,
    MessagePage,
    MessageSummary,
    OutgoingMessage,
    ProviderCapabilities,
    SearchCriteria,
    SendResult,
)
from ._common import reply_subject, truncate_body
from .base import EmailConnector

__all__ = ["MockConnector", "seed_messages"]

_COUNTER = itertools.count(1000)


def seed_messages(account_id: str) -> list[MessageDetail]:
    base = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    samples = [
        (
            "m-1",
            "Quarterly report is ready",
            EmailAddress(address="dana@example.com", name="Dana Lee"),
            "Hi,\n\nThe Q4 report is attached in the shared drive. Let me know if you "
            "want the summary slide before Friday.\n\nDana",
            False,
        ),
        (
            "m-2",
            "Re: Vendor renewal",
            EmailAddress(address="sam@vendor.example", name="Sam Ortiz"),
            "Following up on the renewal quote. We can hold the current pricing until "
            "the end of the month.\n\nSam",
            True,
        ),
        (
            "m-3",
            "Lunch tomorrow?",
            EmailAddress(address="alex@example.com", name="Alex Kim"),
            "Are you free around noon? Happy to meet near the office.",
            True,
        ),
    ]
    return [
        MessageDetail(
            account_id=account_id,
            provider="mock",
            message_id=message_id,
            thread_id=f"t-{message_id}",
            subject=subject,
            sender=sender,
            recipients=(EmailAddress(address="user@example.com"),),
            received_at=base + timedelta(hours=index),
            snippet=body.splitlines()[0][:120],
            is_read=is_read,
            folder="INBOX",
            body_text=body,
            in_reply_to=f"<{message_id}@mock.local>",
        )
        for index, (message_id, subject, sender, body, is_read) in enumerate(samples)
    ]


class MockConnector(EmailConnector):
    """A deterministic mailbox backed by a Python list."""

    provider = "mock"

    def __init__(
        self,
        account_id: str = "mock-inbox",
        email: str = "user@example.com",
        *,
        display_name: str | None = "Mock Mailbox",
        messages: list[MessageDetail] | None = None,
        max_body_characters: int = 8000,
    ) -> None:
        super().__init__(account_id, email, display_name)
        self._messages = messages if messages is not None else seed_messages(account_id)
        self._max_body_characters = max_body_characters
        #: Everything the connector was asked to send, for assertions in tests.
        self.sent: list[OutgoingMessage] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_threads=True, max_page_size=25)

    def _matches(self, message: MessageDetail, criteria: SearchCriteria) -> bool:
        if criteria.unread_only and message.is_read:
            return False
        if criteria.folder and (message.folder or "").lower() != criteria.folder.lower():
            return False
        if criteria.sender and criteria.sender.lower() not in (
            message.sender.address.lower() if message.sender else ""
        ):
            return False
        if criteria.subject and criteria.subject.lower() not in message.subject.lower():
            return False
        if criteria.query:
            haystack = f"{message.subject}\n{message.body_text}".lower()
            if criteria.query.lower() not in haystack:
                return False
        return True

    async def list_messages(self, criteria: SearchCriteria) -> MessagePage:
        limit = max(1, min(criteria.limit, self.capabilities.max_page_size))
        offset = 0
        if criteria.page_token:
            try:
                offset = int(criteria.page_token)
            except ValueError:
                raise ValidationError("The page token is not valid for this mailbox.") from None
        matches = [m for m in self._messages if self._matches(m, criteria)]
        window = matches[offset : offset + limit]
        next_offset = offset + limit
        return MessagePage(
            messages=tuple(MessageSummary(**m.model_dump(include=set(MessageSummary.model_fields))) for m in window),
            next_page_token=str(next_offset) if next_offset < len(matches) else None,
        )

    async def get_message(self, message_id: str) -> MessageDetail:
        for message in self._messages:
            if message.message_id == message_id:
                body, truncated = truncate_body(message.body_text, self._max_body_characters)
                return message.model_copy(update={"body_text": body, "body_truncated": truncated})
        raise NotFoundError(f"Message '{message_id}' was not found in this mailbox.")

    async def send_message(self, message: OutgoingMessage) -> SendResult:
        if not message.to:
            raise ValidationError("At least one recipient is required.")
        self.sent.append(message)
        return SendResult(
            account_id=self.account_id,
            provider=self.provider,
            message_id=f"sent-{next(_COUNTER)}",
            accepted_recipients=tuple(addr.address for addr in (*message.to, *message.cc, *message.bcc)),
        )

    async def reply_message(self, message_id: str, body_text: str, reply_all: bool = False) -> SendResult:
        original = await self.get_message(message_id)
        recipients = [original.sender] if original.sender else []
        cc = ()
        if reply_all:
            own = self.email.lower()
            recipients += [a for a in original.recipients if a.address.lower() != own]
            cc = tuple(a for a in original.cc if a.address.lower() != own)
        if not recipients:
            raise ValidationError("The original message has no sender to reply to.")
        result = await self.send_message(
            OutgoingMessage(
                to=tuple(recipients),
                cc=cc,
                subject=reply_subject(original.subject),
                body_text=body_text,
                in_reply_to_message_id=original.in_reply_to,
            )
        )
        return result.model_copy(update={"thread_id": original.thread_id})
