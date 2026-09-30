"""Shared helpers for connectors: address parsing, MIME building, truncation."""

from __future__ import annotations

import re
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from datetime import datetime

from ..models import AttachmentInfo, EmailAddress, OutgoingMessage

__all__ = [
    "parse_addresses",
    "parse_address",
    "build_mime",
    "truncate_body",
    "html_to_text",
    "parse_date",
    "reply_subject",
]

_TAG_RE = re.compile(r"(?s)<(script|style).*?</\1>|<[^>]+>")
_WS_RE = re.compile(r"[ \t]*\n[ \t]*")


def parse_addresses(raw: str | None) -> tuple[EmailAddress, ...]:
    """Parse a raw address header into normalized mailboxes."""
    if not raw:
        return ()
    result = []
    for name, address in getaddresses([raw]):
        if not address:
            continue
        result.append(EmailAddress(address=address.strip(), name=name.strip() or None))
    return tuple(result)


def parse_address(raw: str | None) -> EmailAddress | None:
    addresses = parse_addresses(raw)
    return addresses[0] if addresses else None


def parse_date(raw: str | None) -> datetime | None:
    """Parse an RFC 2822 / ISO 8601 date header, tolerating malformed input."""
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def html_to_text(html: str) -> str:
    """Best-effort HTML to plain text, used when no text/plain part exists."""
    import html as _html

    text = _TAG_RE.sub(" ", html)
    text = _html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    # Stripping inline tags can leave a gap before punctuation.
    text = re.sub(r" +([.,;:!?)\]])", r"\1", text)
    return _WS_RE.sub("\n", text).strip()


def truncate_body(text: str, max_characters: int) -> tuple[str, bool]:
    """Bound a message body so tool results stay within a predictable size."""
    if max_characters <= 0 or len(text) <= max_characters:
        return text, False
    return text[:max_characters], True


def reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()


def build_mime(
    message: OutgoingMessage,
    sender: str,
    *,
    references: tuple[str, ...] = (),
) -> EmailMessage:
    """Build an RFC 5322 message for SMTP submission or raw-MIME APIs."""
    mime = EmailMessage()
    mime["From"] = sender
    mime["To"] = ", ".join(addr.formatted() for addr in message.to)
    if message.cc:
        mime["Cc"] = ", ".join(addr.formatted() for addr in message.cc)
    if message.bcc:
        mime["Bcc"] = ", ".join(addr.formatted() for addr in message.bcc)
    mime["Subject"] = message.subject
    if message.in_reply_to_message_id:
        mime["In-Reply-To"] = message.in_reply_to_message_id
    if references:
        mime["References"] = " ".join(references)
    mime.set_content(message.body_text)
    return mime


def attachment_from_part(filename: str, mime_type: str | None, size: int | None, part_id: str | None) -> AttachmentInfo:
    return AttachmentInfo(
        attachment_id=part_id,
        filename=filename,
        mime_type=mime_type,
        size_bytes=size,
    )
