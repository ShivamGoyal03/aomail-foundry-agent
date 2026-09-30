"""Agent tools exposed to the model.

Design rules enforced here:

* the model never chooses *whose* mailbox to touch — the principal comes from
  the request context, and the registry validates account ownership;
* tool results contain only :mod:`omniport_agent.models` data, never
  credentials, tokens or provider configuration;
* provider failures are returned as structured, scrubbed error payloads so the
  model can explain the problem instead of retrying blindly;
* ``send_draft`` requires both a pre-existing draft and an explicit human
  approval (``approval_mode="always_require"``).
"""

from __future__ import annotations

import functools
import inspect
from typing import Annotated, Any, Awaitable, Callable

from agent_framework import tool
from pydantic import Field

from .config import Settings, get_settings
from .drafts import DraftStore
from .errors import OmniPortAgentError, ValidationError
from .identity import Principal, current_principal
from .models import EmailAddress, OutgoingMessage, SearchCriteria
from .registry import AccountRegistry, default_registry

__all__ = ["EmailToolset"]

_MAX_RECIPIENTS = 25


def _parse_recipients(raw: list[str] | None, label: str) -> tuple[EmailAddress, ...]:
    from ._addressing import parse_recipient_list

    return parse_recipient_list(raw, label, _MAX_RECIPIENTS)


