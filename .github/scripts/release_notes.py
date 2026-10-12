"""Extract one version's section from CHANGELOG.md.

A release whose notes were generated from commit subjects says what changed in
the repository; the changelog says what changed for whoever installs it. The
second is the one worth publishing, and keeping it as the single source means
a release cannot describe something the changelog does not.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HEADING = re.compile(r"^##\s+(?P<version>\S+)")


def extract(changelog: str, version: str) -> str | None:
    lines = changelog.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        match = HEADING.match(line)
        if match is None:
            continue
        if start is not None:
            return "\n".join(lines[start:index]).strip()
        if match.group("version") == version:
            start = index + 1
    if start is None:
        return None
    return "\n".join(lines[start:]).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="version to extract, without a leading v")
    parser.add_argument(
        "--changelog", type=Path, default=Path("CHANGELOG.md"), help="changelog path"
    )
    args = parser.parse_args()

    notes = extract(args.changelog.read_text(encoding="utf-8"), args.version)
    if not notes:
        print(
            f"{args.changelog} has no '## {args.version}' section. "
            "Add one before tagging.",
            file=sys.stderr,
        )
        return 1
    print(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
