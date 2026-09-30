"""Generic IMAP-over-TLS + SMTP-submission connector.

Works with any provider that still allows password/app-password authentication
(Yahoo, Fastmail, custom domains, Gmail and Outlook in IMAP fallback mode).
Blocking ``imaplib``/``smtplib`` calls are offloaded to worker threads so the
agent event loop is never blocked.

Extension point for XOAUTH2: override :meth:`ImapConnector._authenticate` and
call ``imap.authenticate("XOAUTH2", ...)`` with a provider-issued access token.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import smtplib
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from email.message import Message as RawMessage

from ..config import ImapCredentials
from ..errors import (
    AuthenticationError,
    NotFoundError,
    ProviderError,
    ProviderTimeoutError,
    ValidationError,
)
from ..models import (
    AttachmentInfo,
    EmailAddress,
    MessageDetail,
    MessagePage,
    MessageSummary,
    OutgoingMessage,
    ProviderCapabilities,
    SearchCriteria,
    SendResult,
)
from ._common import (
    build_mime,
    html_to_text,
    parse_address,
    parse_addresses,
    parse_date,
    reply_subject,
    truncate_body,
)
from .base import EmailConnector

__all__ = ["ImapConnector"]

_PROVIDER = "IMAP/SMTP"


def _quote(value: str) -> str:
    """Quote an IMAP search term, neutralising quotes and CRLF injection."""
    cleaned = value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")
    return f'"{cleaned}"'


class ImapConnector(EmailConnector):
    provider = "imap"

    def __init__(
        self,
        account_id: str,
        email_address: str,
        credentials: ImapCredentials,
        *,
        display_name: str | None = None,
        timeout: float = 30.0,
        max_body_characters: int = 8000,
    ) -> None:
        super().__init__(account_id, email_address, display_name)
        self._credentials = credentials
        self._timeout = timeout
        self._max_body_characters = max_body_characters

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            can_send=self._credentials.smtp_host is not None,
            can_reply=self._credentials.smtp_host is not None,
            supports_threads=False,
            supports_labels=False,
            supports_server_side_query=True,
            max_page_size=50,
        )

    # -- connections ----------------------------------------------------------

    @contextmanager
    def _imap(self, folder: str) -> Iterator[imaplib.IMAP4]:
        creds = self._credentials
        try:
            if creds.imap_use_ssl:
                connection: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                    creds.imap_host,
                    creds.imap_port,
                    timeout=self._timeout,
                    ssl_context=ssl.create_default_context(),
                )
            else:
                connection = imaplib.IMAP4(creds.imap_host, creds.imap_port, timeout=self._timeout)
                connection.starttls(ssl_context=ssl.create_default_context())
        except TimeoutError:
            raise ProviderTimeoutError(
                "The IMAP server did not respond within the configured timeout."
            ) from None
        except (OSError, ssl.SSLError, imaplib.IMAP4.error) as exc:
            raise ProviderError(f"Could not connect to the IMAP server: {type(exc).__name__}.") from None

        try:
            self._authenticate(connection)
            status, _ = connection.select(_quote(folder), readonly=True)
            if status != "OK":
                raise NotFoundError(f"IMAP folder '{folder}' does not exist or is not selectable.")
            yield connection
        finally:
            try:
                connection.logout()
            except (imaplib.IMAP4.error, OSError):  # pragma: no cover - best effort
                pass

    def _authenticate(self, connection: imaplib.IMAP4) -> None:
        """Log in to IMAP. Override for XOAUTH2-based providers."""
        try:
            connection.login(self._credentials.username, self._credentials.password.get_secret_value())
        except imaplib.IMAP4.error:
            # The server's reply can echo the submitted credential, so none of it
            # is propagated; only the fact of the failure is reported.
            raise AuthenticationError(
                "The IMAP server rejected the stored credentials for this mailbox. "
                "Re-authorize the account."
            ) from None

    # -- reads ----------------------------------------------------------------

    def _search_criteria(self, criteria: SearchCriteria) -> list[str]:
        parts: list[str] = []
        if criteria.unread_only:
            parts.append("UNSEEN")
        if criteria.sender:
            parts += ["FROM", _quote(criteria.sender)]
        if criteria.recipient:
            parts += ["TO", _quote(criteria.recipient)]
        if criteria.subject:
            parts += ["SUBJECT", _quote(criteria.subject)]
        if criteria.query:
            parts += ["TEXT", _quote(criteria.query)]
        return parts or ["ALL"]

    async def list_messages(self, criteria: SearchCriteria) -> MessagePage:
        return await asyncio.to_thread(self._list_messages_sync, criteria)

    def _list_messages_sync(self, criteria: SearchCriteria) -> MessagePage:
        limit = max(1, min(criteria.limit, self.capabilities.max_page_size))
        offset = self._decode_page_token(criteria.page_token)
        folder = criteria.folder or self._credentials.default_folder

        with self._imap(folder) as connection:
            status, data = connection.search(None, *self._search_criteria(criteria))
            if status != "OK":
                raise ProviderError("The IMAP server rejected the search request.")
            uids = (data[0] or b"").split()
            # Newest first; IMAP returns ascending sequence numbers.
            uids.reverse()
            window = uids[offset : offset + limit]
            summaries = []
            for uid in window:
                raw = self._fetch(connection, uid.decode(), "(BODY.PEEK[HEADER] FLAGS)")
                if raw is None:
                    continue
                summaries.append(self._to_summary(uid.decode(), raw, folder))

        next_offset = offset + limit
        next_token = str(next_offset) if next_offset < len(uids) else None
        return MessagePage(messages=tuple(summaries), next_page_token=next_token)

    @staticmethod
    def _decode_page_token(token: str | None) -> int:
        if not token:
            return 0
        try:
            offset = int(token)
        except ValueError:
            raise ValidationError("The page token is not valid for this mailbox.") from None
        if offset < 0:
            raise ValidationError("The page token is not valid for this mailbox.")
        return offset

    @staticmethod
    def _fetch(connection: imaplib.IMAP4, uid: str, parts: str) -> bytes | None:
        status, payload = connection.fetch(uid, parts)
        if status != "OK" or not payload:
            return None
        for item in payload:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                return bytes(item[1])
        return None

    def _to_summary(self, uid: str, raw_headers: bytes, folder: str) -> MessageSummary:
        parsed = email.message_from_bytes(raw_headers)
        return MessageSummary(
            account_id=self.account_id,
            provider=self.provider,
            message_id=uid,
            thread_id=None,
            subject=self._header(parsed, "Subject"),
            sender=parse_address(self._header(parsed, "From")),
            recipients=parse_addresses(self._header(parsed, "To")),
            cc=parse_addresses(self._header(parsed, "Cc")),
            received_at=parse_date(self._header(parsed, "Date")),
            snippet="",
            folder=folder,
        )

    @staticmethod
    def _header(message: RawMessage, name: str) -> str:
        from email.header import decode_header, make_header

        raw = message.get(name)
        if not raw:
            return ""
        try:
            return str(make_header(decode_header(raw)))
        except (UnicodeDecodeError, LookupError, ValueError):
            return str(raw)

    async def get_message(self, message_id: str) -> MessageDetail:
        return await asyncio.to_thread(self._get_message_sync, message_id)

    def _get_message_sync(self, message_id: str, folder: str | None = None) -> MessageDetail:
        if not message_id.isdigit():
            raise ValidationError("An IMAP message id must be the numeric sequence id returned by a search.")
        folder = folder or self._credentials.default_folder
        with self._imap(folder) as connection:
            raw = self._fetch(connection, message_id, "(RFC822)")
        if raw is None:
            raise NotFoundError(f"IMAP message '{message_id}' was not found in folder '{folder}'.")

        parsed = email.message_from_bytes(raw)
        body, attachments = self._extract_body(parsed)
        body_text, truncated = truncate_body(body, self._max_body_characters)
        references = tuple((self._header(parsed, "References") or "").split())
        return MessageDetail(
            account_id=self.account_id,
            provider=self.provider,
            message_id=message_id,
            subject=self._header(parsed, "Subject"),
            sender=parse_address(self._header(parsed, "From")),
            recipients=parse_addresses(self._header(parsed, "To")),
            cc=parse_addresses(self._header(parsed, "Cc")),
            received_at=parse_date(self._header(parsed, "Date")),
            snippet=body_text[:200],
            has_attachments=bool(attachments),
            folder=folder,
            body_text=body_text,
            body_truncated=truncated,
            attachments=attachments,
            in_reply_to=self._header(parsed, "Message-ID") or None,
            references=references,
        )

    @staticmethod
    def _extract_body(message: RawMessage) -> tuple[str, tuple[AttachmentInfo, ...]]:
        text_parts: list[str] = []
        html_parts: list[str] = []
        attachments: list[AttachmentInfo] = []

        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            filename = part.get_filename()
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                continue
            if filename or part.get_content_disposition() == "attachment":
                attachments.append(
                    AttachmentInfo(
                        filename=filename or "attachment",
                        mime_type=part.get_content_type(),
                        size_bytes=len(payload),
                    )
                )
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except LookupError:
                decoded = payload.decode("utf-8", errors="replace")
            if part.get_content_type() == "text/plain":
                text_parts.append(decoded)
            elif part.get_content_type() == "text/html":
                html_parts.append(decoded)

        body = "\n".join(text_parts).strip()
        if not body and html_parts:
            body = html_to_text("\n".join(html_parts))
        return body, tuple(attachments)

    # -- writes ---------------------------------------------------------------

    async def send_message(self, message: OutgoingMessage) -> SendResult:
        return await asyncio.to_thread(self._send_sync, message)

    def _send_sync(self, message: OutgoingMessage) -> SendResult:
        creds = self._credentials
        if not creds.smtp_host:
            raise ValidationError(
                "This IMAP account has no SMTP host configured, so it is read-only."
            )
        if not message.to:
            raise ValidationError("At least one recipient is required.")

        mime = build_mime(message, self.email)
        context = ssl.create_default_context()
        try:
            if creds.smtp_starttls:
                with smtplib.SMTP(creds.smtp_host, creds.smtp_port, timeout=self._timeout) as smtp:
                    smtp.starttls(context=context)
                    smtp.login(creds.username, creds.password.get_secret_value())
                    smtp.send_message(mime)
            else:
                with smtplib.SMTP_SSL(
                    creds.smtp_host, creds.smtp_port, timeout=self._timeout, context=context
                ) as smtp:
                    smtp.login(creds.username, creds.password.get_secret_value())
                    smtp.send_message(mime)
        except smtplib.SMTPAuthenticationError:
            raise AuthenticationError("The SMTP server rejected the stored credentials.") from None
        except TimeoutError:
            raise ProviderTimeoutError(
                "The SMTP server did not respond within the configured timeout."
            ) from None
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            raise ProviderError(f"SMTP submission failed: {type(exc).__name__}.") from None

        return SendResult(
            account_id=self.account_id,
            provider=self.provider,
            message_id=mime.get("Message-ID"),
            accepted_recipients=tuple(addr.address for addr in (*message.to, *message.cc, *message.bcc)),
        )

    async def reply_message(self, message_id: str, body_text: str, reply_all: bool = False) -> SendResult:
        return await asyncio.to_thread(self._reply_sync, message_id, body_text, reply_all)

    def _reply_sync(self, message_id: str, body_text: str, reply_all: bool) -> SendResult:
        original = self._get_message_sync(message_id)
        recipients: list[EmailAddress] = [original.sender] if original.sender else []
        cc: tuple[EmailAddress, ...] = ()
        if reply_all:
            own = self.email.lower()
            recipients += [addr for addr in original.recipients if addr.address.lower() != own]
            cc = tuple(addr for addr in original.cc if addr.address.lower() != own)
        if not recipients:
            raise ValidationError("The original message has no sender to reply to.")
        return self._send_sync(
            OutgoingMessage(
                to=tuple(recipients),
                cc=cc,
                subject=reply_subject(original.subject),
                body_text=body_text,
                in_reply_to_message_id=original.in_reply_to,
            )
        )
