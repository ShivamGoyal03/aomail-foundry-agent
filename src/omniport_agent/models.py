"""Provider-neutral data model shared by every connector.

The shapes here are what the language model actually sees, so they deliberately
contain no credentials, no provider-internal tokens and no raw headers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AccountInfo",
    "AttachmentInfo",
    "EmailAddress",
    "MessageDetail",
    "MessagePage",
    "MessageSummary",
    "OutgoingMessage",
    "Provider",
    "ProviderCapabilities",
    "SearchCriteria",
    "SendResult",
]

Provider = Literal["gmail", "microsoft", "imap", "mock"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EmailAddress(_Model):
    """An RFC 5322 mailbox."""

    address: str
    name: str | None = None

    def formatted(self) -> str:
        return f"{self.name} <{self.address}>" if self.name else self.address


class AttachmentInfo(_Model):
    """Attachment metadata. Content is never inlined into tool results."""

    attachment_id: str | None = None
    filename: str
    mime_type: str | None = None
    size_bytes: int | None = None


class MessageSummary(_Model):
    """Lightweight message record returned by list/search operations."""

    account_id: str
    provider: Provider
    message_id: str
    thread_id: str | None = None
    subject: str = ""
    sender: EmailAddress | None = None
    recipients: tuple[EmailAddress, ...] = ()
    cc: tuple[EmailAddress, ...] = ()
    received_at: datetime | None = None
    snippet: str = ""
    is_read: bool = False
    has_attachments: bool = False
    folder: str | None = None
    labels: tuple[str, ...] = ()


class MessageDetail(MessageSummary):
    """Full message record returned by read operations."""

    body_text: str = ""
    body_truncated: bool = False
    attachments: tuple[AttachmentInfo, ...] = ()
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()


class MessagePage(_Model):
    """One page of results plus an opaque cursor for the next page."""

    messages: tuple[MessageSummary, ...] = ()
    next_page_token: str | None = None


class OutgoingMessage(_Model):
    """A message the agent proposes to send. Always reviewed before sending."""

    to: tuple[EmailAddress, ...]
    subject: str = ""
    body_text: str = ""
    cc: tuple[EmailAddress, ...] = ()
    bcc: tuple[EmailAddress, ...] = ()
    in_reply_to_message_id: str | None = None


class SendResult(_Model):
    """Outcome of a send/reply operation."""

    account_id: str
    provider: Provider
    message_id: str | None = None
    thread_id: str | None = None
    accepted_recipients: tuple[str, ...] = ()


class ProviderCapabilities(_Model):
    """What a given connector can actually do, for honest tool responses."""

    can_search: bool = True
    can_send: bool = True
    can_reply: bool = True
    supports_threads: bool = False
    supports_labels: bool = False
    supports_server_side_query: bool = True
    max_page_size: int = 50


class AccountInfo(_Model):
    """Model-visible description of a linked mailbox. Contains no secrets."""

    account_id: str
    provider: Provider
    email: str
    display_name: str | None = None
    capabilities: ProviderCapabilities = Field(default_factory=ProviderCapabilities)


class SearchCriteria(_Model):
    """Normalized search request translated per provider."""

    query: str = ""
    sender: str | None = None
    recipient: str | None = None
    subject: str | None = None
    unread_only: bool = False
    folder: str | None = None
    limit: int = 20
    page_token: str | None = None
