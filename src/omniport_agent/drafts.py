"""Server-side draft store backing the two-step approval flow.

The model can never send an email from a single natural-language instruction:
it must first create a draft (which the user sees in full), and then call the
send tool with that draft's id. The send tool is additionally marked
``approval_mode="always_require"`` so the hosting layer raises a human approval
request before the function body ever runs.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from .errors import ApprovalRequiredError, NotFoundError
from .models import OutgoingMessage

__all__ = ["Draft", "DraftStore"]


@dataclass(frozen=True)
class Draft:
    """A proposed outbound message awaiting human approval."""

    draft_id: str
    principal_id: str
    account_id: str
    message: OutgoingMessage
    reply_to_message_id: str | None = None
    reply_all: bool = False
    created_at: float = field(default_factory=time.monotonic)

    def preview(self) -> dict[str, object]:
        """Model-visible summary of what will be sent if approved."""
        return {
            "draft_id": self.draft_id,
            "account_id": self.account_id,
            "to": [addr.formatted() for addr in self.message.to],
            "cc": [addr.formatted() for addr in self.message.cc],
            "bcc": [addr.formatted() for addr in self.message.bcc],
            "subject": self.message.subject,
            "body_text": self.message.body_text,
            "reply_to_message_id": self.reply_to_message_id,
            "reply_all": self.reply_all,
        }


class DraftStore:
    """Bounded, TTL-limited in-memory store of pending drafts.

    Replace with a durable store (for example ``FoundryFunctionApprovalStore``
    or a database) if drafts must survive a container restart.
    """

    def __init__(self, *, ttl_seconds: float = 3600.0, max_drafts: int = 200) -> None:
        self._ttl = ttl_seconds
        self._max = max_drafts
        self._drafts: dict[str, Draft] = {}

    def _evict(self) -> None:
        now = time.monotonic()
        expired = [key for key, draft in self._drafts.items() if now - draft.created_at > self._ttl]
        for key in expired:
            del self._drafts[key]
        while len(self._drafts) > self._max:
            oldest = min(self._drafts, key=lambda key: self._drafts[key].created_at)
            del self._drafts[oldest]

    def create(
        self,
        *,
        principal_id: str,
        account_id: str,
        message: OutgoingMessage,
        reply_to_message_id: str | None = None,
        reply_all: bool = False,
    ) -> Draft:
        self._evict()
        draft = Draft(
            draft_id=f"draft_{secrets.token_urlsafe(12)}",
            principal_id=principal_id,
            account_id=account_id,
            message=message,
            reply_to_message_id=reply_to_message_id,
            reply_all=reply_all,
        )
        self._drafts[draft.draft_id] = draft
        return draft

    def get(self, principal_id: str, draft_id: str) -> Draft:
        """Return a draft owned by ``principal_id``.

        Raises:
            ApprovalRequiredError: if the id is unknown, which is the case when
                the model invents one instead of drafting first.
        """
        self._evict()
        draft = self._drafts.get(draft_id)
        if draft is None or draft.principal_id != principal_id:
            raise ApprovalRequiredError(
                "That draft id does not exist or has expired. Create the message with "
                "draft_email or draft_reply, show it to the user, and send it only after "
                "they approve it."
            )
        return draft

    def discard(self, principal_id: str, draft_id: str) -> None:
        draft = self._drafts.get(draft_id)
        if draft is None or draft.principal_id != principal_id:
            raise NotFoundError("That draft id does not exist or has already been discarded.")
        del self._drafts[draft_id]

    def consume(self, principal_id: str, draft_id: str) -> Draft:
        """Fetch and remove a draft so it can never be sent twice."""
        draft = self.get(principal_id, draft_id)
        self._drafts.pop(draft_id, None)
        return draft

    def __len__(self) -> int:
        return len(self._drafts)
