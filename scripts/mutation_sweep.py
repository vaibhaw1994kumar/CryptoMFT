#!/usr/bin/env python3
"""Exhaustively mutate one safety idiom at a time and report survivors.

A survivor is a mutation that no test noticed: the idiom at that site is
load-bearing in production but nothing depends on it, so the next edit
can quietly remove it.

Every sweep derives its own site list by scanning the source at run
time. Nothing here hard-codes a line number, so a site added later is
swept automatically and a site removed cannot leave a phantom entry
behind. That property is the whole reason this is safe to commit: the
script cannot go stale, it can only report on whatever is there today.

This is a mutation harness, not a test. It rewrites the module on disk,
so it must not run in CI beside the suite. Run it by hand, or nightly:

    uv run python scripts/mutation_sweep.py fold
    uv run python scripts/mutation_sweep.py lock
    uv run python scripts/mutation_sweep.py --list

A ``SURVIVED`` line names a site whose idiom no test depends on. Either
write the test, or delete the idiom -- but do not leave it as it is.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "src" / "trading_platform_poc" / "execution_controls.py"


@dataclass(frozen=True)
class Site:
    """One occurrence of the idiom under sweep."""

    line_no: int
    occurrence: int
    before: str
    after: str

    @property
    def label(self) -> str:
        suffix = f"#{self.occurrence}" if self.occurrence else ""
        return f"{self.line_no}{suffix}"


@dataclass(frozen=True)
class Sweep:
    """A syntactically enumerable idiom and the way to disable it."""

    name: str
    summary: str
    find: Callable[[str], list[Site]]
    targets: list[str]
    repeats: int = 1
    extra_args: list[str] = field(default_factory=list)
    suffix: str = ""
    """Appended to the module, so line numbers above are untouched."""


def _find_folds(source: str) -> list[Site]:
    """Every ``_normalise_venue(x)`` call, stripped back to ``x``.

    The argument is extracted by matching parentheses rather than by a
    character class, so a nested call like ``_normalise_venue(d.get(a,
    b))`` is swept like any other instead of being set aside. A call
    that spans lines still cannot be rewritten in place and is reported
    so it is never silently dropped from the sweep.
    """
    marker = "_normalise_venue("
    sites: list[Site] = []
    for line_no, line in enumerate(source.splitlines(), start=1):
        if line.lstrip().startswith(("#", '"""', "'''")):
            continue
        if "def _normalise_venue" in line:
            continue
        start = 0
        index = 0
        while True:
            found = line.find(marker, start)
            if found < 0:
                break
            open_at = found + len(marker) - 1
            depth = 0
            close_at = -1
            for position in range(open_at, len(line)):
                if line[position] == "(":
                    depth += 1
                elif line[position] == ")":
                    depth -= 1
                    if depth == 0:
                        close_at = position
                        break
            if close_at < 0:
                sites.append(Site(line_no, index, line, _NESTED))
                break
            argument = line[open_at + 1: close_at]
            sites.append(
                Site(
                    line_no,
                    index,
                    line,
                    line[:found] + argument + line[close_at + 1:],
                )
            )
            start = close_at + 1
            index += 1
    return sites


_SWEPT_LOCK_DEF = '''

class _SweptLock:
    """A lock that guards nothing but still costs two calls."""

    def __enter__(self) -> "_SweptLock":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


_SWEPT_LOCK = _SweptLock()
'''


def _find_locks(source: str) -> list[Site]:
    """Every ``with self._lock:`` guard, stripped of mutual exclusion.

    The replacement is a lock that guards nothing rather than ``if
    True:``, and the difference matters more than it looks. Acquiring a
    lock is itself a point where the interpreter may switch threads, so
    deleting the ``with`` deletes a preemption opportunity and *narrows*
    the very window the sweep is trying to open. A no-op context manager
    keeps both calls, and with them the scheduling, while removing the
    exclusion -- which makes it strictly the more sensitive mutant.
    """
    sites: list[Site] = []
    for line_no, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped != "with self._lock:":
            continue
        indent = line[: len(line) - len(line.lstrip())]
        sites.append(
            Site(line_no, 0, line, f"{indent}with _SWEPT_LOCK:  # swept")
        )
    return sites


