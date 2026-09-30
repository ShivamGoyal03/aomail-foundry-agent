"""Connector implementations for OmniPort Email Agent."""

from .base import EmailConnector
from .gmail import GmailConnector
from .imap_smtp import ImapConnector
from .microsoft import MicrosoftConnector
from .mock import MockConnector

__all__ = [
    "EmailConnector",
    "GmailConnector",
    "ImapConnector",
    "MicrosoftConnector",
    "MockConnector",
]
