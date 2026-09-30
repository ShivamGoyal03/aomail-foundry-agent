"""Safety, approval and identity-boundary behaviour of the agent tools."""

from __future__ import annotations

import pytest

from omniport_agent.errors import AccountNotFoundError, PermissionDeniedError
from omniport_agent.identity import Principal, current_principal, use_principal
from omniport_agent.registry import AccountRegistry
from omniport_agent.tools import EmailToolset

from .conftest import OTHER_PRINCIPAL, PRINCIPAL


async def test_tools_require_a_bound_principal(toolset: EmailToolset) -> None:
    result = await toolset.list_accounts()
    assert result["error"]["code"] == "permission_denied"


async def test_list_accounts_only_returns_the_callers_mailboxes(toolset: EmailToolset) -> None:
    with use_principal(PRINCIPAL):
        result = await toolset.list_accounts()
    ids = [account["account_id"] for account in result["accounts"]]
    assert ids == ["mock-inbox"]
    assert all("credentials" not in account for account in result["accounts"])


async def test_other_users_mailbox_is_indistinguishable_from_a_missing_one(
    toolset: EmailToolset,
) -> None:
    with use_principal(PRINCIPAL):
        foreign = await toolset.search_messages(account_id="someone-else")
        missing = await toolset.search_messages(account_id="no-such-account")
    # Same error code and same wording apart from the echoed id, so the model
    # cannot tell "owned by someone else" from "does not exist".
    assert foreign["error"]["code"] == missing["error"]["code"] == "account_not_found"
    assert foreign["error"]["message"].replace("someone-else", "X") == missing["error"][
        "message"
    ].replace("no-such-account", "X")


async def test_registry_rejects_cross_principal_access(registry: AccountRegistry) -> None:
    with pytest.raises(AccountNotFoundError):
        registry.resolve(PRINCIPAL, "someone-else")
    # The owner can still reach it.
    assert registry.resolve(OTHER_PRINCIPAL, "someone-else").email == "other@example.com"


async def test_search_and_read_round_trip(toolset: EmailToolset) -> None:
    with use_principal(PRINCIPAL):
        page = await toolset.search_messages(account_id="mock-inbox", query="report")
        assert [m["message_id"] for m in page["messages"]] == ["m-1"]

        detail = await toolset.read_message(account_id="mock-inbox", message_id="m-1")
        assert "Q4 report" in detail["body_text"]


async def test_read_message_reports_unknown_ids_safely(toolset: EmailToolset) -> None:
    with use_principal(PRINCIPAL):
        result = await toolset.read_message(account_id="mock-inbox", message_id="nope")
    assert result["error"]["code"] == "not_found"


