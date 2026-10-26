"""OmniPort Email Agent.

A Microsoft Agent Framework hosted agent that reads, summarizes, drafts and
(with explicit human approval) sends email across Gmail, Microsoft 365/Outlook
and generic IMAP/SMTP mailboxes.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = [
    "AccountRegistry",
    "DraftStore",
    "EmailToolset",
    "Principal",
    "__version__",
    "build_agent",
    "create_server",
    "use_principal",
]


def __getattr__(name: str) -> object:
    # Lazy re-exports keep ``import omniport_agent`` cheap and avoid requiring
    # the Foundry hosting extras for library-only use.
    if name in {"AccountRegistry"}:
        from .registry import AccountRegistry

        return AccountRegistry
    if name == "DraftStore":
        from .drafts import DraftStore

        return DraftStore
    if name == "EmailToolset":
        from .tools import EmailToolset

        return EmailToolset
    if name in {"Principal", "use_principal"}:
        from . import identity

        return getattr(identity, name)
    if name == "build_agent":
        from .agent import build_agent

        return build_agent
    if name == "create_server":
        from .app import create_server

        return create_server
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
