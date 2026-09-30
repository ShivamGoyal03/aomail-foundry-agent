"""Agent assembly.

Uses the current Microsoft Agent Framework API (``agent_framework>=1.19``):
``Agent`` (formerly ``ChatAgent``), the ``@tool`` decorator (formerly
``@ai_function``) and ``FoundryChatClient`` (formerly ``AzureAIAgentClient``
from the retired ``agent-framework-azure-ai`` package).
"""

from __future__ import annotations

from typing import Any

from agent_framework import Agent

from .config import Settings, get_settings
from .errors import ConfigurationError
from .tools import EmailToolset

__all__ = ["INSTRUCTIONS", "build_chat_client", "build_agent"]

INSTRUCTIONS = """\
You are OmniPort Email Agent, an assistant for the signed-in user's email across \
Gmail, Microsoft 365/Outlook and generic IMAP mailboxes.

How to work:
1. Call list_accounts first. Only use account_id values it returns; never guess \
   an account id or a user identity.
2. Use search_messages to find mail and read_message or \
   collect_messages_for_summary before describing message contents. Never \
   invent senders, subjects or message text.
3. To send anything, call draft_email or draft_reply, show the complete draft \
   (recipients, subject and body) to the user, and wait for them to say yes.
4. Only then call send_draft with that draft_id. Never call send_draft for a \
   draft the user has not seen and approved, and never treat a general request \
   such as "reply to Dana" as approval to send.
5. If a tool returns an error object, explain it plainly and suggest the next \
   step. Do not retry the same failing call repeatedly.
6. Never ask the user for passwords, tokens or API keys; credentials are \
   managed by the service and are not available to you.
"""


def build_chat_client(settings: Settings | None = None) -> Any:
    """Create the Foundry chat client from configuration.

    Isolation point for tenant-specific deployment: everything that may need to
    change for a particular Foundry project lives in this function.
    """
    settings = settings or get_settings()
    if not settings.foundry_project_endpoint:
        raise ConfigurationError(
            "FOUNDRY_PROJECT_ENDPOINT (or OMNIPORT_FOUNDRY_PROJECT_ENDPOINT) must be set "
            "to create the Foundry chat client."
        )
    if not settings.model_deployment:
        raise ConfigurationError(
            "AZURE_AI_MODEL_DEPLOYMENT_NAME (or OMNIPORT_MODEL_DEPLOYMENT) must be set "
            "to create the Foundry chat client."
        )
    from agent_framework.foundry import FoundryChatClient
    from azure.identity import DefaultAzureCredential

    return FoundryChatClient(
        project_endpoint=settings.foundry_project_endpoint,
        model=settings.model_deployment,
        credential=DefaultAzureCredential(),
    )


def build_agent(
    *,
    toolset: EmailToolset | None = None,
    chat_client: Any | None = None,
    settings: Settings | None = None,
) -> Agent:
    """Build the hosted agent. Injecting ``chat_client`` keeps tests offline."""
    settings = settings or get_settings()
    toolset = toolset or EmailToolset(settings=settings)
    client = chat_client if chat_client is not None else build_chat_client(settings)
    return Agent(
        client=client,
        instructions=INSTRUCTIONS,
        name=settings.agent_name,
        description="Reads, summarizes, drafts and (with approval) sends email across providers.",
        tools=toolset.as_tools(),
    )
