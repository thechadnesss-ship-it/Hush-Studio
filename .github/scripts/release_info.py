"""Release facts for .github/workflows/release.yml (T22): version, pre-release flag, notes.

    python .github/scripts/release_info.py [--tag v0.3.0] [--notes notes.md]

* The version is core/app_info.py's APP_VERSION (the only place it lives).
* With --tag, the tag must be v<APP_VERSION> (a release) or v<APP_VERSION>-<label>, e.g.
  v0.3.0-rc.1 (a pre-release). Anything else fails, so a forgotten version bump can't ship.
* The notes are CHANGELOG.md's "## [<APP_VERSION>]" section; a missing or empty section fails.
* Prints key=value lines and appends them to $GITHUB_OUTPUT when it is set.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def app_version(root: Path = ROOT) -> str:
    text = (root / "core" / "app_info.py").read_text(encoding="utf-8")
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        raise SystemExit("APP_VERSION not found in core/app_info.py")
    return m.group(1)


def check_tag(tag: str, version: str) -> bool:
    """True for a pre-release tag, False for a release; SystemExit if the tag doesn't match."""
    if tag == f"v{version}":
        return False
    if re.fullmatch(rf"v{re.escape(version)}-[0-9A-Za-z][0-9A-Za-z.]*", tag):
        return True
    raise SystemExit(f"Tag {tag!r} doesn't match core/app_info.py APP_VERSION {version!r}: "
                     f"tag v{version} (release) or v{version}-rc.1 (pre-release), or bump the version.")


def changelog_notes(version: str, root: Path = ROOT) -> str:
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.MULTILINE | re.DOTALL)
    if not m or not m.group(1).strip():
        raise SystemExit(f"CHANGELOG.md has no '## [{version}] - YYYY-MM-DD' section with notes.")
    return m.group(1).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tag", help="the pushed git tag, e.g. v0.3.0")
    ap.add_argument("--notes", type=Path, help="write the changelog section here")
    args = ap.parse_args(argv)

    version = app_version()
    if not VERSION_RE.match(version):
        raise SystemExit(f"APP_VERSION {version!r} must be MAJOR.MINOR.PATCH (digits only).")
    prerelease = check_tag(args.tag, version) if args.tag else True
    notes = changelog_notes(version)
    if args.notes:
        args.notes.write_text(notes, encoding="utf-8")

    out = {"version": version, "tag": args.tag or "", "prerelease": str(prerelease).lower()}
    lines = [f"{k}={v}" for k, v in out.items()]
    print("\n".join(lines))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
