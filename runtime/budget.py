"""Keep one step's result inside what the host accepts.

The host scans every string of an extension response for reflected secrets and
fails closed past fixed budgets (core ``secret_reflection``: 16,384 candidate
strings — dict keys and string values — 65,536 traversed nodes, 8 M compared
code points), and ``main.py`` caps the serialized response at 4.5 MiB. A wide
Rivile table reaches those limits quickly: 100 rows of 80 fields is already
16,000 strings. This module measures a result the same way, with a margin for
the envelope, so multi-page reads stop at a page boundary instead of being
blocked whole.
"""

from __future__ import annotations

import json
from typing import Any

from . import errors
from .errors import ExtensionError

MAX_CANDIDATE_STRINGS = 12_000
MAX_NODES = 48_000
MAX_CODEPOINTS = 5_000_000
MAX_SERIALIZED_BYTES = 3_500_000


def measure(value: Any) -> tuple[int, int, int, int]:
    """(candidate strings, nodes, string code points, serialized bytes) of ``value``."""
    candidates = nodes = codepoints = 0
    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        nodes += 1
        if isinstance(node, str):
            candidates += 1
            codepoints += len(node)
        elif isinstance(node, dict):
            for key, nested in node.items():
                nodes += 1
                if isinstance(key, str):
                    candidates += 1
                    codepoints += len(key)
                stack.append(nested)
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return candidates, nodes, codepoints, size


def _within(totals: tuple[int, int, int, int]) -> bool:
    candidates, nodes, codepoints, size = totals
    return (
        candidates <= MAX_CANDIDATE_STRINGS
        and nodes <= MAX_NODES
        and codepoints <= MAX_CODEPOINTS
        and size <= MAX_SERIALIZED_BYTES
    )


def too_large() -> ExtensionError:
    return ExtensionError(
        errors.RESPONSE_TOO_LARGE,
        "This Rivile result is too large for one step output. Narrow it with fil or list=H, "
        "lower top, or use export_list to save it as a file.",
        definitely_no_external_effect=True,
        external_effect_status="failed",
    )


def fits(result: Any) -> bool:
    return _within(measure(result))


def ensure_fits(result: Any) -> None:
    if not fits(result):
        raise too_large()


class Meter:
    """Running totals for a result assembled page by page."""

    def __init__(self) -> None:
        self.totals = (0, 0, 0, 0)

    def admit(self, chunk: Any) -> bool:
        """Add ``chunk`` if the running result still fits; report whether it did."""
        added = measure(chunk)
        combined = (
            self.totals[0] + added[0],
            self.totals[1] + added[1],
            self.totals[2] + added[2],
            self.totals[3] + added[3],
        )
        if not _within(combined):
            return False
        self.totals = combined
        return True


__all__ = ["Meter", "ensure_fits", "fits", "measure", "too_large"]
