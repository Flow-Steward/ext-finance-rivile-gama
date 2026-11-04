"""Immutable description of one Rivile GAMA API v2 method."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Method:
    """One documented Rivile method and the inputs its operation accepts.

    ``params`` pairs each operation input name with the exact Rivile wire name
    (``("error_action", "errorAction")``). ``data_roots`` are the documented
    top-level tags of the request ``data`` block (``N08``, ``I06``…).
    """

    method: str
    write: bool
    paged: bool
    params: tuple[tuple[str, str], ...]
    data_roots: tuple[str, ...]
    title: str
    group: str
    required: tuple[str, ...] = ()

    @property
    def operation_id(self) -> str:
        return self.method.lower()

    def wire_name(self, lowered: str) -> str:
        """The documented wire name of a param, matched case-insensitively, or ''."""
        for _name, wire in self.params:
            if wire.lower() == lowered:
                return wire
        return ""

    @property
    def page_param(self) -> str:
        """The wire name Rivile documents for the page number, or ''."""
        for _name, wire in self.params:
            if wire.lower() == "pagenumber":
                return wire
        return ""


__all__ = ["Method"]
