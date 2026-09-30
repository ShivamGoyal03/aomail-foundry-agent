"""Microsoft 365 / Outlook connector built on Microsoft Graph v1.0.

Permissions required: ``Mail.ReadWrite`` and ``Mail.Send`` (delegated, or the
application equivalents scoped to a specific mailbox).
"""

from __future__ import annotations

from typing import Any

from ..config import MicrosoftCredentials
from ..errors import ConfigurationError, NotFoundError, ProviderError, ValidationError
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
from ._common import html_to_text, parse_date, truncate_body
from ._http import OAuth2TokenCache, new_client, request_json
from .base import EmailConnector

__all__ = ["MicrosoftConnector"]

_PROVIDER = "Microsoft Graph"

_SUMMARY_FIELDS = (
    "id,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,"
    "bodyPreview,isRead,hasAttachments,parentFolderId"
)


def _to_address(entry: Any) -> EmailAddress | None:
    if not isinstance(entry, dict):
        return None
    inner = entry.get("emailAddress") if "emailAddress" in entry else entry
    if not isinstance(inner, dict):
        return None
    address = inner.get("address")
    if not address:
        return None
    return EmailAddress(address=str(address), name=inner.get("name") or None)


def _to_addresses(entries: Any) -> tuple[EmailAddress, ...]:
    if not isinstance(entries, list):
        return ()
    parsed = (_to_address(entry) for entry in entries)
    return tuple(addr for addr in parsed if addr is not None)


def _from_address(addr: EmailAddress) -> dict[str, Any]:
    payload: dict[str, Any] = {"address": addr.address}
    if addr.name:
        payload["name"] = addr.name
    return {"emailAddress": payload}


