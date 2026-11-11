"""T22: the release workflow's version/tag/changelog rules (.github/scripts/release_info.py)."""
import runpy
from pathlib import Path

import pytest

from core.app_info import APP_VERSION

ROOT = Path(__file__).resolve().parents[1]
info = runpy.run_path(str(ROOT / ".github" / "scripts" / "release_info.py"))


def test_version_is_plain_numbers_and_read_from_app_info():
    assert info["VERSION_RE"].match(APP_VERSION)        # Inno Setup VersionInfoVersion: digits + dots
    assert info["app_version"]() == APP_VERSION


def test_changelog_has_notes_for_the_current_version():
    notes = info["changelog_notes"](APP_VERSION)
    assert notes.strip() and "## [" not in notes        # only this version's section


def test_tag_must_match_the_version():
    assert info["check_tag"]("v1.2.3", "1.2.3") is False          # release
    assert info["check_tag"]("v1.2.3-rc.1", "1.2.3") is True      # pre-release
    for bad in ("1.2.3", "v1.2.4", "v1.2.3.1", "v1.2.3-", "v1.2"):
        with pytest.raises(SystemExit):
            info["check_tag"](bad, "1.2.3")


def test_workflow_pins_tool_hashes_and_publishes_to_the_releases_repo():
    wf = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "Hush-Studio-Releases" in wf and "secrets.RELEASES_TOKEN" in wf          # D05
    assert "FFMPEG_SHA256" in wf and "DEEPFILTER_SHA256" in wf
    assert "build\\build.ps1" in wf and "pytest" in wf
