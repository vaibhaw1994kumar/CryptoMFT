from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import heapq
import json
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import load_config
from .models import BookState, EventType, MarketEvent, canonical_json, json_default, object_hash, utc_now_iso
from .orderbook import OrderBook
from .replay import ORDERING_RULE, event_order_key


@dataclass(frozen=True, slots=True)
class VenueEligibility:
    venue: str
    instrument_id: str
    book_state: BookState
    last_event_id: str
    last_receive_ts_ns: int | None
    age_ns: int | None
    valid: bool
    reason: str


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    eligible: bool
    evaluated_ts_ns: int
    instrument_id: str
    inputs: tuple[VenueEligibility, ...]
    reasons: tuple[str, ...]
    fingerprint: str


class CrossVenueEligibilityPolicy:
    """One validity/freshness rule shared by signal creation and OMS routing."""

    def __init__(
        self,
        required_venues: tuple[str, ...],
        instrument_id: str,
        max_age_ns: int,
    ):
        self.required_venues = required_venues
        self.instrument_id = instrument_id
        self.max_age_ns = max_age_ns
        self.evaluations = 0
        self.eligible_evaluations = 0
        self.blocked_evaluations = 0

    def evaluate(
        self,
        books: dict[tuple[str, str], OrderBook],
        now_ns: int,
    ) -> EligibilityDecision:
        inputs: list[VenueEligibility] = []
        reasons: list[str] = []
        for venue in self.required_venues:
            book = books.get((venue, self.instrument_id))
            if book is None:
                item = VenueEligibility(
                    venue, self.instrument_id, BookState.SYNCING, "", None, None,
                    False, "missing_required_book",
                )
            else:
                book.mark_stale(now_ns, self.max_age_ns)
                age = (
                    now_ns - book.last_receive_ts_ns
                    if book.last_receive_ts_ns is not None else None
                )
                valid = (
                    book.state == BookState.VALID
                    and age is not None
                    and 0 <= age <= self.max_age_ns
                )
                if book.state != BookState.VALID:
                    reason = f"book_{book.state.value.lower()}"
                elif age is None:
                    reason = "missing_receive_timestamp"
                elif age < 0:
                    reason = "future_book_timestamp"
                elif age > self.max_age_ns:
                    reason = "book_over_age_limit"
                else:
                    reason = ""
                item = VenueEligibility(
                    venue=venue,
                    instrument_id=self.instrument_id,
                    book_state=book.state,
                    last_event_id=book.last_event_id or "",
                    last_receive_ts_ns=book.last_receive_ts_ns,
                    age_ns=age,
                    valid=valid,
                    reason=reason,
                )
            inputs.append(item)
            if not item.valid:
                reasons.append(f"{venue}:{item.reason}")
        fingerprint_payload = {
            "policy": "cross-venue-eligibility-v1",
            "instrument_id": self.instrument_id,
            "required_venues": self.required_venues,
            "max_age_ns": self.max_age_ns,
            "evaluated_ts_ns": now_ns,
            "inputs": [asdict(item) for item in inputs],
        }
        fingerprint = sha256(canonical_json(fingerprint_payload).encode()).hexdigest()
        eligible = not reasons
        self.evaluations += 1
        self.eligible_evaluations += int(eligible)
        self.blocked_evaluations += int(not eligible)
        return EligibilityDecision(
            eligible, now_ns, self.instrument_id, tuple(inputs), tuple(reasons), fingerprint
        )


class CrossVenueStreamMerger:
    """Stable k-way merge that yields the original immutable event objects."""

    def merge(
        self, streams: dict[str, Iterable[MarketEvent]]
    ) -> Iterator[MarketEvent]:
        iterators: dict[str, Iterator[MarketEvent]] = {
            venue: iter(streams[venue]) for venue in sorted(streams)
        }
        heap: list[tuple[tuple[int | str, ...], str, MarketEvent]] = []
        for venue, iterator in iterators.items():
            event = next(iterator, None)
            if event is not None:
                self._validate_source(venue, event)
                heapq.heappush(heap, (event_order_key(event), venue, event))
        while heap:
            _, venue, event = heapq.heappop(heap)
            yield event
            following = next(iterators[venue], None)
            if following is not None:
                self._validate_source(venue, following)
                heapq.heappush(heap, (event_order_key(following), venue, following))

    @staticmethod
    def _validate_source(venue: str, event: MarketEvent) -> None:
        if event.venue != venue:
            raise ValueError(f"stream {venue} contained event for {event.venue}")


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def align_canonical_files(
    config_path: Path,
    venue_files: dict[str, Path],
    output_path: Path,
) -> dict[str, Any]:
    cfg, _ = load_config(config_path)
    required = set(cfg.venues)
    if set(venue_files) != required:
        raise ValueError(
            f"alignment requires exactly {sorted(required)}; received {sorted(venue_files)}"
        )
    streams = {}
    input_counts = {}
    for venue, path in sorted(venue_files.items()):
        with path.open(encoding="utf-8") as handle:
            events = [MarketEvent.from_dict(json.loads(line)) for line in handle if line.strip()]
        streams[venue] = events
        input_counts[venue] = len(events)

    books = {
        (venue, cfg.instrument_id): OrderBook(venue, cfg.instrument_id)
        for venue in cfg.venues
    }
    policy = CrossVenueEligibilityPolicy(cfg.venues, cfg.instrument_id, cfg.max_book_age_ns)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path = output_path.with_suffix(output_path.suffix + ".eligibility.jsonl")
    native_hashes: dict[str, str] = {}
    output_count = 0
    with output_path.open("w", encoding="utf-8") as output, trace_path.open("w", encoding="utf-8") as trace:
        for event in CrossVenueStreamMerger().merge(streams):
            native_before = object_hash(event.native)
            if event.event_type in (EventType.BOOK_SNAPSHOT, EventType.BOOK_DELTA):
                books[(event.venue, event.instrument_id)].apply(event)
            decision = policy.evaluate(books, event.receive_ts_ns)
            if object_hash(event.native) != native_before:
                raise RuntimeError("alignment mutated native source state")
            native_hashes[event.event_id] = native_before
            output.write(json.dumps(asdict(event), default=json_default, sort_keys=True) + "\n")
            trace.write(json.dumps(asdict(decision), default=json_default, sort_keys=True) + "\n")
            output_count += 1

    stable_result = {
        "input_counts": input_counts,
        "output_count": output_count,
        "output_sha256": _file_sha256(output_path),
        "eligibility_trace_sha256": _file_sha256(trace_path),
        "native_state_hashes": native_hashes,
        "ordering_rule": list(ORDERING_RULE),
        "eligible_evaluations": policy.eligible_evaluations,
        "blocked_evaluations": policy.blocked_evaluations,
    }
    report = {
        "schema_version": "1.0.0",
        "created_at": utc_now_iso(),
        "inputs": {venue: str(path) for venue, path in sorted(venue_files.items())},
        "input_sha256": {venue: _file_sha256(path) for venue, path in sorted(venue_files.items())},
        "output": str(output_path),
        "eligibility_trace": str(trace_path),
        **stable_result,
        "deterministic_alignment_sha256": sha256(canonical_json(stable_result).encode()).hexdigest(),
    }
    report_path = output_path.with_suffix(output_path.suffix + ".alignment.json")
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report
