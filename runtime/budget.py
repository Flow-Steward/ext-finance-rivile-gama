"""Keep one step's result inside what the host accepts.

The host scans every string of an extension response for reflected secrets and
fails closed past fixed budgets (core ``secret_reflection`` since Flow Steward
0.9.16: 3,145,728 traversed nodes and 64 Mi compared code points — distinct
strings times the number of connection secrets; earlier hosts also capped the
response at 16,384 candidate strings, a limit that no longer exists), and
``main.py`` caps the serialized response at 4.5 MiB. This module measures a
result the same way, well inside those limits and with a margin for the
envelope, so multi-page reads stop at a page boundary instead of being blocked
whole.

The budgets below are not a copy of the host's numbers. Each one stays far
below the matching host limit: a 3.5 MB JSON document holds at most ~1.75 M
nodes, so the serialized-bytes budget is in practice the binding one, and the
comparison work (code points plus one unit per string, for the one API key a
connection holds) stays under 6 M of the host's 64 Mi.
"""

from __future__ import annotations

import json
from typing import Any

from . import errors
from .errors import ExtensionError

MAX_NODES = 1_000_000
MAX_CODEPOINTS = 5_000_000
MAX_SERIALIZED_BYTES = 3_500_000


def measure(value: Any) -> tuple[int, int, int]:
    """(nodes, string code points, serialized bytes) of ``value``.

    Nodes are counted as the host traverses them: every value, and every dict
    key next to its value.
    """
    nodes = codepoints = 0
    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        nodes += 1
        if isinstance(node, str):
            codepoints += len(node)
        elif isinstance(node, dict):
            for key, nested in node.items():
                nodes += 1
                if isinstance(key, str):
                    codepoints += len(key)
                stack.append(nested)
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return nodes, codepoints, size


def _within(totals: tuple[int, int, int]) -> bool:
    nodes, codepoints, size = totals
    return nodes <= MAX_NODES and codepoints <= MAX_CODEPOINTS and size <= MAX_SERIALIZED_BYTES


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
        self.totals = (0, 0, 0)

    def admit(self, chunk: Any) -> bool:
        """Add ``chunk`` if the running result still fits; report whether it did."""
        added = measure(chunk)
        combined = (
            self.totals[0] + added[0],
            self.totals[1] + added[1],
            self.totals[2] + added[2],
        )
        if not _within(combined):
            return False
        self.totals = combined
        return True


__all__ = [
    "MAX_CODEPOINTS",
    "MAX_NODES",
    "MAX_SERIALIZED_BYTES",
    "Meter",
    "ensure_fits",
    "fits",
    "measure",
    "too_large",
]
