"""Validate an acceptance evidence bundle.

The bundle is what ships to a client, so CI has to judge it rather than trust
the exit code of the command that produced it. A suite that silently ran zero
checks, or one whose report cannot name the build it came from, exits zero all
the same.

Standard library only: this also runs on a bare runner against a bundle a
container wrote, where the package itself is not installed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EXPECTED_TOTAL = 14

DELIVERABLES = (
    "venue_capability_matrix",
    "canonical_schemas",
    "security_master",
    "fee_schedule",
    "risk_limit_matrix",
    "order_state_machine",
    "monitoring_and_alerts",
    "feature_catalogue",
)

PLACEHOLDER_VERSION = "0.0.0+local"
PLACEHOLDER_REVISION = "unavailable"


def check(bundle: Path, expect_version: str | None) -> list[str]:
    problems: list[str] = []

    report_json = bundle / "acceptance_report.json"
    if not (bundle / "acceptance_report.md").is_file():
        problems.append("acceptance_report.md is missing")
    if not report_json.is_file():
        problems.append("acceptance_report.json is missing")
        return problems

    report = json.loads(report_json.read_text(encoding="utf-8"))

    total = report.get("total")
    if total != EXPECTED_TOTAL:
        problems.append(f"ran {total} checks, expected {EXPECTED_TOTAL}")
    if not report.get("passed"):
        problems.append(f"failed checks: {report.get('failed')}")

    results = {item.get("test_id"): item for item in report.get("checks", [])}
    for index in range(1, EXPECTED_TOTAL + 1):
        check_id = f"AT-{index:02d}"
        entry = results.get(check_id)
        if entry is None:
            problems.append(f"{check_id} is missing from the report")
        elif not entry.get("passed"):
            problems.append(f"{check_id} did not pass")
        elif not entry.get("owner"):
            problems.append(f"{check_id} names no owner")

    build = report.get("build", {})
    version = build.get("package_version")
    revision = build.get("git_revision")
    if not version or version == PLACEHOLDER_VERSION:
        problems.append(f"package_version is a placeholder: {version!r}")
    if not revision or revision == PLACEHOLDER_REVISION:
        problems.append(f"git_revision is a placeholder: {revision!r}")
    if expect_version is not None and version != expect_version:
        problems.append(
            f"package_version is {version!r}, expected {expect_version!r}"
        )

    # Section 10 requires a signed report: results alone are not one. An empty
    # defect log is a valid answer, a missing key is not, so membership is
    # tested rather than truthiness.
    for section in ("defects", "limitations", "sign_off"):
        if section not in report:
            problems.append(f"report has no {section} section")
    if not report.get("sign_off"):
        problems.append("sign-off table is empty")

    for name in DELIVERABLES:
        if not list((bundle / "deliverables").glob(f"{name}.*")):
            problems.append(f"deliverable {name} is missing")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="directory holding the bundle")
    parser.add_argument(
        "--expect-version",
        default=None,
        help="fail unless the report names this package version",
    )
    args = parser.parse_args()

    problems = check(args.bundle, args.expect_version)
    if problems:
        print(f"{args.bundle} is not a valid evidence bundle:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    report = json.loads(
        (args.bundle / "acceptance_report.json").read_text(encoding="utf-8")
    )
    build = report["build"]
    print(
        f"{report['total']}/{EXPECTED_TOTAL} passed on "
        f"{build['package_version']} @ {build['git_revision'][:12]} "
        f"({len(report['limitations'])} accepted limitations, "
        f"{len(report['defects'])} open defects)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
