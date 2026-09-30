"""Recipient parsing and validation for tool arguments."""

from __future__ import annotations

import re

from .errors import ValidationError
from .models import EmailAddress

__all__ = ["parse_recipient_list"]

# Deliberately permissive but anchored: rejects whitespace, control characters
# and header-injection payloads such as "a@b.com\nBcc: c@d.com".
_ADDRESS_RE = re.compile(r"^[^\s@<>,;:\\\"\[\]]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,}$")


def parse_recipient_list(raw: list[str] | None, label: str, max_recipients: int) -> tuple[EmailAddress, ...]:
    """Validate and normalize a list of recipient addresses from the model."""
    if not raw:
        return ()
    if len(raw) > max_recipients:
        raise ValidationError(f"Too many '{label}' recipients: at most {max_recipients} are allowed.")

    from email.utils import parseaddr

    addresses: list[EmailAddress] = []
    seen: set[str] = set()
    for entry in raw:
        name, address = parseaddr(str(entry))
        address = address.strip()
        if not _ADDRESS_RE.match(address):
            raise ValidationError(f"'{entry}' in '{label}' is not a valid email address.")
        key = address.lower()
        if key in seen:
            continue
        seen.add(key)
        addresses.append(EmailAddress(address=address, name=name.strip() or None))
    return tuple(addresses)
