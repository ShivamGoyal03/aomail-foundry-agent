"""Connector normalization tests, one mocked operation per provider family."""

from __future__ import annotations

import base64
import email
from datetime import UTC

import httpx
import pytest

from omniport_agent.config import GmailCredentials, ImapCredentials, MicrosoftCredentials
from omniport_agent.connectors.gmail import GmailConnector
from omniport_agent.connectors.imap_smtp import ImapConnector
from omniport_agent.connectors.microsoft import MicrosoftConnector
from omniport_agent.connectors.mock import MockConnector
from omniport_agent.errors import (
    AuthenticationError,
    NotFoundError,
    PermissionDeniedError,
    ProviderTimeoutError,
    ValidationError,
)
from omniport_agent.models import EmailAddress, OutgoingMessage, SearchCriteria

# --------------------------------------------------------------------------- #
# Gmail
# --------------------------------------------------------------------------- #

GMAIL_CREDENTIALS = GmailCredentials(
    client_id="client-id",
    client_secret="client-secret",
    refresh_token="refresh-token",
)

_GMAIL_BODY = base64.urlsafe_b64encode(b"Hello from Gmail.\nRegards, Dana").decode().rstrip("=")

_GMAIL_MESSAGE = {
    "id": "g1",
    "threadId": "t1",
    "snippet": "Hello from Gmail.",
    "labelIds": ["INBOX", "UNREAD"],
    "payload": {
        "mimeType": "multipart/mixed",
        "headers": [
            {"name": "From", "value": "Dana Lee <dana@example.com>"},
            {"name": "To", "value": "user@example.com"},
            {"name": "Cc", "value": "cc@example.com"},
            {"name": "Subject", "value": "Quarterly report"},
            {"name": "Date", "value": "Mon, 05 Jan 2026 09:00:00 +0000"},
            {"name": "Message-ID", "value": "<g1@mail.example>"},
        ],
        "parts": [
            {"mimeType": "text/plain", "filename": "", "body": {"data": _GMAIL_BODY, "size": 30}},
            {
                "mimeType": "application/pdf",
                "filename": "report.pdf",
                "body": {"attachmentId": "a1", "size": 1024},
            },
        ],
    },
}


