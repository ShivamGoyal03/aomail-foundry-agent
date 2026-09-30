"""Gmail connector built directly on the Gmail REST API v1.

Scopes required: ``https://www.googleapis.com/auth/gmail.modify`` for reading
and ``https://www.googleapis.com/auth/gmail.send`` for sending.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from ..config import GmailCredentials
from ..errors import NotFoundError, ProviderError, ValidationError
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
from ._http import OAuth2TokenCache, new_client, request_json
from .base import EmailConnector

__all__ = ["GmailConnector"]

_PROVIDER = "Gmail"


def _b64url_decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        return b""


class GmailConnector(EmailConnector):
    provider = "gmail"

    def __init__(
        self,
        account_id: str,
        email: str,
        credentials: GmailCredentials,
        *,
        display_name: str | None = None,
        timeout: float = 30.0,
        max_body_characters: int = 8000,
        client: Any | None = None,
    ) -> None:
        super().__init__(account_id, email, display_name)
        self._credentials = credentials
        self._max_body_characters = max_body_characters
        self._client = client or new_client(timeout)
        self._tokens = OAuth2TokenCache()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_threads=True,
            supports_labels=True,
            supports_server_side_query=True,
            max_page_size=100,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- auth -----------------------------------------------------------------

    async def _access_token(self) -> str:
        cached = self._tokens.get()
        if cached:
            return cached
        payload = await request_json(
            self._client,
            "POST",
            self._credentials.token_uri,
            provider=_PROVIDER,
            data={
                "client_id": self._credentials.client_id,
                "client_secret": self._credentials.client_secret.get_secret_value(),
                "refresh_token": self._credentials.refresh_token.get_secret_value(),
                "grant_type": "refresh_token",
            },
        )
        token = (payload or {}).get("access_token")
        if not token:
            raise ProviderError("Gmail did not return an access token for this mailbox.")
        self._tokens.set(token, float((payload or {}).get("expires_in", 3600)))
        return str(token)

    async def _headers(self) -> dict[str, str]:
        token = await self._access_token()
        return {"Authorization": "Bearer " + token}

    async def _api(self, method: str, path: str, **kwargs: Any) -> Any:
        return await request_json(
            self._client,
            method,
            f"{self._credentials.api_base_url}{path}",
            provider=_PROVIDER,
            headers=await self._headers(),
            **kwargs,
        )

    # -- reads ----------------------------------------------------------------

    @staticmethod
    def _build_query(criteria: SearchCriteria) -> str:
        parts: list[str] = []
        if criteria.query:
            parts.append(criteria.query)
        if criteria.sender:
            parts.append(f"from:{criteria.sender}")
        if criteria.recipient:
            parts.append(f"to:{criteria.recipient}")
        if criteria.subject:
            parts.append(f"subject:{criteria.subject}")
        if criteria.unread_only:
            parts.append("is:unread")
        if criteria.folder:
            parts.append(f"in:{criteria.folder}")
        return " ".join(parts)

    async def list_messages(self, criteria: SearchCriteria) -> MessagePage:
        limit = max(1, min(criteria.limit, self.capabilities.max_page_size))
        params: dict[str, Any] = {"maxResults": limit}
        query = self._build_query(criteria)
        if query:
            params["q"] = query
        if criteria.page_token:
            params["pageToken"] = criteria.page_token

        listing = await self._api("GET", "/users/me/messages", params=params) or {}
        summaries: list[MessageSummary] = []
        for stub in listing.get("messages", [])[:limit]:
            message_id = stub.get("id")
            if not message_id:
                continue
            payload = await self._api(
                "GET",
                f"/users/me/messages/{message_id}",
                params={
                    "format": "metadata",
                    "metadataHeaders": ["From", "To", "Cc", "Subject", "Date"],
                },
            )
            summaries.append(self._to_summary(payload or {}))
        return MessagePage(messages=tuple(summaries), next_page_token=listing.get("nextPageToken"))

    async def get_message(self, message_id: str) -> MessageDetail:
        if not message_id:
            raise ValidationError("A Gmail message id is required.")
        payload = await self._api("GET", f"/users/me/messages/{message_id}", params={"format": "full"})
        if not payload:
            raise NotFoundError(f"Gmail message '{message_id}' was not found.")
        summary = self._to_summary(payload)
        body, attachments = self._extract_body(payload.get("payload") or {})
        body_text, truncated = truncate_body(body, self._max_body_characters)
        headers = self._header_map(payload)
        references = tuple(headers.get("references", "").split()) if headers.get("references") else ()
        return MessageDetail(
            **summary.model_dump(),
            body_text=body_text,
            body_truncated=truncated,
            attachments=attachments,
            in_reply_to=headers.get("message-id"),
            references=references,
        )

    @staticmethod
    def _header_map(payload: dict[str, Any]) -> dict[str, str]:
        headers = (payload.get("payload") or {}).get("headers") or []
        return {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in headers}

    def _to_summary(self, payload: dict[str, Any]) -> MessageSummary:
        headers = self._header_map(payload)
        label_ids = tuple(payload.get("labelIds") or ())
        return MessageSummary(
            account_id=self.account_id,
            provider=self.provider,
            message_id=str(payload.get("id", "")),
            thread_id=payload.get("threadId"),
            subject=headers.get("subject", ""),
            sender=parse_address(headers.get("from")),
            recipients=parse_addresses(headers.get("to")),
            cc=parse_addresses(headers.get("cc")),
            received_at=parse_date(headers.get("date")),
            snippet=str(payload.get("snippet", "")),
            is_read="UNREAD" not in label_ids,
            has_attachments=self._has_attachments(payload.get("payload") or {}),
            labels=label_ids,
        )

    @classmethod
    def _has_attachments(cls, part: dict[str, Any]) -> bool:
        if part.get("filename"):
            return True
        return any(cls._has_attachments(child) for child in part.get("parts") or ())

    def _extract_body(self, part: dict[str, Any]) -> tuple[str, tuple[AttachmentInfo, ...]]:
        text_parts: list[str] = []
        html_parts: list[str] = []
        attachments: list[AttachmentInfo] = []

        def walk(node: dict[str, Any]) -> None:
            filename = node.get("filename")
            body = node.get("body") or {}
            mime_type = node.get("mimeType")
            if filename:
                attachments.append(
                    AttachmentInfo(
                        attachment_id=body.get("attachmentId"),
                        filename=filename,
                        mime_type=mime_type,
                        size_bytes=body.get("size"),
                    )
                )
            elif body.get("data"):
                decoded = _b64url_decode(body["data"]).decode("utf-8", errors="replace")
                if mime_type == "text/plain":
                    text_parts.append(decoded)
                elif mime_type == "text/html":
                    html_parts.append(decoded)
            for child in node.get("parts") or ():
                walk(child)

        walk(part)
        body_text = "\n".join(text_parts).strip()
        if not body_text and html_parts:
            body_text = html_to_text("\n".join(html_parts))
        return body_text, tuple(attachments)

    # -- writes ---------------------------------------------------------------

    async def send_message(self, message: OutgoingMessage) -> SendResult:
        if not message.to:
            raise ValidationError("At least one recipient is required.")
        mime = build_mime(message, self.email)
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
        payload = await self._api("POST", "/users/me/messages/send", json={"raw": raw})
        return SendResult(
            account_id=self.account_id,
            provider=self.provider,
            message_id=(payload or {}).get("id"),
            thread_id=(payload or {}).get("threadId"),
            accepted_recipients=tuple(addr.address for addr in (*message.to, *message.cc, *message.bcc)),
        )

    async def reply_message(self, message_id: str, body_text: str, reply_all: bool = False) -> SendResult:
        original = await self.get_message(message_id)
        recipients: list[EmailAddress] = [original.sender] if original.sender else []
        cc: tuple[EmailAddress, ...] = ()
        if reply_all:
            own = self.email.lower()
            recipients += [addr for addr in original.recipients if addr.address.lower() != own]
            cc = tuple(addr for addr in original.cc if addr.address.lower() != own)
        if not recipients:
            raise ValidationError("The original message has no sender to reply to.")

        parent_id = original.in_reply_to
        references = (*original.references, parent_id) if parent_id else original.references
        mime = build_mime(
            OutgoingMessage(
                to=tuple(recipients),
                cc=cc,
                subject=reply_subject(original.subject),
                body_text=body_text,
                in_reply_to_message_id=parent_id,
            ),
            self.email,
            references=references,
        )
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
        body: dict[str, Any] = {"raw": raw}
        if original.thread_id:
            body["threadId"] = original.thread_id
        payload = await self._api("POST", "/users/me/messages/send", json=body)
        return SendResult(
            account_id=self.account_id,
            provider=self.provider,
            message_id=(payload or {}).get("id"),
            thread_id=(payload or {}).get("threadId") or original.thread_id,
            accepted_recipients=tuple(addr.address for addr in (*recipients, *cc)),
        )
