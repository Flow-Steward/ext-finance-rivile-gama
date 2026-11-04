"""Health command for the Rivile GAMA extension (no network)."""

from __future__ import annotations

import json


def check_health() -> dict[str, object]:
    from runtime.catalog import METHODS_BY_OPERATION, OPERATION_IDS, WRITE_OPERATION_IDS
    from runtime.connection import ENDPOINT

    return {
        "extension_id": "flowsteward.rivile-gama",
        "ok": True,
        "status": "healthy",
        "endpoint": ENDPOINT,
        "rivile_methods": len(METHODS_BY_OPERATION),
        "declared_operations": len(OPERATION_IDS),
        "write_operations": len(WRITE_OPERATION_IDS),
    }


if __name__ == "__main__":
    print(json.dumps(check_health(), separators=(",", ":"), sort_keys=True))
