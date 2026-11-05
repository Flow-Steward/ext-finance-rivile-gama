"""Input checks shared by the per-method operations and the everyday ones.

One definition each, so a value accepted by one kind of step (for example
``max_pages: "5"``) is accepted by every other.
"""

from __future__ import annotations

import re
from typing import Any

from . import errors
from .errors import ExtensionError

_DIGITS = re.compile(r"[0-9]{1,9}")


def invalid(message: str) -> ExtensionError:
    """A refused input: nothing was sent, so there was no external effect."""
    return ExtensionError(
        errors.INVALID_PAYLOAD,
        message,
        definitely_no_external_effect=True,
        external_effect_status="failed",
    )


def whole(name: str, value: Any, *, default: int, maximum: int, minimum: int = 1) -> int:
    """A whole number from ``minimum`` to ``maximum``; ASCII digit strings are accepted."""
    if value is None:
        return default
    if isinstance(value, str) and _DIGITS.fullmatch(value.strip()):
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise invalid(f"{name} must be a whole number from {minimum} to {maximum}")
    return value


def flag(name: str, value: Any, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise invalid(f"{name} must be true or false")
    return value


__all__ = ["flag", "invalid", "whole"]
