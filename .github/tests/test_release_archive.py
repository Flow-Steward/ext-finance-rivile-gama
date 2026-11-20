"""The release publishes the exact archive made by the clean package job."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"


def _workflow(name: str) -> dict:
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_only_the_dependency_free_job_uploads_the_release_archive():
    ci = _workflow("ci.yml")
    package = ci["jobs"]["package"]
    validation = ci["jobs"]["extension"]
    assert validation["needs"] == "package"
    assert all("pip" not in step.get("run", "") for step in package["steps"])
    assert [
        step["with"]["name"]
        for step in package["steps"]
        if "upload-artifact@" in step.get("uses", "")
    ] == ["dist"]
    assert all("upload-artifact@" not in step.get("uses", "") for step in validation["steps"])
    assert ci["on"]["workflow_call"]["outputs"]["archive_sha256"]["value"] == (
        "${{ jobs.package.outputs.archive_sha256 }}"
    )


@pytest.mark.parametrize("verified", [True, False])
def test_release_checks_the_archive_bytes(tmp_path, verified):
    release = _workflow("release.yml")["jobs"]["release"]
    steps = release["steps"]
    verify = next(step for step in steps if step.get("name") == "Verify the release archive")
    assert verify["env"]["EXPECTED_ARCHIVE_SHA256"] == "${{ needs.ci.outputs.archive_sha256 }}"
    assert steps[-2] is verify and steps[-1]["name"] == "Create the release"
    dist = tmp_path / "dist"
    dist.mkdir()
    archive = dist / "bundle.zip"
    archive.write_bytes(b"original archive")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if not verified:
        archive.write_bytes(b"different archive")
    process = subprocess.run(  # nosec B603 B607 - run the checked-in verification step
        ["bash", "-e", "-c", verify["run"]],
        cwd=tmp_path,
        env={**os.environ, "EXPECTED_ARCHIVE_SHA256": f"{digest}  {archive.name}"},
        check=False,
    )
    assert (process.returncode == 0) is verified