def _gmail_transport(sent: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        url = str(request.url)
        if "oauth2.googleapis.com" in url:
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if url.endswith("/users/me/messages/send"):
            return httpx.Response(200, json={"id": "sent-1", "threadId": "t1"})
        if "/users/me/messages/g1" in url:
            return httpx.Response(200, json=_GMAIL_MESSAGE)
        if "/users/me/messages" in url:
            return httpx.Response(200, json={"messages": [{"id": "g1"}], "nextPageToken": "page-2"})
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


def _gmail_connector(sent: list[httpx.Request] | None = None) -> GmailConnector:
    client = httpx.AsyncClient(transport=_gmail_transport(sent if sent is not None else []))
    return GmailConnector("gmail-1", "user@example.com", GMAIL_CREDENTIALS, client=client)


async def test_gmail_list_and_read_normalizes_payload() -> None:
    connector = _gmail_connector()
    page = await connector.list_messages(SearchCriteria(query="report", unread_only=True, limit=5))

    assert page.next_page_token == "page-2"
    assert len(page.messages) == 1
    summary = page.messages[0]
    assert summary.provider == "gmail"
    assert summary.account_id == "gmail-1"
    assert summary.subject == "Quarterly report"
    assert summary.sender == EmailAddress(address="dana@example.com", name="Dana Lee")
    assert summary.is_read is False
    assert summary.has_attachments is True
    assert summary.received_at is not None
    assert summary.received_at.astimezone(UTC).hour == 9

    detail = await connector.get_message("g1")
    assert detail.body_text.startswith("Hello from Gmail.")
    assert [a.filename for a in detail.attachments] == ["report.pdf"]
    assert detail.in_reply_to == "<g1@mail.example>"
    await connector.aclose()


async def test_gmail_search_query_translation() -> None:
    query = GmailConnector._build_query(
        SearchCriteria(query="invoice", sender="a@b.com", subject="Q4", unread_only=True, folder="inbox")
    )
    assert query == "invoice from:a@b.com subject:Q4 is:unread in:inbox"


async def test_gmail_send_uses_raw_mime_and_reuses_token() -> None:
    sent: list[httpx.Request] = []
    connector = _gmail_connector(sent)
    result = await connector.send_message(
        OutgoingMessage(to=(EmailAddress(address="dana@example.com"),), subject="Hi", body_text="Body")
    )
    assert result.message_id == "sent-1"
    assert result.accepted_recipients == ("dana@example.com",)

    await connector.send_message(
        OutgoingMessage(to=(EmailAddress(address="dana@example.com"),), subject="Hi 2", body_text="Body")
    )
    token_requests = [r for r in sent if "oauth2.googleapis.com" in str(r.url)]
    assert len(token_requests) == 1, "the access token must be cached between calls"
    await connector.aclose()


async def test_gmail_body_is_truncated_to_configured_limit() -> None:
    long_body = base64.urlsafe_b64encode(b"x" * 500).decode().rstrip("=")
    payload = {**_GMAIL_MESSAGE, "payload": {**_GMAIL_MESSAGE["payload"]}}
    payload["payload"]["parts"] = [
        {"mimeType": "text/plain", "filename": "", "body": {"data": long_body, "size": 500}}
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2.googleapis.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        return httpx.Response(200, json=payload)

    connector = GmailConnector(
        "gmail-1",
        "user@example.com",
        GMAIL_CREDENTIALS,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_body_characters=100,
    )
    detail = await connector.get_message("g1")
    assert len(detail.body_text) == 100
    assert detail.body_truncated is True
    await connector.aclose()


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, AuthenticationError), (403, PermissionDeniedError), (404, NotFoundError)],
)
async def test_gmail_maps_http_errors(status: int, expected: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2.googleapis.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        return httpx.Response(status, text="refresh_token=super-secret-value")

    connector = GmailConnector(
        "gmail-1",
        "user@example.com",
        GMAIL_CREDENTIALS,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(expected) as excinfo:
        await connector.get_message("g1")
    assert "super-secret-value" not in str(excinfo.value)
    await connector.aclose()


async def test_gmail_timeout_is_mapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    connector = GmailConnector(
        "gmail-1",
        "user@example.com",
        GMAIL_CREDENTIALS,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderTimeoutError):
        await connector.get_message("g1")
    await connector.aclose()


async def test_gmail_rejects_empty_message_id() -> None:
    connector = _gmail_connector()
    with pytest.raises(ValidationError):
        await connector.get_message("")
    await connector.aclose()


# --------------------------------------------------------------------------- #
# Microsoft Graph
# --------------------------------------------------------------------------- #

MS_CREDENTIALS = MicrosoftCredentials(
    tenant_id="tenant",
    client_id="client-id",
    client_secret="client-secret",
    refresh_token="refresh-token",
)

_GRAPH_MESSAGE = {
    "id": "m1",
    "conversationId": "c1",
    "subject": "Vendor renewal",
    "from": {"emailAddress": {"name": "Sam Ortiz", "address": "sam@vendor.example"}},
    "toRecipients": [{"emailAddress": {"address": "user@contoso.com"}}],
    "ccRecipients": [],
    "receivedDateTime": "2026-01-05T09:00:00Z",
    "bodyPreview": "Following up",
    "isRead": True,
    "hasAttachments": False,
    "parentFolderId": "inbox-id",
    "body": {"contentType": "html", "content": "<p>Following up on the <b>renewal</b>.</p>"},
    "internetMessageId": "<m1@vendor.example>",
}


def _graph_connector(sent: list[httpx.Request] | None = None) -> MicrosoftConnector:
    recorded = sent if sent is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        url = str(request.url)
        if "login.microsoftonline.com" in url:
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if url.endswith("/sendMail") or url.endswith("/reply") or url.endswith("/replyAll"):
            return httpx.Response(202)
        if "/messages/m1" in url:
            return httpx.Response(200, json=_GRAPH_MESSAGE)
        if "/messages" in url:
            return httpx.Response(
                200,
                json={"value": [_GRAPH_MESSAGE], "@odata.nextLink": "https://graph.example/next"},
            )
        return httpx.Response(404, json={"error": "not found"})

    return MicrosoftConnector(
        "ms-1",
        "user@contoso.com",
        MS_CREDENTIALS,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_graph_list_and_read_normalizes_payload() -> None:
    connector = _graph_connector()
    page = await connector.list_messages(SearchCriteria(query="renewal", limit=5))
    assert page.next_page_token == "https://graph.example/next"
    summary = page.messages[0]
    assert summary.provider == "microsoft"
    assert summary.sender == EmailAddress(address="sam@vendor.example", name="Sam Ortiz")
    assert summary.thread_id == "c1"
    assert summary.is_read is True

    detail = await connector.get_message("m1")
    assert detail.body_text == "Following up on the renewal."
    assert "<p>" not in detail.body_text, "HTML must be converted to plain text"
    await connector.aclose()


async def test_graph_reply_all_calls_the_right_action() -> None:
    sent: list[httpx.Request] = []
    connector = _graph_connector(sent)
    await connector.reply_message("m1", "Sounds good", reply_all=True)
    assert any(str(r.url).endswith("/messages/m1/replyAll") for r in sent)
    await connector.aclose()


async def test_graph_application_credentials_target_named_mailbox() -> None:
    credentials = MicrosoftCredentials(
        tenant_id="tenant",
        client_id="client-id",
        client_secret="client-secret",
        mailbox_upn="shared@contoso.com",
    )
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if "login.microsoftonline.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        return httpx.Response(202)

    connector = MicrosoftConnector(
        "ms-2",
        "shared@contoso.com",
        credentials,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await connector.send_message(
        OutgoingMessage(to=(EmailAddress(address="a@b.com"),), subject="Hi", body_text="Body")
    )
    assert any("/users/shared@contoso.com/sendMail" in str(r.url) for r in sent)
    token_request = next(r for r in sent if "login.microsoftonline.com" in str(r.url))
    assert b"client_credentials" in token_request.content
    await connector.aclose()


async def test_graph_filter_escapes_single_quotes() -> None:
    odata = MicrosoftConnector._build_filter(SearchCriteria(sender="o'brien@example.com"))
    assert odata == "from/emailAddress/address eq 'o''brien@example.com'"


# --------------------------------------------------------------------------- #
# IMAP / SMTP
# --------------------------------------------------------------------------- #

# Dummy value only; no real credential is used anywhere in the test suite.
FAKE_SECRET = "not-a-real-app-password"

IMAP_CREDENTIALS = ImapCredentials(
    imap_host="imap.example.com",
    username="user@example.com",
    password=FAKE_SECRET,
    smtp_host="smtp.example.com",
)

RAW_EMAIL = (
    b"From: Alex Kim <alex@example.com>\r\n"
    b"To: user@example.com\r\n"
    b"Subject: Lunch tomorrow?\r\n"
    b"Date: Mon, 05 Jan 2026 09:00:00 +0000\r\n"
    b"Message-ID: <i1@example.com>\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Are you free around noon?\r\n"
)


class FakeIMAP:
    """Minimal stand-in for :class:`imaplib.IMAP4_SSL`."""

    def __init__(self) -> None:
        self.selected: str | None = None
        self.searched: list[str] = []
        self.logged_out = False

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        return "OK", [b"logged in"]

    def select(self, folder: str, readonly: bool = True) -> tuple[str, list[bytes]]:
        self.selected = folder
        return "OK", [b"3"]

    def search(self, charset: object, *criteria: str) -> tuple[str, list[bytes]]:
        self.searched = list(criteria)
        return "OK", [b"1 2 3"]

    def fetch(self, uid: bytes, parts: str) -> tuple[str, list[object]]:
        return "OK", [(b"1 (RFC822 {%d}" % len(RAW_EMAIL), RAW_EMAIL), b")"]

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "BYE", [b"bye"]


@pytest.fixture
def fake_imap(monkeypatch: pytest.MonkeyPatch) -> FakeIMAP:
    fake = FakeIMAP()
    monkeypatch.setattr(
        "omniport_agent.connectors.imap_smtp.imaplib.IMAP4_SSL",
        lambda *args, **kwargs: fake,
    )
    return fake


async def test_imap_list_normalizes_headers_and_pages(fake_imap: FakeIMAP) -> None:
    connector = ImapConnector("imap-1", "user@example.com", IMAP_CREDENTIALS)
    page = await connector.list_messages(SearchCriteria(query="lunch", limit=2))

    assert fake_imap.selected == '"INBOX"'
    assert fake_imap.searched == ["TEXT", '"lunch"']
    assert len(page.messages) == 2
    assert page.next_page_token == "2"
    assert page.messages[0].subject == "Lunch tomorrow?"
    assert page.messages[0].sender == EmailAddress(address="alex@example.com", name="Alex Kim")
    assert page.messages[0].provider == "imap"


async def test_imap_read_message_extracts_body(fake_imap: FakeIMAP) -> None:
    connector = ImapConnector("imap-1", "user@example.com", IMAP_CREDENTIALS)
    detail = await connector.get_message("1")
    assert detail.body_text == "Are you free around noon?"
    assert detail.in_reply_to == "<i1@example.com>"


async def test_imap_search_terms_are_quoted_against_injection() -> None:
    connector = ImapConnector("imap-1", "user@example.com", IMAP_CREDENTIALS)
    criteria = connector._search_criteria(SearchCriteria(query='evil" UID 1:*\r\nNOOP'))
    assert criteria == ["TEXT", '"evil\\" UID 1:*  NOOP"']


async def test_imap_rejects_non_numeric_message_id(fake_imap: FakeIMAP) -> None:
    connector = ImapConnector("imap-1", "user@example.com", IMAP_CREDENTIALS)
    with pytest.raises(ValidationError):
        await connector.get_message("not-a-uid")


async def test_imap_rejects_bad_page_token(fake_imap: FakeIMAP) -> None:
    connector = ImapConnector("imap-1", "user@example.com", IMAP_CREDENTIALS)
    with pytest.raises(ValidationError):
        await connector.list_messages(SearchCriteria(page_token="abc"))


async def test_imap_without_smtp_is_read_only() -> None:
    credentials = ImapCredentials(
        imap_host="imap.example.com",
        username="user@example.com",
        password=FAKE_SECRET,
    )
    connector = ImapConnector("imap-2", "user@example.com", credentials)
    assert connector.capabilities.can_send is False
    with pytest.raises(ValidationError):
        await connector.send_message(
            OutgoingMessage(to=(EmailAddress(address="a@b.com"),), subject="Hi", body_text="Body")
        )


async def test_imap_send_submits_over_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            captured["host"] = host
            captured["port"] = port

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def starttls(self, context: object) -> None:
            captured["starttls"] = True

        def login(self, user: str, password: str) -> None:
            captured["user"] = user

        def send_message(self, message: object) -> None:
            captured["message"] = message

    monkeypatch.setattr("omniport_agent.connectors.imap_smtp.smtplib.SMTP", FakeSMTP)
    connector = ImapConnector("imap-1", "user@example.com", IMAP_CREDENTIALS)
    result = await connector.send_message(
        OutgoingMessage(
            to=(EmailAddress(address="alex@example.com", name="Alex"),),
            subject="Lunch",
            body_text="Noon works.",
        )
    )
    assert captured["host"] == "smtp.example.com"
    assert captured["starttls"] is True
    assert result.accepted_recipients == ("alex@example.com",)
    mime = captured["message"]
    assert mime["Subject"] == "Lunch"
    assert "Alex" in mime["To"]


async def test_imap_login_failure_is_authentication_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import imaplib

    class FailingIMAP(FakeIMAP):
        def login(self, user: str, password: str):  # type: ignore[override]
            raise imaplib.IMAP4.error(f"AUTHENTICATIONFAILED for {password}")

    monkeypatch.setattr(
        "omniport_agent.connectors.imap_smtp.imaplib.IMAP4_SSL",
        lambda *args, **kwargs: FailingIMAP(),
    )
    connector = ImapConnector("imap-1", "user@example.com", IMAP_CREDENTIALS)
    with pytest.raises(AuthenticationError) as excinfo:
        await connector.get_message("1")
    assert FAKE_SECRET not in str(excinfo.value)


def test_raw_email_fixture_is_parseable() -> None:
    assert email.message_from_bytes(RAW_EMAIL)["Subject"] == "Lunch tomorrow?"


# --------------------------------------------------------------------------- #
# Mock provider
# --------------------------------------------------------------------------- #


async def test_mock_connector_searches_and_pages() -> None:
    connector = MockConnector()
    page = await connector.list_messages(SearchCriteria(limit=2))
    assert len(page.messages) == 2
    assert page.next_page_token == "2"

    second = await connector.list_messages(SearchCriteria(limit=2, page_token=page.next_page_token))
    assert len(second.messages) == 1
    assert second.next_page_token is None

    unread = await connector.list_messages(SearchCriteria(unread_only=True))
    assert [m.message_id for m in unread.messages] == ["m-1"]


async def test_mock_connector_unknown_message_raises_not_found() -> None:
    connector = MockConnector()
    with pytest.raises(NotFoundError):
        await connector.get_message("does-not-exist")