_NESTED = "\x00nested\x00"

TESTS = "tests/test_execution_controls.py"

SWEEPS: dict[str, Sweep] = {
    "fold": Sweep(
        name="fold",
        summary="every _normalise_venue call site",
        # The whole suite, not just this module's tests: the fold that
        # keeps a two-leg opportunity from collapsing into one leg is
        # asserted from the opportunity tests, and targeting the
        # narrower file reported it as a survivor it never was.
        find=_find_folds,
        targets=["tests"],
    ),
    "lock": Sweep(
        name="lock",
        summary="every 'with self._lock' acquisition",
        find=_find_locks,
        # The unit suite is single-threaded, so it would report every
        # lock as a survivor and teach nothing. Only tests that start
        # real threads can observe a missing acquisition, and they are
        # selected by marker rather than by name so a concurrency test
        # added later is swept without editing this file.
        targets=[TESTS],
        repeats=3,
        extra_args=["-m", "concurrency", "-p", "no:randomly"],
        suffix=_SWEPT_LOCK_DEF,
    ),
}


def _run(sweep: Sweep) -> bool:
    command = [
        "uv", "run", "pytest", "-q", "--no-header",
        *sweep.extra_args, *sweep.targets,
    ]
    for _ in range(sweep.repeats):
        result = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True,
        )
        if result.returncode != 0:
            return True
    return False


def sweep(name: str) -> int:
    plan = SWEEPS[name]
    original = MODULE.read_text()
    sites = plan.find(original)
    lines = original.splitlines(keepends=True)

    print(f"sweep '{plan.name}': {plan.summary}")
    print(f"{len(sites)} site(s) in {MODULE.relative_to(ROOT)}")
    print(f"targets: {' '.join(plan.targets)} x{plan.repeats}\n")

    backup = Path(tempfile.mkdtemp()) / "execution_controls.py"
    shutil.copy2(MODULE, backup)
    survivors: list[Site] = []
    manual: list[Site] = []
    try:
        for site in sites:
            if site.after is _NESTED:
                manual.append(site)
                print(f"  {site.label:>9}  NESTED (mutate by hand)")
                continue
            index = site.line_no - 1
            ending = "\n" if lines[index].endswith("\n") else ""
            lines[index] = site.after + ending
            MODULE.write_text("".join(lines) + plan.suffix)
            caught = _run(plan)
            lines[index] = site.before + ending
            MODULE.write_text("".join(lines))
            verdict = "CAUGHT" if caught else "SURVIVED"
            print(f"  {site.label:>9}  {verdict}   {site.before.strip()}")
            if not caught:
                survivors.append(site)
    finally:
        shutil.copy2(backup, MODULE)
        if MODULE.read_text() != original:  # pragma: no cover
            raise SystemExit("FATAL: could not restore the module")

    print(f"\n{len(survivors)} survivor(s), {len(manual)} needing hand work")
    for site in survivors:
        print(f"  SURVIVED {site.label}: {site.before.strip()}")
    for site in manual:
        print(f"  NESTED   {site.label}: {site.before.strip()}")
    return 1 if survivors or manual else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sweep", nargs="?", choices=sorted(SWEEPS))
    parser.add_argument(
        "--list", action="store_true", help="show registered sweeps"
    )
    args = parser.parse_args()
    if args.list or not args.sweep:
        for plan in SWEEPS.values():
            found = len(plan.find(MODULE.read_text()))
            print(f"{plan.name:8} {found:>3} sites  {plan.summary}")
        return 0
    return sweep(args.sweep)


if __name__ == "__main__":
    sys.exit(main())
