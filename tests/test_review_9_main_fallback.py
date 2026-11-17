"""Review 2026-11-17, item 9: a write's result too large for the step output keeps its
identifiers and the write's facts, also through the real main.py."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from typing import Any

from conftest import BUNDLE_ROOT as BUNDLE


def _main_with(script: str) -> tuple[int, dict[str, Any], int]:
    """Run the real main.py, its dispatch replaced by ``script``'s ``dispatch``."""
    code = textwrap.dedent(
        """
        import sys
        import main
        {script}
        main.dispatch_runtime = dispatch
        raise SystemExit(main.main())
        """
    ).format(script=textwrap.dedent(script).strip())
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BUNDLE,
        input=b"{}",
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert proc.stderr == b"", proc.stderr.decode()[-2000:]
    return proc.returncode, json.loads(proc.stdout), len(proc.stdout)


def _padded(size: int) -> str:
    """A dispatch whose serialized response (plus the newline) is exactly ``size`` bytes."""
    return f"""
        def response(pad):
            return {{"ok": True, "result": {{"id": 77, "pad": "x" * pad}}, "error_code": None,
                     "error": None, "errors": [], "external_effect_status": "succeeded",
                     "definitely_no_external_effect": False}}
        base = len(main._json_bytes(response(0))) + 1
        def dispatch(payload):
            return response({size} - base)
        """


def test_9_main_passes_a_response_exactly_at_the_limit() -> None:
    from main import MAX_SERIALIZED_RESPONSE_BYTES

    code, response, size = _main_with(_padded(MAX_SERIALIZED_RESPONSE_BYTES))
    assert size == MAX_SERIALIZED_RESPONSE_BYTES
    assert code == 0 and response["result"]["id"] == 77


def test_9_main_one_byte_over_the_limit_keeps_the_write_facts() -> None:
    from main import MAX_SERIALIZED_RESPONSE_BYTES

    code, response, _ = _main_with(_padded(MAX_SERIALIZED_RESPONSE_BYTES + 1))
    assert code == 2
    assert response["error_code"] == "response_too_large"
    assert response["external_effect_status"] == "succeeded"
    assert response["definitely_no_external_effect"] is False
    assert response["result"] == {"id": 77}
