"""Project-scoped Rivile GAMA connection: the API key and the reading options.

A connection is built from what the host hydrates for one invocation and is
discarded when that invocation ends. The API key lives in a private field of a
frozen dataclass whose ``repr`` never shows it, and it is never placed in a
result, a log line or an exception message.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import errors
from .errors import ExtensionError

CONNECTION_TYPE_ID = "rivile_gama"
#: Rivile API v2 has one endpoint for every method. It is fixed here and never
#: taken from configuration or workflow input.
ENDPOINT = "https://api.manorivile.lt/client/v2"
ENDPOINT_HOST = "api.manorivile.lt"

CONFIG_FIELDS: frozenset[str] = frozenset({"label", "trim_values"})
SECRET_FIELDS: frozenset[str] = frozenset({"api_key"})

MIN_KEY_LENGTH = 8
MAX_KEY_LENGTH = 512
#: Printable, non-space ASCII. Anything else could inject a header.
_KEY_RE = re.compile(r"^[\x21-\x7e]+$")


@dataclass(frozen=True)
class Connection:
    """One validated Rivile GAMA API key and how its responses are read."""

    connection_id: str
    trim_values: bool
    _api_key: str = field(repr=False)

    @property
    def api_user(self) -> str:
        """The Rivile GAMA user the key logs in as (the part before the dot)."""
        head, _, _rest = self._api_key.partition(".")
        return head if _rest else ""

    def headers(self) -> dict[str, str]:
        return {"ApiKey": self._api_key}

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"Connection(connection_id={self.connection_id!r})"

    __str__ = __repr__


def _api_key(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ExtensionError(errors.INVALID_CONNECTION, "Add the Rivile API key to this connection")
    if len(text) < MIN_KEY_LENGTH or len(text) > MAX_KEY_LENGTH or _KEY_RE.fullmatch(text) is None:
        raise ExtensionError(errors.INVALID_CONNECTION, "The Rivile API key is not usable")
    return text


def _flag(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ExtensionError(errors.INVALID_CONNECTION, "trim_values must be true or false")


def connection_from_payload(payload: Mapping[str, Any], *, connection_ref: str) -> Connection:
    """Validate the selected project connection before any outbound request.

    Connection material is read only from ``action.target.connection``, and the
    operation's ``connection_ref`` must name that same connection.
    """
    action = payload.get("action")
    target = action.get("target") if isinstance(action, Mapping) else None
    connection = target.get("connection") if isinstance(target, Mapping) else None
    if not isinstance(connection, Mapping):
        raise ExtensionError(errors.INVALID_CONNECTION, "The selected connection is unavailable")
    connection_id = str(connection.get("connection_id") or "")
    if not connection_id or (connection_ref and connection_id != connection_ref):
        raise ExtensionError(
            errors.INVALID_CONNECTION, "The selected connection does not match connection_ref"
        )
    connection_type = str(
        connection.get("connection_type_id") or connection.get("connection_type") or ""
    )
    if connection_type and connection_type != CONNECTION_TYPE_ID:
        raise ExtensionError(errors.INVALID_CONNECTION, "The selected connection is not supported")
    config = connection.get("config")
    config = config if isinstance(config, Mapping) else {}
    secrets = connection.get("secrets")
    secrets = secrets if isinstance(secrets, Mapping) else {}
    if set(secrets) - SECRET_FIELDS:
        raise ExtensionError(
            errors.INVALID_CONNECTION, "The connection carries unsupported secrets"
        )
    return Connection(
        connection_id=connection_id,
        trim_values=_flag(config.get("trim_values"), True),
        _api_key=_api_key(secrets.get("api_key")),
    )


__all__ = [
    "CONFIG_FIELDS",
    "CONNECTION_TYPE_ID",
    "ENDPOINT",
    "ENDPOINT_HOST",
    "SECRET_FIELDS",
    "Connection",
    "connection_from_payload",
]