class MicrosoftConnector(EmailConnector):
    provider = "microsoft"

    def __init__(
        self,
        account_id: str,
        email: str,
        credentials: MicrosoftCredentials,
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
            supports_labels=False,
            supports_server_side_query=True,
            max_page_size=100,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- auth -----------------------------------------------------------------

    @property
    def _mailbox_path(self) -> str:
        """Graph resource prefix for the mailbox this connector acts on."""
        if self._credentials.refresh_token is not None:
            return "/me"
        upn = self._credentials.mailbox_upn or self.email
        if not upn:
            raise ConfigurationError(
                "Application-only Microsoft credentials require 'mailbox_upn' to identify the mailbox."
            )
        return f"/users/{upn}"

    async def _access_token(self) -> str:
        cached = self._tokens.get()
        if cached:
            return cached
        form: dict[str, str] = {
            "client_id": self._credentials.client_id,
            "client_secret": self._credentials.client_secret.get_secret_value(),
            "scope": self._credentials.scope,
        }
        if self._credentials.refresh_token is not None:
            form["grant_type"] = "refresh_token"
            form["refresh_token"] = self._credentials.refresh_token.get_secret_value()
        else:
            form["grant_type"] = "client_credentials"

        payload = await request_json(
            self._client,
            "POST",
            f"{self._credentials.authority}/{self._credentials.tenant_id}/oauth2/v2.0/token",
            provider=_PROVIDER,
            data=form,
        )
        token = (payload or {}).get("access_token")
        if not token:
            raise ProviderError("Microsoft Entra ID did not return an access token for this mailbox.")
        self._tokens.set(token, float((payload or {}).get("expires_in", 3600)))
        return str(token)

    async def _headers(self) -> dict[str, str]:
        token = await self._access_token()
        return {"Authorization": "Bearer " + token}

    async def _api(self, method: str, path_or_url: str, **kwargs: Any) -> Any:
        url = (
            path_or_url
            if path_or_url.startswith("http")
            else f"{self._credentials.api_base_url}{path_or_url}"
        )
        return await request_json(
            self._client, method, url, provider=_PROVIDER, headers=await self._headers(), **kwargs
        )

    # -- reads ----------------------------------------------------------------

    @staticmethod
    def _build_filter(criteria: SearchCriteria) -> str | None:
        clauses: list[str] = []
        if criteria.unread_only:
            clauses.append("isRead eq false")
        if criteria.sender:
            escaped = criteria.sender.replace("'", "''")
            clauses.append(f"from/emailAddress/address eq '{escaped}'")
        return " and ".join(clauses) if clauses else None

    @staticmethod
    def _build_search(criteria: SearchCriteria) -> str | None:
        terms = [term for term in (criteria.query, criteria.subject, criteria.recipient) if term]
        return " ".join(terms) if terms else None

    async def list_messages(self, criteria: SearchCriteria) -> MessagePage:
        limit = max(1, min(criteria.limit, self.capabilities.max_page_size))
        if criteria.page_token:
            payload = await self._api("GET", criteria.page_token) or {}
        else:
            base = self._mailbox_path
            path = f"{base}/mailFolders/{criteria.folder}/messages" if criteria.folder else f"{base}/messages"
            params: dict[str, Any] = {"$top": limit, "$select": _SUMMARY_FIELDS}
            search = self._build_search(criteria)
            if search:
                # $search and $orderby are mutually exclusive in Graph.
                params["$search"] = f'"{search}"'
            else:
                params["$orderby"] = "receivedDateTime desc"
            odata_filter = self._build_filter(criteria)
            if odata_filter:
                params["$filter"] = odata_filter
            payload = await self._api("GET", path, params=params) or {}

        messages = tuple(self._to_summary(item) for item in payload.get("value", [])[:limit])
        return MessagePage(messages=messages, next_page_token=payload.get("@odata.nextLink"))

    async def get_message(self, message_id: str) -> MessageDetail:
        if not message_id:
            raise ValidationError("A Microsoft Graph message id is required.")
        payload = await self._api(
            "GET",
            f"{self._mailbox_path}/messages/{message_id}",
            params={"$select": f"{_SUMMARY_FIELDS},body,internetMessageId"},
        )
        if not payload:
            raise NotFoundError(f"Microsoft Graph message '{message_id}' was not found.")

        body = payload.get("body") or {}
        raw_body = str(body.get("content") or "")
        if str(body.get("contentType", "")).lower() == "html":
            raw_body = html_to_text(raw_body)
        body_text, truncated = truncate_body(raw_body, self._max_body_characters)

        attachments: tuple[AttachmentInfo, ...] = ()
        if payload.get("hasAttachments"):
            listing = await self._api(
                "GET",
                f"{self._mailbox_path}/messages/{message_id}/attachments",
                params={"$select": "id,name,contentType,size"},
            ) or {}
            attachments = tuple(
                AttachmentInfo(
                    attachment_id=item.get("id"),
                    filename=str(item.get("name") or "attachment"),
                    mime_type=item.get("contentType"),
                    size_bytes=item.get("size"),
                )
                for item in listing.get("value", [])
            )

        return MessageDetail(
            **self._to_summary(payload).model_dump(),
            body_text=body_text,
            body_truncated=truncated,
            attachments=attachments,
            in_reply_to=payload.get("internetMessageId"),
        )

    def _to_summary(self, payload: dict[str, Any]) -> MessageSummary:
        return MessageSummary(
            account_id=self.account_id,
            provider=self.provider,
            message_id=str(payload.get("id", "")),
            thread_id=payload.get("conversationId"),
            subject=str(payload.get("subject") or ""),
            sender=_to_address(payload.get("from")),
            recipients=_to_addresses(payload.get("toRecipients")),
            cc=_to_addresses(payload.get("ccRecipients")),
            received_at=parse_date(payload.get("receivedDateTime")),
            snippet=str(payload.get("bodyPreview") or ""),
            is_read=bool(payload.get("isRead", False)),
            has_attachments=bool(payload.get("hasAttachments", False)),
            folder=payload.get("parentFolderId"),
        )

    # -- writes ---------------------------------------------------------------

    async def send_message(self, message: OutgoingMessage) -> SendResult:
        if not message.to:
            raise ValidationError("At least one recipient is required.")
        body: dict[str, Any] = {
            "message": {
                "subject": message.subject,
                "body": {"contentType": "Text", "content": message.body_text},
                "toRecipients": [_from_address(addr) for addr in message.to],
                "ccRecipients": [_from_address(addr) for addr in message.cc],
                "bccRecipients": [_from_address(addr) for addr in message.bcc],
            },
            "saveToSentItems": True,
        }
        await self._api("POST", f"{self._mailbox_path}/sendMail", json=body)
        return SendResult(
            account_id=self.account_id,
            provider=self.provider,
            accepted_recipients=tuple(addr.address for addr in (*message.to, *message.cc, *message.bcc)),
        )

    async def reply_message(self, message_id: str, body_text: str, reply_all: bool = False) -> SendResult:
        if not message_id:
            raise ValidationError("A Microsoft Graph message id is required.")
        action = "replyAll" if reply_all else "reply"
        await self._api(
            "POST",
            f"{self._mailbox_path}/messages/{message_id}/{action}",
            json={"comment": body_text},
        )
        return SendResult(account_id=self.account_id, provider=self.provider, message_id=message_id)