async def test_collect_messages_for_summary_bounds_the_batch(toolset: EmailToolset) -> None:
    with use_principal(PRINCIPAL):
        empty = await toolset.collect_messages_for_summary(account_id="mock-inbox", message_ids=[])
        too_many = await toolset.collect_messages_for_summary(
            account_id="mock-inbox", message_ids=[f"m-{i}" for i in range(11)]
        )
        ok = await toolset.collect_messages_for_summary(
            account_id="mock-inbox", message_ids=["m-1", "missing"]
        )
    assert empty["error"]["code"] == "validation_error"
    assert too_many["error"]["code"] == "validation_error"
    assert ok["messages"][0]["subject"] == "Quarterly report is ready"
    assert ok["messages"][1]["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- #
# Approval flow
# --------------------------------------------------------------------------- #


async def test_send_tool_is_marked_always_require_approval(toolset: EmailToolset) -> None:
    tools = {t.name: t for t in toolset.as_tools()}
    assert tools["send_draft"].approval_mode == "always_require"
    for name in ("list_accounts", "search_messages", "read_message", "draft_email", "draft_reply"):
        assert tools[name].approval_mode == "never_require"
    assert "send_email" not in tools, "there must be no single-shot send tool"


async def test_send_draft_refuses_a_fabricated_draft_id(toolset: EmailToolset) -> None:
    with use_principal(PRINCIPAL):
        result = await toolset.send_draft(draft_id="draft_made_up")
    assert result["error"]["code"] == "approval_required"
    assert len(toolset.drafts) == 0


async def test_draft_then_send_sends_exactly_once(toolset: EmailToolset, registry) -> None:
    with use_principal(PRINCIPAL):
        drafted = await toolset.draft_email(
            account_id="mock-inbox",
            to=["dana@example.com"],
            subject="Report follow-up",
            body="Thanks, looks good.",
        )
        assert drafted["status"] == "awaiting_user_approval"
        connector = registry.connector(PRINCIPAL, "mock-inbox")
        assert connector.sent == [], "drafting must not send anything"

        draft_id = drafted["draft"]["draft_id"]
        sent = await toolset.send_draft(draft_id=draft_id)
        assert sent["status"] == "sent"
        assert len(connector.sent) == 1

        replayed = await toolset.send_draft(draft_id=draft_id)
    assert replayed["error"]["code"] == "approval_required"
    assert len(connector.sent) == 1, "a draft must not be sendable twice"


async def test_failed_send_keeps_the_draft_for_retry(toolset: EmailToolset, registry) -> None:
    from omniport_agent.errors import ProviderError

    connector = registry.connector(PRINCIPAL, "mock-inbox")

    async def boom(_message):
        raise ProviderError("The provider is unavailable.")

    connector.send_message = boom  # type: ignore[method-assign]

    with use_principal(PRINCIPAL):
        drafted = await toolset.draft_email(
            account_id="mock-inbox", to=["dana@example.com"], subject="Hi", body="Body"
        )
        failed = await toolset.send_draft(draft_id=drafted["draft"]["draft_id"])
    assert failed["error"]["code"] == "provider_error"
    assert len(toolset.drafts) == 1


async def test_a_draft_cannot_be_sent_by_another_principal(toolset: EmailToolset) -> None:
    with use_principal(PRINCIPAL):
        drafted = await toolset.draft_email(
            account_id="mock-inbox", to=["dana@example.com"], subject="Hi", body="Body"
        )
    with use_principal(OTHER_PRINCIPAL):
        result = await toolset.send_draft(draft_id=drafted["draft"]["draft_id"])
    assert result["error"]["code"] == "approval_required"


async def test_draft_reply_quotes_the_right_recipients(toolset: EmailToolset, registry) -> None:
    with use_principal(PRINCIPAL):
        drafted = await toolset.draft_reply(
            account_id="mock-inbox", message_id="m-1", body="Sounds good."
        )
        draft = drafted["draft"]
        assert draft["to"] == ["Dana Lee <dana@example.com>"]
        assert draft["subject"] == "Re: Quarterly report is ready"
        assert draft["reply_to_message_id"] == "m-1"

        await toolset.send_draft(draft_id=draft["draft_id"])
    connector = registry.connector(PRINCIPAL, "mock-inbox")
    assert connector.sent[0].subject == "Re: Quarterly report is ready"


async def test_discard_draft_prevents_sending(toolset: EmailToolset) -> None:
    with use_principal(PRINCIPAL):
        drafted = await toolset.draft_email(
            account_id="mock-inbox", to=["dana@example.com"], subject="Hi", body="Body"
        )
        draft_id = drafted["draft"]["draft_id"]
        assert (await toolset.discard_draft(draft_id=draft_id))["status"] == "discarded"
        result = await toolset.send_draft(draft_id=draft_id)
    assert result["error"]["code"] == "approval_required"


@pytest.mark.parametrize(
    "bad",
    [
        "not-an-address",
        "dana@example.com\nBcc: attacker@evil.example",
        "<script>@example.com",
        "dana@example",
    ],
)
async def test_draft_email_rejects_malformed_recipients(toolset: EmailToolset, bad: str) -> None:
    with use_principal(PRINCIPAL):
        result = await toolset.draft_email(
            account_id="mock-inbox", to=[bad], subject="Hi", body="Body"
        )
    assert result["error"]["code"] == "validation_error"


async def test_draft_email_caps_recipient_count(toolset: EmailToolset) -> None:
    with use_principal(PRINCIPAL):
        result = await toolset.draft_email(
            account_id="mock-inbox",
            to=[f"user{i}@example.com" for i in range(26)],
            subject="Hi",
            body="Body",
        )
    assert result["error"]["code"] == "validation_error"


async def test_draft_email_deduplicates_recipients(toolset: EmailToolset) -> None:
    with use_principal(PRINCIPAL):
        result = await toolset.draft_email(
            account_id="mock-inbox",
            to=["dana@example.com", "DANA@example.com"],
            subject="Hi",
            body="Body",
        )
    assert result["draft"]["to"] == ["dana@example.com"]


async def test_principal_context_is_restored(toolset: EmailToolset) -> None:
    with use_principal(Principal(principal_id="a")):
        assert current_principal().principal_id == "a"
        with use_principal(Principal(principal_id="b")):
            assert current_principal().principal_id == "b"
        assert current_principal().principal_id == "a"
    with pytest.raises(PermissionDeniedError):
        current_principal()


def test_tool_schemas_expose_the_right_parameters(toolset: EmailToolset) -> None:
    tools = {t.name: t for t in toolset.as_tools()}
    expected = {
        "list_accounts": set(),
        "search_messages": {
            "account_id", "query", "sender", "subject", "unread_only", "folder", "limit", "page_token",
        },
        "read_message": {"account_id", "message_id"},
        "collect_messages_for_summary": {"account_id", "message_ids"},
        "draft_email": {"account_id", "to", "subject", "body", "cc", "bcc"},
        "draft_reply": {"account_id", "message_id", "body", "reply_all"},
        "discard_draft": {"draft_id"},
        "send_draft": {"draft_id"},
    }
    for name, params in expected.items():
        properties = set(tools[name].parameters().get("properties") or {})
        assert properties == params, name
        # ``self`` must never become a model-controlled argument.
        assert "self" not in properties
