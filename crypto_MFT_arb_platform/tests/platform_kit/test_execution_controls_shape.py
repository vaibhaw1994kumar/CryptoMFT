"""Enumerate advisory-read-then-act pairs, and hold the set closed.

The priority-ceiling defect was a value read under one lock and acted
on under another. It was found three times: twice by reading the code,
and a third time only after a mechanical pass. Grepping cannot find it
-- the third site never wrote the name of the method it was calling,
because it went through a wrapper -- so the third site survived a fix
that looked complete.

What closes a class of defect is not the count of instances fixed, it
is an enumerator validated on a revision where the answer is known.
This module is that enumerator, plus the two things that keep it
honest:

* a **frozen baseline**, so a new pair fails rather than being
  reported to a human who is not required to read it, and so an entry
  going missing fails too -- an allowlist nobody notices has gone
  stale is worse than none;
* a **positive control**, so the enumerator cannot silently stop
  working. It is run against a deliberately re-broken copy of the
  source and must find the damage.

The baseline is keyed on names, never on line numbers: a baseline that
churns on every commit that moves code gets deleted within a month.

The detector's scope is one frame, deliberately. The invariant it
serves has two clauses -- an advisory read must be paired either with
an authoritative call *in the same acquisition*, or with a *handler for
that call's refusal* -- and only the first is a shape. The second is
behaviour, so it belongs to the suite: the module's one instance of it,
`_draw_down`'s fallback, is held by
`test_a_reservation_released_mid_spend_still_admits` rather than by
interprocedural analysis. Making this detector cross frames would have
reported that site and then told us a handler exists, which was already
true and already insufficient -- while adding exactly the kind of
complexity that lets a detector fail silently.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from trading_platform_poc.execution_controls import ADVISORY_READS

SOURCE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "trading_platform_poc"
    / "execution_controls.py"
)


#: One entry per pair, as ``(function, receiver, advisory, then)``,
#: mapped to **the test that holds its justification**.
#:
#: The holder is the point. An entry is keyed on names only -- no body
#: -- so the key does not move when the body it describes changes. The
#: failure mode is therefore not "someone adds a bad entry", it is "an
#: entry's reason quietly stops being true while the key stays
#: identical". Prose cannot notice that; a named test can.
#:
#: The rot check below is the smaller half of the value. The larger
#: half is that an entry with no nameable holder becomes visible at the
#: moment someone writes it, which is the only moment anyone is
#: thinking about it -- and "what holds this?" is a much easier
#: question to answer honestly than "is this benign?".
#:
#: This does not grow with the module. Its size is bounded by
#: ``len(ADVISORY_READS)`` times the call sites of those names, so on
#: 2,700 lines it is two entries. It grows only when someone
#: deliberately adds a name to a three-element declared set, which is
#: the right place for the friction.
#:
#: If it ever does get noisy, the narrowing to reach for is **lexical
#: scope** -- require the read and the following call to be in the same
#: block. Control-flow structure is a fact about the language, so it
#: removes branch-exclusive false positives without guessing at
#: semantics. Do *not* narrow the receiver predicate back to "calls
#: believed to mutate": that inference was corrected three times, each
#: correction prompted by a miss, and removing it is what made this
#: enumerator trustworthy.
BASELINE: dict[tuple[str, str, str, str], str] = {
    # The fix itself: both calls happen inside one acquisition of the
    # budget's own re-entrant lock, so nothing can land between them.
    # Skipping that acquisition leaves this pair spelled identically,
    # so the holder is a concurrency test, not this file.
    ("try_reserve_within", "self", "above_ceiling", "try_reserve"): (
        "test_a_reservation_and_an_admission_cannot_both_clear_one_"
        "ceiling"
    ),
    # A preflight: it reads and raises, and takes nothing, so there is
    # no action to be atomic with. Held by this enumerator itself --
    # inserting a take here produces a new, unbaselined pair. That was
    # only true once `find_pairs` stopped recording just the first
    # following call.
    ("_preflight", "budget", "_check_ceiling", "available"): (
        "test_the_enumerator_still_finds_the_defect_it_was_built_for"
    ),
}


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _delegation(tree: ast.AST) -> dict[str, int]:
    """Which argument an advisory wrapper actually reads.

    ``self._check_ceiling(budget, ...)`` names ``self``, but the read
    lands on ``budget``. Without resolving that, the receiver of the
    read and the receiver of the action never match and all three
    ledger sites are invisible -- which is exactly how they were
    missed the first time.
    """

    resolved: dict[str, int] = {}
    for fn in _functions(tree):
        if fn.name not in ADVISORY_READS:
            continue
        params = [arg.arg for arg in fn.args.args]
        if params and params[0] == "self":
            params = params[1:]
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ADVISORY_READS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in params
            ):
                resolved[fn.name] = params.index(node.func.value.id)
    return resolved


def _method_calls(fn: ast.AST, delegates: dict[str, int]):
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        name = node.func.attr
        receiver = ast.unparse(node.func.value)
        index = delegates.get(name)
        if index is not None and len(node.args) > index:
            receiver = ast.unparse(node.args[index])
        yield node.lineno, receiver, name


def find_pairs(source: str) -> set[tuple[str, str, str, str]]:
    """Every advisory read followed by an action on the same object.

    Deliberately conservative: *any* later non-advisory call on that
    receiver counts, not only ones a classifier believes to mutate.
    Guessing which calls mutate is what needed correcting three times,
    and a false positive costs one line of baseline while a false
    negative costs a live defect.

    *Every* later call is recorded, not just the first. Stopping at the
    first hides a take placed after a benign call on the same object --
    a `try_reserve` inserted into `_preflight` after its `available()`
    read produced no pair at all, which is the original defect in the
    original shape, silently. Recording them all costs nothing here:
    the pair set on this module is identical either way.
    """

    tree = ast.parse(source)
    delegates = _delegation(tree)
    pairs: set[tuple[str, str, str, str]] = set()
    for fn in _functions(tree):
        calls = sorted(_method_calls(fn, delegates))
        for index, (_line, receiver, name) in enumerate(calls):
            if name not in ADVISORY_READS:
                continue
            for _later, receiver2, name2 in calls[index + 1:]:
                if receiver2 == receiver and name2 not in ADVISORY_READS:
                    pairs.add((fn.name, receiver, name, name2))
    return pairs


def test_no_advisory_read_is_acted_on_outside_its_acquisition() -> None:
    """The set of read-then-act pairs is exactly the reviewed set.

    Failing in *both* directions is deliberate. A new pair is a
    possible ceiling defect and must be justified before it ships. An
    entry that disappears is not a success to be waved through: it
    means the baseline no longer describes the code, and a stale
    allowlist quietly stops protecting anything.
    """

    found = find_pairs(SOURCE.read_text())
    assert found - set(BASELINE) == set(), (
        "new advisory read acted on outside its acquisition -- fold the "
        "pair into one acquisition, or add it to BASELINE with the "
        "reason it is safe"
    )
    assert set(BASELINE) - found == set(), (
        "BASELINE names a pair that no longer exists -- remove the "
        "entry, or the allowlist has gone stale"
    )


def test_the_enumerator_still_finds_the_defect_it_was_built_for() -> None:
    """Positive control: re-break the code and the sweep must see it.

    Without this, the enumerator is a test that passes because it
    finds nothing, and it would keep passing after any change that
    stopped it working -- a renamed wrapper, a lost delegation rule, a
    receiver spelled differently. Every mechanical check in this
    project that lacked a positive control was eventually found to be
    measuring nothing.

    The damage applied here is the original defect: split the ceiling
    test and the reservation back into two separate acquisitions,
    reached through the wrapper that hid them.
    """

    source = SOURCE.read_text()
    folded = """        refusal = budget.try_reserve_within(ceiling, messages)
        if refusal == "ceiling":"""
    split = """        self._check_ceiling(budget, ceiling, messages, priority)
        refusal = "" if budget.try_reserve(messages) else "capacity"
        if refusal == "ceiling":"""
    assert source.count(folded) == 1, "positive control needs updating"

    broken = find_pairs(source.replace(folded, split))
    regression = broken - set(BASELINE)
    assert regression == {
        ("_take", "budget", "_check_ceiling", "try_reserve"),
        ("_take", "budget", "_check_ceiling", "available"),
    }, regression


@pytest.mark.parametrize("name", sorted(ADVISORY_READS))
def test_every_declared_advisory_read_exists(name: str) -> None:
    """A declaration that names a method that is gone protects nothing.

    ``ADVISORY_READS`` is hand-maintained, which is what makes it
    trustworthy and also what makes it rot. If a method is renamed and
    the set is not, the enumerator silently stops covering it and every
    test above still passes.
    """

    tree = ast.parse(SOURCE.read_text())
    assert any(fn.name == name for fn in _functions(tree)), name


def test_the_declaration_is_closed_under_delegation() -> None:
    """A wrapper around an advisory read is itself an advisory read.

    This is the blindness that hid the third ceiling site for a whole
    round. ``ReservationLedger.reserve`` never wrote ``above_ceiling``
    -- it called ``self._check_ceiling(budget, ...)`` -- so a search
    for the name found two of three sites and the fix shipped looking
    complete.

    ``_delegation`` only resolves wrappers already named in
    ``ADVISORY_READS``, so a *new* undeclared wrapper reintroduces
    exactly that blindness, silently and by omission. Today
    ``_check_ceiling`` is the only such function and it is declared,
    which makes the set self-closing by accident. This makes it
    self-closing on purpose.
    """

    tree = ast.parse(SOURCE.read_text())
    undeclared: set[str] = set()
    for fn in _functions(tree):
        params = [arg.arg for arg in fn.args.args]
        if params and params[0] == "self":
            params = params[1:]
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ADVISORY_READS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in params
                and fn.name not in ADVISORY_READS
            ):
                undeclared.add(fn.name)
    assert undeclared == set(), (
        "these forward an advisory read to one of their own arguments, "
        "so callers reach it without naming it -- add them to "
        f"ADVISORY_READS or the enumerator cannot see through them: "
        f"{sorted(undeclared)}"
    )


@pytest.mark.parametrize(
    ("pair", "holder"), sorted(BASELINE.items())
)
def test_every_baselined_pair_names_a_test_that_exists(
    pair: tuple[str, str, str, str], holder: str
) -> None:
    """An exception with no holder is prose, and prose does not fail.

    A baseline entry is keyed on names only, so the key does not move
    when the body it describes changes. The danger is not somebody
    adding a bad entry -- it is a good entry's justification quietly
    ceasing to be true while the key stays identical. Naming the test
    that holds it converts that from unnoticeable into a rename away
    from a failure.

    The rot check is the smaller half. The larger half happens before
    this test ever runs: requiring a holder makes an unheld exception
    visible at the moment it is written, which is the only moment
    anyone is thinking about it.
    """

    names: set[str] = set()
    for path in sorted(pathlib.Path(__file__).parent.glob("test_*.py")):
        for fn in _functions(ast.parse(path.read_text())):
            names.add(fn.name)
    assert holder in names, (
        f"{pair} is excused by {holder!r}, which does not exist -- "
        "either the holder was renamed, or this exception is now held "
        "by nothing"
    )