def _guard(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Convert known errors into structured, model-safe tool results."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except OmniPortAgentError as exc:
            return {"error": {"code": exc.code, "message": exc.message}}

    # ``*args, **kwargs`` would erase the parameter schema, and ``__wrapped__``
    # would make ``inspect.signature`` resolve to the *unbound* function and so
    # expose ``self`` as a tool parameter. Pin the signature explicitly instead:
    # it still includes ``self``, which ``inspect.signature`` strips again when
    # the tool is read off a bound method.
    wrapper.__signature__ = inspect.signature(func)  # type: ignore[attr-defined]
    del wrapper.__wrapped__
    return wrapper


class EmailToolset:
    """Builds the bound tool callables for one process.

    Tools are instance methods so they close over the registry, draft store and
    settings without using module-level globals, which keeps them testable.
    """

    def __init__(
        self,
        registry: AccountRegistry | None = None,
        *,
        drafts: DraftStore | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._registry = registry or default_registry()
        self._drafts = drafts or DraftStore()

    # -- helpers --------------------------------------------------------------

    @property
    def drafts(self) -> DraftStore:
        return self._drafts

    def _principal(self) -> Principal:
        return current_principal()

    # -- read tools -----------------------------------------------------------

    @_guard
    async def list_accounts(self) -> dict[str, Any]:
        """List the email accounts linked to the signed-in user.

        Always call this first: the returned ``account_id`` values are the only
        identifiers accepted by the other tools.
        """
        principal = self._principal()
        accounts = self._registry.accounts_for(principal)
        return {
            "accounts": [
                self._registry.connector(principal, account.account_id).account_info().model_dump(mode="json")
                for account in accounts
            ]
        }

    @_guard
    async def search_messages(
        self,
        account_id: Annotated[str, Field(description="Account id returned by list_accounts.")],
        query: Annotated[str, Field(description="Free-text search terms. Empty lists recent mail.")] = "",
        sender: Annotated[str | None, Field(description="Filter by sender address.")] = None,
        subject: Annotated[str | None, Field(description="Filter by subject text.")] = None,
        unread_only: Annotated[bool, Field(description="Only return unread messages.")] = False,
        folder: Annotated[str | None, Field(description="Folder or label to search in.")] = None,
        limit: Annotated[int, Field(description="Maximum messages to return (1-50).", ge=1, le=50)] = 20,
        page_token: Annotated[str | None, Field(description="Cursor from a previous call.")] = None,
    ) -> dict[str, Any]:
        """Search or list messages in one mailbox. Returns metadata, not bodies."""
        principal = self._principal()
        connector = self._registry.connector(principal, account_id)
        criteria = SearchCriteria(
            query=query,
            sender=sender,
            subject=subject,
            unread_only=unread_only,
            folder=folder,
            limit=min(limit, self._settings.max_page_size),
            page_token=page_token,
        )
        page = await connector.list_messages(criteria)
        return page.model_dump(mode="json")

    @_guard
    async def read_message(
        self,
        account_id: Annotated[str, Field(description="Account id returned by list_accounts.")],
        message_id: Annotated[str, Field(description="Message id returned by search_messages.")],
    ) -> dict[str, Any]:
        """Read one message, including its plain-text body and attachment names."""
        principal = self._principal()
        connector = self._registry.connector(principal, account_id)
        detail = await connector.get_message(message_id)
        return detail.model_dump(mode="json")

    @_guard
    async def collect_messages_for_summary(
        self,
        account_id: Annotated[str, Field(description="Account id returned by list_accounts.")],
        message_ids: Annotated[list[str], Field(description="Up to 10 message ids to fetch.")],
    ) -> dict[str, Any]:
        """Fetch several message bodies at once so they can be summarized.

        Use this before writing a summary for the user; summarize the returned
        text yourself rather than guessing at message contents.
        """
        if not message_ids:
            raise ValidationError("Provide at least one message id.")
        if len(message_ids) > 10:
            raise ValidationError("At most 10 message ids can be summarized in one call.")
        principal = self._principal()
        connector = self._registry.connector(principal, account_id)
        results: list[dict[str, Any]] = []
        for message_id in message_ids:
            try:
                detail = await connector.get_message(message_id)
            except OmniPortAgentError as exc:
                results.append({"message_id": message_id, "error": {"code": exc.code, "message": exc.message}})
                continue
            results.append(
                {
                    "message_id": detail.message_id,
                    "subject": detail.subject,
                    "sender": detail.sender.formatted() if detail.sender else None,
                    "received_at": detail.received_at.isoformat() if detail.received_at else None,
                    "body_text": detail.body_text,
                    "body_truncated": detail.body_truncated,
                }
            )
        return {"messages": results}

    # -- draft tools ----------------------------------------------------------

    @_guard
    async def draft_email(
        self,
        account_id: Annotated[str, Field(description="Account id to send from.")],
        to: Annotated[list[str], Field(description="Recipient email addresses.")],
        subject: Annotated[str, Field(description="Subject line.")],
        body: Annotated[str, Field(description="Plain-text body of the message.")],
        cc: Annotated[list[str] | None, Field(description="Carbon-copy addresses.")] = None,
        bcc: Annotated[list[str] | None, Field(description="Blind carbon-copy addresses.")] = None,
    ) -> dict[str, Any]:
        """Prepare an email and return a draft for the user to review.

        Nothing is sent. Show the returned draft to the user verbatim and only
        call send_draft once they have explicitly approved it.
        """
        principal = self._principal()
        self._registry.resolve(principal, account_id)
        message = OutgoingMessage(
            to=_parse_recipients(to, "to"),
            cc=_parse_recipients(cc, "cc"),
            bcc=_parse_recipients(bcc, "bcc"),
            subject=subject,
            body_text=body,
        )
        draft = self._drafts.create(
            principal_id=principal.principal_id, account_id=account_id, message=message
        )
        return {"draft": draft.preview(), "status": "awaiting_user_approval"}

    @_guard
    async def draft_reply(
        self,
        account_id: Annotated[str, Field(description="Account id to reply from.")],
        message_id: Annotated[str, Field(description="Message id being replied to.")],
        body: Annotated[str, Field(description="Plain-text body of the reply.")],
        reply_all: Annotated[bool, Field(description="Include all original recipients.")] = False,
    ) -> dict[str, Any]:
        """Prepare a reply and return a draft for the user to review.

        Nothing is sent. Call send_draft only after explicit user approval.
        """
        principal = self._principal()
        connector = self._registry.connector(principal, account_id)
        original = await connector.get_message(message_id)
        recipients = [original.sender] if original.sender else []
        if not recipients:
            raise ValidationError("The original message has no sender to reply to.")
        cc: tuple[EmailAddress, ...] = ()
        if reply_all:
            own = connector.email.lower()
            recipients += [addr for addr in original.recipients if addr.address.lower() != own]
            cc = tuple(addr for addr in original.cc if addr.address.lower() != own)

        from .connectors._common import reply_subject

        message = OutgoingMessage(
            to=tuple(recipients),
            cc=cc,
            subject=reply_subject(original.subject),
            body_text=body,
            in_reply_to_message_id=original.in_reply_to,
        )
        draft = self._drafts.create(
            principal_id=principal.principal_id,
            account_id=account_id,
            message=message,
            reply_to_message_id=message_id,
            reply_all=reply_all,
        )
        return {"draft": draft.preview(), "status": "awaiting_user_approval"}

    @_guard
    async def discard_draft(
        self,
        draft_id: Annotated[str, Field(description="Draft id returned by draft_email or draft_reply.")],
    ) -> dict[str, Any]:
        """Delete a pending draft that the user rejected or changed their mind about."""
        principal = self._principal()
        self._drafts.discard(principal.principal_id, draft_id)
        return {"draft_id": draft_id, "status": "discarded"}

    # -- write tool (human approval required) ---------------------------------

    @_guard
    async def send_draft(
        self,
        draft_id: Annotated[str, Field(description="Draft id returned by draft_email or draft_reply.")],
    ) -> dict[str, Any]:
        """Send a previously drafted email or reply. Requires human approval.

        This is the only tool that transmits mail. It refuses any draft id that
        was not produced by draft_email or draft_reply in this session, so a
        message can never be sent straight from a natural-language request.
        """
        principal = self._principal()
        draft = self._drafts.get(principal.principal_id, draft_id)
        connector = self._registry.connector(principal, draft.account_id)
        if draft.reply_to_message_id:
            result = await connector.reply_message(
                draft.reply_to_message_id, draft.message.body_text, draft.reply_all
            )
        else:
            result = await connector.send_message(draft.message)
        # Only drop the draft once the provider accepted it, so a transient
        # failure can be retried without re-approval of a different message.
        self._drafts.consume(principal.principal_id, draft_id)
        return {"status": "sent", "result": result.model_dump(mode="json")}

    # -- assembly -------------------------------------------------------------

    def as_tools(self) -> list[Any]:
        """Return the tool objects to hand to :class:`agent_framework.Agent`."""
        return [
            tool(self.list_accounts, name="list_accounts", approval_mode="never_require"),
            tool(self.search_messages, name="search_messages", approval_mode="never_require"),
            tool(self.read_message, name="read_message", approval_mode="never_require"),
            tool(
                self.collect_messages_for_summary,
                name="collect_messages_for_summary",
                approval_mode="never_require",
            ),
            tool(self.draft_email, name="draft_email", approval_mode="never_require"),
            tool(self.draft_reply, name="draft_reply", approval_mode="never_require"),
            tool(self.discard_draft, name="discard_draft", approval_mode="never_require"),
            # The only outbound-mail tool: always gated by a human approval.
            tool(self.send_draft, name="send_draft", approval_mode="always_require"),
        ]
