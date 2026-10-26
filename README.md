# OmniPort Email Agent

A **Microsoft Agent Framework hosted agent** that reads, searches, summarizes, drafts and — only with explicit human approval — sends email across **Gmail**, **Microsoft 365 / Outlook** and **generic IMAP/SMTP** mailboxes. It ships as a container suitable for deployment to **Microsoft Foundry Hosted Agents**.

The product requirements and connector model are informed by the open-source
[`aomail-ai/aomail-app`](https://github.com/aomail-ai/aomail-app) project, whose backend splits providers into
`backend/aomail/email_providers/{google,microsoft,imap,smtp}`. **No application code was copied.** This repository is a
standalone agent service that reuses only the architectural idea of a provider-neutral connector layer — see
[Assumptions and limitations](#assumptions-and-limitations).

---

## Contents

- [Architecture](#architecture)
- [Request, auth and tool flow](#request-auth-and-tool-flow)
- [The approval flow](#the-approval-flow)
- [Security model](#security-model)
- [Quick start (no credentials needed)](#quick-start-no-credentials-needed)
- [Environment variables](#environment-variables)
- [Provider configuration](#provider-configuration)
- [Tools](#tools)
- [Testing, linting and type checking](#testing-linting-and-type-checking)
- [Docker](#docker)
- [Deploying to Microsoft Foundry](#deploying-to-microsoft-foundry)
- [Assumptions and limitations](#assumptions-and-limitations)

---

## Architecture

```
Your application (web UI, Teams, API gateway)
   │  authenticates the end user, then injects
   │  x-omniport-principal-id  (+ HMAC signature)
   ▼
┌──────────────────────────────────────────────────────────────────┐
│  OmniPort Email Agent container (Foundry hosted agent)           │
│                                                                  │
│  ResponsesHostServer  ──  POST /responses                        │
│         │                 GET  /readiness   GET /health          │
│         │                                                        │
│   PrincipalMiddleware  →  binds a Principal into a ContextVar    │
│         │                                                        │
│   agent_framework.Agent  (model from the Foundry catalog)        │
│         │  tools registered with @tool                           │
│         ▼                                                        │
│   EmailToolset ── DraftStore (pending, unsent drafts)            │
│         │                                                        │
│   AccountRegistry  →  enforces principal → account ownership     │
│         │                                                        │
│   ┌────────────┬────────────────┬──────────────┬──────────────┐  │
│   │ Gmail      │ Microsoft      │ IMAP/SMTP    │ Mock         │  │
│   │ REST v1    │ Graph v1.0     │ imaplib /    │ in-memory    │  │
│   │            │                │ smtplib      │              │  │
│   └────────────┴────────────────┴──────────────┴──────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

Every connector implements `omniport_agent.connectors.base.EmailConnector` and normalizes provider payloads into the
shared types in `omniport_agent.models`, so the model sees one consistent shape regardless of provider.

| Module | Responsibility |
| --- | --- |
| `omniport_agent/models.py` | Provider-neutral, credential-free data model |
| `omniport_agent/config.py` | Settings and `SecretStr`-wrapped account credentials |
| `omniport_agent/identity.py` | `Principal`, header resolution, HMAC verification |
| `omniport_agent/registry.py` | Principal → account mapping, connector construction |
| `omniport_agent/connectors/` | Gmail, Microsoft Graph, IMAP/SMTP, mock |
| `omniport_agent/drafts.py` | Server-side store of unsent drafts |
| `omniport_agent/tools.py` | The `@tool` functions exposed to the model |
| `omniport_agent/agent.py` | `Agent` + `FoundryChatClient` assembly |
| `omniport_agent/app.py` | `ResponsesHostServer`, middleware, `/health` |

---

## Request, auth and tool flow

```
1. User        →  "Summarize unread mail and reply to Dana."

2. Your app    →  POST /responses
                  x-omniport-principal-id: user-42
                  x-omniport-principal-signature: <hmac-sha256(user-42, key)>

3. Middleware  →  verifies the signature, binds Principal("user-42")
                  no header and no dev mode  → 401
                  bad signature              → 403

4. Agent       →  list_accounts()                  → only user-42's mailboxes
                  search_messages(...)             → metadata only
                  collect_messages_for_summary(...)→ bodies to summarize

5. Agent       →  draft_reply(...)  →  returns a draft. NOTHING IS SENT.

6. Your app    →  shows the draft to the user

7. User        →  "Yes, send it."

8. Agent       →  send_draft(draft_id)
                  ↳ approval_mode="always_require" makes the framework emit a
                    FunctionApprovalRequestContent first
                  ↳ the tool body independently rejects any unknown draft id

9. Connector   →  Gmail API / Microsoft Graph / SMTP submission
```

The model never sees a principal id, a credential or a token. It only ever passes `account_id` values that the registry
has already confirmed belong to the caller.

---

## The approval flow

Sending in a single step is deliberately impossible. Two independent gates must both open.

**Gate 1 — the framework's human-in-the-loop approval.**
`send_draft` is registered with `approval_mode="always_require"`, so Agent Framework emits a
`FunctionApprovalRequestContent` and will not execute the function until it receives a matching
`FunctionApprovalResponseContent`.

**Gate 2 — the draft precondition.**
`send_draft` accepts only a `draft_id` previously minted by `draft_email` or `draft_reply`. There is no
`send_email(to=..., body=...)` tool at all, so a natural-language request such as *"email Dana that I'm running late"*
cannot become an outbound message without the user first seeing the exact recipients, subject and body.

Further guarantees, each covered by a test:

- the draft is consumed on success, so one approval cannot send twice;
- the draft is **kept** if the provider call fails, so a transient error is retryable without re-drafting;
- a draft belongs to the principal that created it and is invisible to everyone else;
- `discard_draft` permanently revokes a rejected draft;
- drafts expire (one hour by default) and the store is size-bounded.

---

## Security model

| Concern | Control |
| --- | --- |
| **Account selection** | The model cannot choose a user. `Principal` comes from a request header bound to a `ContextVar`; `AccountRegistry.resolve` rejects any account owned by a different principal. |
| **Account enumeration** | A mailbox owned by someone else returns the *same* `account_not_found` error as one that does not exist. The difference is logged server-side only. |
| **Secrets in prompts** | Credentials live in `pydantic.SecretStr` fields on `AccountConfig`, which no tool returns. `AccountInfo`, which the model does see, has no credential fields. |
| **Secrets in errors** | `omniport_agent.errors.scrub()` redacts bearer/refresh tokens, client secrets and password-shaped assignments from every message and bounds its length. IMAP authentication failures discard the server reply entirely, because `imaplib` echoes the submitted credential. |
| **Header spoofing** | Set `OMNIPORT_PRINCIPAL_SIGNING_KEY` to require an HMAC-SHA256 signature over the principal id, compared with `hmac.compare_digest`. |
| **Header injection** | Recipient addresses are validated against an anchored pattern that rejects whitespace and newlines, so `a@b.com\nBcc: attacker@evil.example` is refused. |
| **IMAP command injection** | Search terms are quoted and CR/LF stripped before reaching `IMAP4.search`. |
| **Transport** | IMAP uses TLS (`IMAP4_SSL`, or `STARTTLS` with a default-verifying `SSLContext`); SMTP uses STARTTLS or implicit TLS. |
| **Resource limits** | A timeout on every provider call, bounded page sizes, truncated bodies, a recipient cap, and a batch cap on summarization. |
| **Least privilege** | Gmail: `gmail.modify` + `gmail.send`. Graph: `Mail.ReadWrite` + `Mail.Send`. The container runs as a non-root user. |

> **Never enable `OMNIPORT_DEV_PRINCIPAL_ID` in a deployed environment.** It makes unauthenticated requests resolve to a
> fixed principal and exists only for local development. The service logs a warning at startup when it is set.

---

## Quick start (no credentials needed)

Requires Python 3.11 or newer.

```bash
git clone https://github.com/ShivamGoyal03/aomail-foundry-agent.git
cd aomail-foundry-agent

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements-dev.txt
pip install -e .

cp .env.example .env
```

Run the test suite. It is fully offline and uses mocked providers:

```bash
pytest
```

Exercise the tools against the built-in in-memory mailbox, with no Foundry project and no real credentials:

```bash
OMNIPORT_DEV_PRINCIPAL_ID=local-dev OMNIPORT_ENABLE_MOCK_ACCOUNT=1 python - <<'PY'
import asyncio
from omniport_agent.identity import Principal, use_principal
from omniport_agent.registry import AccountRegistry
from omniport_agent.tools import EmailToolset

async def main():
    tools = EmailToolset(AccountRegistry.from_environment())
    with use_principal(Principal("local-dev")):
        print(await tools.list_accounts())
        print(await tools.search_messages(account_id="mock-inbox", query="report"))
        drafted = await tools.draft_reply(
            account_id="mock-inbox", message_id="m-1", body="Thanks, looks good."
        )
        print(drafted)                                   # nothing sent yet
        print(await tools.send_draft(draft_id=drafted["draft"]["draft_id"]))

asyncio.run(main())
PY
```

Starting the HTTP server additionally needs a model. With a Foundry project configured (see
[Deploying to Microsoft Foundry](#deploying-to-microsoft-foundry)):

```bash
export FOUNDRY_PROJECT_ENDPOINT="https://<your-project>.services.ai.azure.com/api/projects/<project>"
export AZURE_AI_MODEL_DEPLOYMENT_NAME="gpt-4.1-mini"
export OMNIPORT_DEV_PRINCIPAL_ID="local-dev"
export OMNIPORT_ENABLE_MOCK_ACCOUNT=1

python -m omniport_agent.app        # or: omniport-email-agent
```

Then, from another shell:

```bash
curl http://localhost:8088/health
curl http://localhost:8088/readiness

curl -X POST http://localhost:8088/responses \
  -H 'content-type: application/json' \
  -H 'x-omniport-principal-id: local-dev' \
  -d '{"input": "What unread mail do I have?"}'
```

With `OMNIPORT_PRINCIPAL_SIGNING_KEY` set, the signature header is mandatory:

```bash
SIG=$(python -c "from omniport_agent.identity import sign_principal; print(sign_principal('user-42', 'your-key'))")
curl -X POST http://localhost:8088/responses \
  -H 'content-type: application/json' \
  -H 'x-omniport-principal-id: user-42' \
  -H "x-omniport-principal-signature: $SIG" \
  -d '{"input": "List my accounts."}'
```

---

## Environment variables

### Model and hosting

| Variable | Default | Description |
| --- | --- | --- |
| `FOUNDRY_PROJECT_ENDPOINT` | — | Foundry project endpoint. Injected automatically when hosted. |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | — | Model deployment name. Injected automatically when hosted. |
| `PORT` | `8088` | Listener port. Foundry sets this. |
| `OMNIPORT_HOST` | `0.0.0.0` | Bind address. |
| `OMNIPORT_AGENT_NAME` | `OmniPortEmailAgent` | Agent name reported to the host. |

### Identity boundary

| Variable | Default | Description |
| --- | --- | --- |
| `OMNIPORT_PRINCIPAL_HEADER` | `x-omniport-principal-id` | Header carrying the authenticated user id. |
| `OMNIPORT_PRINCIPAL_SIGNING_KEY` | unset | When set, the principal header must carry a valid HMAC-SHA256 signature. **Strongly recommended.** |
| `OMNIPORT_PRINCIPAL_SIGNATURE_HEADER` | `x-omniport-principal-signature` | Header carrying that signature. |
| `OMNIPORT_DEV_PRINCIPAL_ID` | unset | **Local development only.** Treats unauthenticated requests as this principal. |

### Mailboxes and limits

| Variable | Default | Description |
| --- | --- | --- |
| `OMNIPORT_ACCOUNTS` | unset | JSON array of mailbox configurations (see below). |
| `OMNIPORT_ENABLE_MOCK_ACCOUNT` | auto | `1` adds an in-memory mailbox. Defaults on only when no accounts are configured *and* dev mode is on. |
| `OMNIPORT_REQUEST_TIMEOUT_SECONDS` | `30` | Per-request provider timeout. |
| `OMNIPORT_MAX_PAGE_SIZE` | `50` | Upper bound on messages returned per page. |
| `OMNIPORT_MAX_BODY_CHARACTERS` | `8000` | Message bodies are truncated to this length. |

`.env.example` documents all of these together with a complete `OMNIPORT_ACCOUNTS` example.

---

## Provider configuration

`OMNIPORT_ACCOUNTS` is a JSON array. Each entry binds a mailbox to the `principal_id` that owns it — the value your
application puts in the principal header.

### Gmail (Gmail REST API v1)

Create OAuth credentials in Google Cloud, request the scopes `https://www.googleapis.com/auth/gmail.modify` and
`https://www.googleapis.com/auth/gmail.send`, and obtain a refresh token for the user.

```json
{
  "account_id": "gmail-personal",
  "provider": "gmail",
  "email": "user@gmail.com",
  "principal_id": "user-42",
  "credentials": {
    "kind": "gmail",
    "client_id": "<client-id>.apps.googleusercontent.com",
    "client_secret": "<client-secret>",
    "refresh_token": "<refresh-token>"
  }
}
```

### Microsoft 365 / Outlook (Microsoft Graph v1.0)

Register an application in Microsoft Entra ID with `Mail.ReadWrite` and `Mail.Send`.

*Delegated* — acts as the user; include `refresh_token`:

```json
{
  "account_id": "work-m365",
  "provider": "microsoft",
  "email": "user@contoso.com",
  "principal_id": "user-42",
  "credentials": {
    "kind": "microsoft",
    "tenant_id": "<tenant-guid>",
    "client_id": "<client-id>",
    "client_secret": "<client-secret>",
    "refresh_token": "<refresh-token>"
  }
}
```

*Application-only* — omit `refresh_token` and set `mailbox_upn`. Restrict the app registration to specific mailboxes
with an application access policy:

```json
{
  "credentials": {
    "kind": "microsoft",
    "tenant_id": "<tenant-guid>",
    "client_id": "<client-id>",
    "client_secret": "<client-secret>",
    "mailbox_upn": "shared@contoso.com"
  }
}
```

### Generic IMAP/SMTP

Works with any provider that still permits password or app-password authentication — Yahoo, Fastmail, custom domains,
and Gmail or Outlook in IMAP fallback mode.

```json
{
  "account_id": "fastmail",
  "provider": "imap",
  "email": "user@example.com",
  "principal_id": "user-42",
  "credentials": {
    "kind": "imap",
    "imap_host": "imap.example.com",
    "imap_port": 993,
    "imap_use_ssl": true,
    "username": "user@example.com",
    "password": "<app-password>",
    "smtp_host": "smtp.example.com",
    "smtp_port": 587,
    "smtp_starttls": true,
    "default_folder": "INBOX"
  }
}
```

Omit `smtp_host` to make the mailbox **read-only**: `capabilities.can_send` then reports `false` and any send attempt is
refused with a clear message.

### Provider capability matrix

| Capability | Gmail | Microsoft Graph | IMAP/SMTP | Mock |
| --- | --- | --- | --- | --- |
| Search / list | ✅ server-side `q` | ✅ `$search` / `$filter` | ✅ IMAP `SEARCH` | ✅ in-memory |
| Read body + attachment names | ✅ | ✅ | ✅ | ✅ |
| Send | ✅ raw MIME | ✅ `sendMail` | ✅ SMTP | ✅ recorded |
| Reply / reply-all | ✅ thread-aware | ✅ `reply` / `replyAll` | ⚠️ header-based, no threads | ✅ |
| Threads | ✅ `threadId` | ✅ `conversationId` | ❌ | ✅ |
| Labels | ✅ | ❌ | ❌ | ❌ |
| Pagination | ✅ page token | ✅ `@odata.nextLink` | ⚠️ numeric offset | ✅ offset |

### Extension points

- **OAuth2 / XOAUTH2 for IMAP** — override `ImapConnector._authenticate` and call
  `connection.authenticate("XOAUTH2", ...)` with a provider-issued token.
- **Secret managers** — replace the `SecretStr` fields in `omniport_agent/config.py` with a lookup against Azure Key
  Vault or Google Secret Manager. Nothing outside `config.py` reads them.
- **New providers** — subclass `EmailConnector`, add a credentials model with a new `kind`, and register it in
  `omniport_agent/registry.py::build_connector`.
- **Account source of truth** — replace `AccountRegistry.from_environment` with a lookup against your application
  database.
- **Durable drafts** — swap `DraftStore` for a database-backed implementation.

---

## Tools

| Tool | Approval | Purpose |
| --- | --- | --- |
| `list_accounts` | never | Mailboxes belonging to the signed-in user. |
| `search_messages` | never | Search or list messages. Metadata only, paginated. |
| `read_message` | never | One message: plain-text body and attachment names. |
| `collect_messages_for_summary` | never | Up to 10 bodies at once, for summarizing. |
| `draft_email` | never | Prepare a new message. **Sends nothing.** |
| `draft_reply` | never | Prepare a reply. **Sends nothing.** |
| `discard_draft` | never | Revoke a rejected draft. |
| `send_draft` | **always** | The only tool that transmits mail. |

Tool failures are returned as `{"error": {"code": ..., "message": ...}}` rather than raised, so the model can explain the
problem instead of retrying blindly. Codes include `account_not_found`, `authentication_error`, `permission_denied`,
`not_found`, `provider_error`, `provider_timeout`, `validation_error` and `approval_required`.

---

## Testing, linting and type checking

```bash
pytest                 # unit tests, fully offline
ruff check .           # lint
ruff format .          # format
mypy                   # type-check src/omniport_agent
```

The suite covers connector normalization for all four provider families, HTTP error mapping and timeouts, credential
scrubbing, IMAP quoting, the identity boundary and HMAC verification, the full approval flow, generated tool schemas,
and the HTTP middleware. CI runs all three commands on Python 3.11 and 3.12 (`.github/workflows/ci.yml`).

---

## Docker

```bash
docker build -t omniport-email-agent .

docker run --rm -p 8088:8088 \
  -e FOUNDRY_PROJECT_ENDPOINT="https://<project>.services.ai.azure.com/api/projects/<project>" \
  -e AZURE_AI_MODEL_DEPLOYMENT_NAME="gpt-4.1-mini" \
  -e OMNIPORT_PRINCIPAL_SIGNING_KEY="<shared-secret>" \
  -e OMNIPORT_ACCOUNTS="$(cat accounts.json)" \
  omniport-email-agent
```

The image runs as a non-root user and listens on `8088`. Pass secrets as environment variables from your orchestrator's
secret store — never bake them into the image.

---

## Deploying to Microsoft Foundry

This service uses the **current** Agent Framework hosted-agent conventions. If you are following an older tutorial, note
these renames:

| Obsolete | Current |
| --- | --- |
| `ChatAgent` | `agent_framework.Agent` |
| `@ai_function` / `AIFunction` | `@tool` / `FunctionTool` |
| `agent-framework-azure-ai`, `AzureAIAgentClient` | `agent-framework-foundry`, `FoundryChatClient` |
| `azure-ai-agentserver-agentframework`, `from_agent_framework(agent).run()` | `agent-framework-foundry-hosting`, `ResponsesHostServer(agent).run()` |

Deploy with the Azure Developer CLI:

```bash
azd ext install azure.ai.agents
azd ai agent init                 # scaffolds agent.yaml / agent.manifest.yaml
azd provision
azd deploy

azd ai agent invoke --local "What unread mail do I have?"
```

Configure `OMNIPORT_ACCOUNTS` and `OMNIPORT_PRINCIPAL_SIGNING_KEY` as Foundry secrets. `FOUNDRY_PROJECT_ENDPOINT`,
`AZURE_AI_MODEL_DEPLOYMENT_NAME`, `PORT` and `APPLICATIONINSIGHTS_CONNECTION_STRING` are injected by the platform.

### Surfaces that may need adjustment for your tenant

These are deliberately isolated, so a tenant-specific change touches exactly one place:

1. **`omniport_agent/agent.py::build_chat_client`** — the only Foundry client construction. Swap `FoundryChatClient` or
   the credential type here (for example `ManagedIdentityCredential` instead of `DefaultAzureCredential`).
2. **`omniport_agent/app.py::create_server`** — the only hosting-protocol choice. Replace `ResponsesHostServer` with
   `InvocationsHostServer` if your deployment uses the invocations protocol.
3. **Manifest protocol version** — the pinned `agent-framework-foundry-hosting` beta targets **protocol version
   2.0.0**; declare `2.0.0` in your generated `agent.yaml` / `agent.manifest.yaml`.
4. **Approval transport** — how `FunctionApprovalRequestContent` reaches your UI depends on the client you build on top
   of `/responses`.

---

## Assumptions and limitations

**Assumptions**

- `aomail-ai/aomail-app` was inspected, not copied. Its backend separates `email_providers/google`, `/microsoft`,
  `/imap` and `/smtp`, and its IMAP search composes `AND(...)` criteria over subject, body, sender, recipient and date.
  This repository mirrors that *shape* with an independent implementation and its own normalized data model.
- The mailbox-to-user mapping is supplied through `OMNIPORT_ACCOUNTS`. A production deployment would more likely read it
  from the owning application's database; `AccountRegistry` is the single seam to replace.
- The calling application performs end-user authentication. This service trusts — but can cryptographically verify —
  the principal header.

**Limitations**

- **Drafts are in-memory.** A container restart drops pending drafts, and replicas do not share them. Pin the agent to a
  single replica or replace `DraftStore` before scaling out.
- **IMAP message ids are sequence numbers**, which are not stable across sessions if the mailbox changes. Search again
  before reading. Gmail and Graph ids are stable.
- **IMAP has no thread model.** Replies are header-based (`In-Reply-To` / `References`) and `reply_all` reconstructs
  recipients from the original headers.
- **IMAP pagination is a client-side offset** over the search result set, so it can skew if the mailbox changes between
  pages.
- **No attachment content.** Attachment names, types and sizes are returned; bodies are never downloaded or forwarded.
- **No write operations other than send and reply** — no delete, move, flag or label changes.
- **Single-process token cache.** Access tokens are cached per connector instance, so each replica refreshes
  independently.
- `agent-framework-foundry-hosting` is still a **beta** package; keep it pinned if you need reproducible builds.
- `build_chat_client()` has not been exercised against a live Foundry project in this repository's tests; it is covered
  only by mocked tests, by design.

---

## License

MIT
