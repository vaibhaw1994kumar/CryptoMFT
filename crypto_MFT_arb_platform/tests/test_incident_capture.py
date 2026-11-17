"""W2b (owner 17 Nov; audit A2 2.1-2.3, Q3): incident capture in the live recorder. A ring of marks (file sizes and a copy of each venue's book every few seconds), a trigger table, a window cut into a
small recording of its own (`part-000001` in the recorder's layout, the venue books at the window start as labelled seed snapshots, a manifest with rule, window, frame counts, hashes and completeness),
and the same cut made offline from a recording that already exists. SYNTHETIC frames: wiring only, never a profit figure."""
import gzip
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import crypto_stat_arb.live_feed as lf
from crypto_stat_arb.live_feed import INCIDENT_RULES, SEED_LABEL, IncidentCapture, LiveFeedSettings, RotatingLiveRecorder, cut_recording_window, incident_policy
from crypto_stat_arb.models import BookState, MarketEvent
from crypto_stat_arb.orderbook import OrderBook

T0 = 1_700_000_000_000_000_000
STEP = 50_000_000                       # one frame every 50 ms, the two venues in turn
INSTRUMENT = "SOL-USDT-PERP"
POLICY = {"pre_seconds": 3.0, "post_seconds": 1.0, "mark_seconds": 0.5, "cooldown_seconds": 2.0, "max_captures": 3, "min_free_bytes": 0, "background": False}


class Live:
    """A recorder, two real books and the incident ring, driven frame by frame as the feed service drives them: record the event, apply it to the book, then tick the ring."""

    def __init__(self, root: Path, policy=None, **recorder):
        clock = iter(range(T0, T0 + 10 ** 12, 1000))
        self.rec = RotatingLiveRecorder(root, "live", rotate_seconds=3600.0, rotate_bytes=recorder.pop("rotate_bytes", 256 * 1024 * 1024), durable=False, clock_ns=lambda: next(clock),
                                        flush_every=200, flush_interval_ns=250_000_000)
        self.books = {v: OrderBook(v, INSTRUMENT) for v in ("BINANCE", "BYBIT")}
        self.inc = IncidentCapture(self.rec.session_dir, incident_policy({**POLICY, **(policy or {})}), instrument_id=INSTRUMENT, venues=("BINANCE", "BYBIT"), files=self.rec.open_files,
                                   books=lambda: self.books, flush=self.rec.flush_now, session_id="live")
        self.i = 0
        self.last = {"BINANCE": None, "BYBIT": None}
        self.hash_at: dict[tuple[str, int], str] = {}
        self.ts = T0

    def event(self, venue: str, ts: int) -> dict:
        n = self.i
        if self.last[venue] is None:                             # the first frame of a venue is its snapshot
            seq = 100 if venue == "BINANCE" else 1
            row = {"event_type": "BOOK_SNAPSHOT", "sequence": seq, "sequence_start": seq, "bids": [{"price": "100.00", "quantity": "5"}, {"price": "99.99", "quantity": "7"}],
                   "asks": [{"price": "100.10", "quantity": "5"}, {"price": "100.11", "quantity": "7"}]}
        elif venue == "BINANCE":                                 # USD-M: the first id is NOT last + 1; continuity is pu == the last applied id (the first delta after a snapshot bridges it)
            last = self.last[venue]
            first = last if not self.books[venue].chained_delta else last + 2
            seq = first + 2
            row = {"event_type": "BOOK_DELTA", "sequence": seq, "sequence_start": first, "native": {"pu": last - 1 if not self.books[venue].chained_delta else last},
                   "bids": [{"price": "100.00", "quantity": str(1 + n % 5)}], "asks": [{"price": "100.10", "quantity": str(2 + n % 3)}]}
        else:
            seq = self.last[venue] + 1
            row = {"event_type": "BOOK_DELTA", "sequence": seq, "sequence_start": seq, "bids": [{"price": "99.99", "quantity": str(1 + n % 4)}], "asks": [{"price": "100.11", "quantity": str(3 + n % 2)}]}
        self.last[venue] = row["sequence"]
        return {"event_id": f"{venue}-{n}", "venue": venue, "instrument_id": INSTRUMENT, "exchange_ts_ns": ts - 1_000_000, "source_ts_ns": ts - 1_000_000, "receive_ts_ns": ts, **row}

    def frame(self, tick: bool = True) -> int:
        venue = "BINANCE" if self.i % 2 == 0 else "BYBIT"
        ts = self.ts = T0 + self.i * STEP
        row = self.event(venue, ts)
        wire = self.rec.capture_wire(venue=venue, channel="depth", host="h", transport="WEBSOCKET", receive_ts_ns=ts, original_frame=json.dumps({"n": self.i}))
        raw = self.rec.append_payload(wire, payload={"n": self.i}, vendor=venue.lower(), adapter_version="t")
        ev = MarketEvent.from_dict({**row, "raw_ref": raw.raw_ref})
        self.rec.record_event(wire, raw, ev, self.i)
        state = self.books[venue].apply(ev)
        assert state == BookState.VALID, (venue, self.i, state, self.books[venue].invalid_reason)
        self.hash_at[(venue, ts)] = self.books[venue].snapshot_hash()
        if tick:
            self.inc.tick(ts)
        self.i += 1
        return ts

    def run(self, frames: int, tick: bool = True) -> int:
        for _ in range(frames):
            ts = self.frame(tick)
        return ts

    def persisted(self, name: str, start_ns: int, end_ns: int) -> list[bytes]:
        """The session's own rows of a stream with start <= receive time <= end, across every part: what the recorder put on disk."""
        self.rec.flush_now()
        out = []
        for part in sorted(self.rec.session_dir.glob("part-*")):
            path = part / f"{name}.jsonl"
            if path.exists():
                out += [line for line in path.read_bytes().splitlines(keepends=True) if start_ns < json.loads(line)["receive_ts_ns"] <= end_ns]
        return out


def manifest_of(live: Live, index: int = 0) -> tuple[dict, Path]:
    directory = Path(live.inc.captures[index]["directory"])
    return json.loads((directory / "incident_manifest.json").read_text()), directory


def replay_books(directory: Path) -> tuple[dict, list]:
    from new_backtest.session_replay import iter_recorded_events
    books, events = {}, []
    for ev in iter_recorded_events(directory):
        book = books.setdefault(ev.venue, OrderBook(ev.venue, ev.instrument_id))
        events.append((ev, book.apply(ev)))
    return books, events


class Policy(unittest.TestCase):
    def test_off_by_default_and_the_block_is_validated(self):
        self.assertIsNone(LiveFeedSettings().incident_capture)
        self.assertIsNone(incident_policy(None))
        self.assertIsNone(incident_policy({"enabled": False, "pre_seconds": 90}))
        policy = incident_policy({"enabled": True})
        self.assertEqual((policy["pre_seconds"], policy["post_seconds"], policy["mark_seconds"]), (90.0, 5.0, 5.0))              # the audit's minimums are the defaults
        self.assertTrue(all(policy["rules"][r] for r in INCIDENT_RULES if "source" not in INCIDENT_RULES[r]))
        self.assertFalse(policy["rules"]["markout_worse_than"])                       # named in the table, not built: nothing can raise it
        self.assertFalse(policy["rules"]["venue_dislocation"])
        self.assertFalse(incident_policy({"rules": {"manual": False}})["rules"]["manual"])
        for bad in ({"pre_secs": 1}, {"rules": {"nonsense": True}}, {"pre_seconds": 1.0, "mark_seconds": 2.0}, {"post_seconds": -1}):
            with self.assertRaises(ValueError):
                incident_policy(bad)

    def test_v1_12_is_v1_11_plus_the_block_and_no_earlier_profile_has_it(self):
        from crypto_stat_arb.config import incident_capture_from_profile
        from new_backtest.profiles import load_profile
        a, b = load_profile("perp_perp_maker_taker_v1_11"), load_profile("perp_perp_maker_taker_v1_12")
        body = lambda doc, *skip: {k: v for k, v in doc.items() if not k.startswith("_") and k not in ("name", "description", *skip)}
        self.assertEqual(body(b, "incident_capture"), body(a))
        self.assertIsNone(incident_capture_from_profile(a))
        self.assertIsNone(incident_capture_from_profile(load_profile("perp_perp_maker_taker_v1_10")))
        policy = incident_capture_from_profile(b)
        self.assertEqual((policy["pre_seconds"], policy["post_seconds"]), (90.0, 5.0))
        self.assertTrue(policy["rules"]["breaker_trip"])


class WindowCut(unittest.TestCase):
    def test_a_trigger_cuts_the_window_with_the_persisted_rows_the_seed_books_and_a_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            live = Live(Path(d))
            trigger_ts = live.run(200)                                               # 10 s of frames
            self.assertEqual(live.inc.trigger("breaker_trip", trigger_ts, detail="test"), live.inc.pending["incident_id"])
            self.assertEqual(live.inc.captures, [])                                  # nothing is cut until the post window has passed
            live.run(60)
            self.assertEqual(len(live.inc.captures), 1)
            m, directory = manifest_of(live)
            w = m["window"]
            self.assertEqual((m["completeness"], m["incomplete_reasons"], m["rule"]), ("COMPLETE", [], "breaker_trip"))
            self.assertGreaterEqual(w["pre_seconds"], 3.0)                           # at least what was asked, at most one mark more
            self.assertLessEqual(w["pre_seconds"], 3.0 + 0.5 + 1e-9)
            self.assertGreaterEqual(w["post_seconds"], 1.0)
            # the ring's rows ARE the persisted rows of the interval: equal lines, equal hash, for every stream the session wrote
            for name in ("wire", "raw", "canonical", "trace"):
                rows = live.persisted(name, w["start_ns"], w["end_ns"])
                self.assertEqual(m["frame_counts"][name], len(rows), name)
                self.assertEqual(m["files"][name]["copied_sha256"], hashlib.sha256(b"".join(rows)).hexdigest(), name)
                body = (directory / "part-000001" / f"{name}.jsonl").read_bytes().splitlines(keepends=True)
                self.assertEqual(body[m["files"][name]["seed_rows"]:], rows, name)
                self.assertEqual(m["files"][name]["sha256"], hashlib.sha256(b"".join(body)).hexdigest(), name)
            self.assertEqual((m["files"]["canonical"]["seed_rows"], m["files"]["trace"]["seed_rows"], m["files"]["wire"]["seed_rows"]), (2, 2, 0))
            part = json.loads((directory / "part-000001" / "live_part_manifest.json").read_text())
            self.assertEqual({n: f["sha256"] for n, f in part["files"].items()}, {n: f["sha256"] for n, f in m["files"].items()})
            self.assertIn("first_previous_record_hash", m["wire_chain"])             # a cut of the hash chain, and it says so
            # the seeds: a labelled COPY of each venue's real book at the first mark; Binance's is bridged to its next delta, its true sequence kept
            for venue in ("BINANCE", "BYBIT"):
                self.assertEqual(m["seeds"][venue]["status"], SEED_LABEL)
                self.assertEqual(m["seeds"][venue]["snapshot_hash"], live.hash_at[(venue, max(ts for (v, ts) in live.hash_at if v == venue and ts <= w["start_ns"]))])
            first_delta = next(json.loads(line) for line in live.persisted("canonical", w["start_ns"], w["end_ns"]) if json.loads(line)["venue"] == "BINANCE")
            self.assertEqual(m["seeds"]["BINANCE"]["book_sequence"], first_delta["native"]["pu"])
            self.assertEqual(m["seeds"]["BINANCE"]["sequence_written"], first_delta["sequence_start"] - 1)
            self.assertGreater(m["seeds"]["BINANCE"]["sequence_written"], m["seeds"]["BINANCE"]["book_sequence"])
            self.assertEqual(m["seeds"]["BYBIT"]["sequence_written"], m["seeds"]["BYBIT"]["book_sequence"])
            # the window opens in the session replay's reader with no change, the seeds come first and are labelled, and every event applies VALID to the same book the live session had
            books, events = replay_books(directory)
            self.assertEqual([ev.quality_flags for ev, _ in events[:2]], [(SEED_LABEL,), (SEED_LABEL,)])
            self.assertTrue(all(state == BookState.VALID for _, state in events))
            for venue, book in books.items():
                last_ts = max(ev.receive_ts_ns for ev, _ in events if ev.venue == venue)
                replayed = OrderBook(venue, INSTRUMENT)
                replayed.bids, replayed.asks, replayed.sequence, replayed.state = book.bids, book.asks, live_sequence(live, venue, last_ts), book.state
                self.assertEqual(replayed.snapshot_hash(), live.hash_at[(venue, last_ts)], venue)        # the same levels as the live book after the same event
            self.assertEqual(live.inc.report()["counts"]["captures"], 1)

    def test_a_window_that_spans_a_part_rotation_is_cut_from_both_parts(self):
        with tempfile.TemporaryDirectory() as d:
            live = Live(Path(d), rotate_bytes=60_000)
            trigger_ts = live.run(200)
            live.inc.trigger("manual", trigger_ts)
            live.run(60)
            m, directory = manifest_of(live)
            self.assertGreater(live.rec.part_number, 2)
            self.assertGreater(len({r["file"].split("/")[0] for r in m["source"]["ranges"]["canonical"]}), 1)         # more than one part of the session fed the window
            self.assertEqual(m["completeness"], "COMPLETE")
            for name in ("wire", "canonical", "trace"):
                rows = live.persisted(name, m["window"]["start_ns"], m["window"]["end_ns"])
                self.assertEqual(m["files"][name]["copied_sha256"], hashlib.sha256(b"".join(rows)).hexdigest(), name)
            _, events = replay_books(directory)
            self.assertTrue(all(state == BookState.VALID for _, state in events))

    def test_a_window_shorter_than_asked_is_incomplete_and_says_why(self):
        with tempfile.TemporaryDirectory() as d:
            live = Live(Path(d))
            ts = live.run(20)                                                       # 1 s of session: 3 s before the trigger do not exist
            live.inc.trigger("kill_switch", ts)
            live.run(10)                                                            # 0.5 s later the session ends: the post window is short too
            live.inc.close(live.ts)
            m, _ = manifest_of(live)
            self.assertEqual(m["completeness"], "INCOMPLETE")
            self.assertEqual(sorted(r.split(":")[0] for r in m["incomplete_reasons"]), ["POST_WINDOW_SHORT", "PRE_WINDOW_SHORT"])
            self.assertGreater(m["frame_counts"]["canonical"], 0)                    # what exists is still kept

    def test_disk_pressure_a_write_error_and_a_stalled_loop_are_never_silent(self):
        with tempfile.TemporaryDirectory() as d:
            live = Live(Path(d), policy={"min_free_bytes": 1 << 62})
            live.inc.trigger("manual", live.run(100))
            live.run(40)
            m, directory = manifest_of(live)
            self.assertEqual((m["completeness"], m["incomplete_reasons"][0].split(":")[0]), ("INCOMPLETE", "DISK_PRESSURE"))
            self.assertFalse((directory / "part-000001").exists())                   # nothing was copied; the manifest is the record
        with tempfile.TemporaryDirectory() as d:
            live = Live(Path(d))
            live.inc.trigger("manual", live.run(100))
            with mock.patch.object(lf, "write_incident", side_effect=OSError(28, "No space left on device")):
                live.run(40)
            m, _ = manifest_of(live)
            self.assertEqual((m["completeness"], m["incomplete_reasons"][0].split(":")[0]), ("INCOMPLETE", "WRITE_ERROR"))
            self.assertIn("No space left", m["incomplete_reasons"][0])
        with tempfile.TemporaryDirectory() as d:
            live = Live(Path(d))
            live.run(100)
            live.run(80, tick=False)                                                # 4 s in which the ring was not served (a stalled loop): frames were recorded, no mark was taken
            live.inc.trigger("manual", live.run(4))
            live.run(40)
            m, _ = manifest_of(live)
            self.assertEqual((m["completeness"], m["incomplete_reasons"][0].split(":")[0]), ("INCOMPLETE", "MARK_GAP"))
            self.assertGreater(live.inc.report()["max_mark_gap_s"], 3.9)

    def test_triggers_join_an_open_window_and_cooldown_limit_and_disabled_rules_are_counted(self):
        with tempfile.TemporaryDirectory() as d:
            live = Live(Path(d), policy={"rules": {"limit_stop": False}, "max_captures": 2})
            first = live.inc.trigger("breaker_trip", live.run(100))
            self.assertEqual(live.inc.trigger("kill_switch", live.run(4)), first)     # inside the open window: the same incident
            self.assertIsNone(live.inc.trigger("limit_stop", live.ts))                # switched off in the block
            self.assertIsNone(live.inc.trigger("no_such_rule", live.ts))
            live.run(40)
            m, _ = manifest_of(live)
            self.assertEqual([t["rule"] for t in m["triggers"]], ["breaker_trip", "kill_switch"])
            self.assertIsNone(live.inc.trigger("manual", live.run(4)))                # within 2 s of the last capture
            second = live.inc.trigger("manual", live.run(60))
            self.assertIsNotNone(second)
            live.run(40)
            self.assertIsNone(live.inc.trigger("manual", live.run(80)))               # the session's limit of two captures
            counts = live.inc.report()["counts"]
            self.assertEqual((counts["captures"], counts["joined_pending"], counts["suppressed_disabled"], counts["unknown_rule"], counts["suppressed_cooldown"], counts["suppressed_limit"]), (2, 1, 1, 1, 1, 1))
            self.assertEqual(counts["triggers"], 7)

    def test_ledger_events_raise_their_rules_and_only_the_states_named(self):
        with tempfile.TemporaryDirectory() as d:
            live = Live(Path(d), policy={"cooldown_seconds": 0.0, "max_captures": 50})
            live.run(100)
            cases = [({"event_type": "FEED_HEALTH_STATE", "state": "DEGRADED"}, None), ({"event_type": "FEED_HEALTH_STATE", "state": "UNSAFE"}, "feed_unsafe"), ({"event_type": "LIMIT_STATE", "state": "CLEARED"}, None),
                     ({"event_type": "LIMIT_STATE", "state": "ACTIVE"}, "limit_stop"), ({"event_type": "KILL_SWITCH_STATE_TRANSITION", "state": "INACTIVE"}, None),
                     ({"event_type": "KILL_SWITCH_STATE_TRANSITION", "state": "ACTIVE"}, "kill_switch"), ({"event_type": "BREAKER_TRIP"}, "breaker_trip"), ({"event_type": "HEDGE_ALARM"}, "hedge_unfilled"),
                     ({"event_type": "HEDGE_LATE_FILL"}, "ghost_hedge"), ({"event_type": "NEW_RISK_HELD_HEDGE_UNKNOWN"}, "unknown_order_state"), ({"event_type": "RECONCILIATION_DISAGREEMENT"}, "reconciliation_mismatch"),
                     ({"event_type": "FILL"}, None)]
            for row, rule in cases:
                before = dict(live.inc.by_rule)
                live.inc.on_ledger_event({**row, "ts_ns": live.ts})
                raised = [r for r in live.inc.by_rule if live.inc.by_rule[r] != before.get(r, 0)]
                self.assertEqual(raised, [rule] if rule else [], row)
                if rule:
                    live.run(30)                                                    # past the post window: the capture is cut and the next trigger opens a new one
            rules = [c["rule"] for c in live.inc.captures]
            self.assertEqual(rules, ["feed_unsafe", "limit_stop", "kill_switch", "breaker_trip", "hedge_unfilled", "ghost_hedge", "unknown_order_state", "reconciliation_mismatch"])
            self.assertTrue(all(c["completeness"] == "COMPLETE" for c in live.inc.captures))

    def test_the_manual_trigger_file_is_taken_once_and_kept(self):
        with tempfile.TemporaryDirectory() as d:
            live = Live(Path(d))
            live.run(100)
            live.inc.dir.mkdir(parents=True, exist_ok=True)
            (live.inc.dir / "TRIGGER").write_text("operator: look at this")
            live.inc.check_trigger_file(live.ts)
            live.inc.check_trigger_file(live.ts)
            self.assertEqual(live.inc.by_rule, {"manual": 1})
            self.assertEqual(live.inc.pending["triggers"][0]["detail"], "operator: look at this")
            self.assertEqual(len(list(live.inc.dir.glob("TRIGGER.taken-*"))), 1)

    def test_a_copy_in_the_background_gives_the_same_window(self):
        with tempfile.TemporaryDirectory() as d:
            live = Live(Path(d), policy={"background": True})
            live.inc.trigger("manual", live.run(200))
            live.run(60)
            live.inc.wait()
            m, _ = manifest_of(live)
            rows = live.persisted("canonical", m["window"]["start_ns"], m["window"]["end_ns"])
            self.assertEqual((m["completeness"], m["files"]["canonical"]["copied_sha256"]), ("COMPLETE", hashlib.sha256(b"".join(rows)).hexdigest()))


def live_sequence(live: Live, venue: str, ts: int) -> int:
    """The live book's sequence after the event it applied at `ts` (read back from the session's own canonical rows)."""
    for line in live.persisted("canonical", ts - 1, ts):
        row = json.loads(line)
        if row["venue"] == venue:
            return row["sequence"]
    raise AssertionError((venue, ts))


class OfflineCut(unittest.TestCase):
    """The same incident layout cut from a recording that already exists (gzip parts), read-only on it; the seed is the book rebuilt from the recording's own events."""

    def _recording(self, root: Path) -> Live:
        live = Live(root)
        live.run(400)
        live.rec.close({})
        for path in list(live.rec.session_dir.glob("part-*/*.jsonl")):               # as the stored captures are: gzip files
            with gzip.open(path.with_name(path.name + ".gz"), "wb") as fh:
                fh.write(path.read_bytes())
            path.unlink()
        return live

    def test_the_cut_holds_the_window_rows_and_rebuilt_seeds_and_the_source_is_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            live = self._recording(Path(d) / "rec")
            session = live.rec.session_dir
            before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in session.rglob("*") if p.is_file()}
            trigger = T0 + 200 * STEP
            m = cut_recording_window(session, Path(d) / "cut", trigger, pre_seconds=4.0, post_seconds=2.0, rule="feed_unsafe", detail="test stall")
            self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in session.rglob("*") if p.is_file()})          # read-only on the recording
            self.assertEqual((m["completeness"], m["rule"], m["venues"], m["instrument_id"]), ("COMPLETE", "feed_unsafe", ["BINANCE", "BYBIT"], INSTRUMENT))
            self.assertEqual(m["frame_counts"]["canonical"], 121)                    # 6 s of frames every 50 ms, both ends included
            self.assertEqual(m["seeds"]["BYBIT"]["source"], "rebuilt from the recording's own canonical events up to the window start")
            books, events = replay_books(Path(d) / "cut")
            self.assertTrue(all(state == BookState.VALID for _, state in events))
            for venue, book in books.items():
                last_ts = max(ev.receive_ts_ns for ev, _ in events if ev.venue == venue)
                check = OrderBook(venue, INSTRUMENT)
                check.bids, check.asks, check.state = book.bids, book.asks, book.state
                check.sequence = next(ev.sequence for ev, _ in reversed(events) if ev.venue == venue)
                self.assertEqual(check.snapshot_hash(), live.hash_at[(venue, last_ts)], venue)           # the rebuilt seed plus the window gives the live book
            with self.assertRaises(FileExistsError):
                cut_recording_window(session, Path(d) / "cut", trigger, pre_seconds=4.0, post_seconds=2.0)        # an incident is never written over

    def test_the_incident_audit_checks_a_window_by_one_call_and_against_its_source(self):
        """Audit A2 2.3: diagnostics callable on a window. Manifest, file hashes, the wire records, the trace references, the books from the seed; with the source recording, event for event equal to it."""
        from crypto_stat_arb.session_audit import audit_incident
        with tempfile.TemporaryDirectory() as d:
            session = self._recording(Path(d) / "rec").rec.session_dir
            cut = Path(d) / "cut"
            cut_recording_window(session, cut, T0 + 200 * STEP, pre_seconds=4.0, post_seconds=2.0)
            audit = audit_incident(cut, Path(d) / "audit", source_session_dir=session)
            self.assertTrue(audit["ok"], audit["checks"])
            self.assertEqual(sorted(audit["checks"]), ["books", "files", "manifest", "source_books", "trace", "wire"])
            self.assertEqual((audit["checks"]["source_books"]["events_compared"], audit["checks"]["source_books"]["first_difference"]), (121, None))
            self.assertEqual(audit["checks"]["books"]["first_event_after_seed"], {"BINANCE": "VALID", "BYBIT": "VALID"})
            self.assertEqual(audit["checks"]["wire"]["records"], 121)
            self.assertEqual(json.loads((Path(d) / "audit" / "incident_audit.json").read_text())["ok"], True)
            with self.assertRaises(ValueError):
                audit_incident(cut, cut / "audit")                                    # never written inside the incident
            # one changed character in a window file is found; a seed that does not chain is found (and cannot pass as a real gap without the source)
            canonical = cut / "part-000001" / "canonical.jsonl"
            original = canonical.read_bytes()
            canonical.write_bytes(original.replace(b'"quantity": "5"', b'"quantity": "6"', 1))
            self.assertEqual(audit_incident(cut)["checks"]["files"]["bad"], {"canonical": "sha256 or row count differs from the manifest"})
            rows = original.splitlines(keepends=True)
            seed = json.loads(rows[0])
            seed["sequence"] = seed["sequence_start"] = seed["sequence"] - 50
            canonical.write_bytes(json.dumps(seed, sort_keys=True).encode() + b"\n" + b"".join(rows[1:]))
            broken = audit_incident(cut, source_session_dir=session)
            self.assertFalse(broken["ok"])
            self.assertFalse(broken["checks"]["books"]["ok"])
            self.assertFalse(broken["checks"]["source_books"]["ok"])                 # the source has no gap there: the seed is at fault, and the audit says so
            self.assertIsNotNone(broken["checks"]["source_books"]["first_difference"])
            wire = cut / "part-000001" / "wire.jsonl"
            wire.write_bytes(wire.read_bytes().replace(b'"n": 2', b'"n": 3', 1) if b'"n": 2' in wire.read_bytes() else wire.read_bytes()[:-20] + b"x" * 19 + b"\n")
            self.assertFalse(audit_incident(cut)["checks"]["wire"]["ok"])
            self.assertFalse(audit_incident(Path(d) / "nothing")["ok"])

    def test_a_window_past_either_end_of_the_recording_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            session = self._recording(Path(d) / "rec").rec.session_dir
            early = cut_recording_window(session, Path(d) / "early", T0 + 20 * STEP, pre_seconds=4.0, post_seconds=1.0)
            self.assertEqual([r.split(":")[0] for r in early["incomplete_reasons"]], ["PRE_WINDOW_SHORT"])
            self.assertEqual(early["seeds"]["BINANCE"]["status"], "NO_VALID_BOOK_AT_WINDOW_START")       # no book existed before the recording: none is invented
            late = cut_recording_window(session, Path(d) / "late", T0 + 390 * STEP, pre_seconds=4.0, post_seconds=5.0)
            self.assertEqual([r.split(":")[0] for r in late["incomplete_reasons"]], ["POST_WINDOW_SHORT"])
            with self.assertRaises(FileNotFoundError):
                cut_recording_window(Path(d) / "nothing", Path(d) / "x", T0)

    def test_a_cut_window_runs_in_the_session_replay_and_a_replay_never_writes_into_it(self):
        import new_backtest.session_replay as sr
        from new_backtest.profiles import apply_profile, load_profile
        slices = Path(__file__).resolve().parents[2] / "out" / "queue_v2" / "slices_mar2024"
        if not slices.is_dir():
            self.skipTest("March slices not available (the profile needs a data root)")
        with tempfile.TemporaryDirectory() as d:
            session = self._recording(Path(d) / "rec").rec.session_dir
            cut = Path(d) / "cut"
            cut_recording_window(session, cut, T0 + 200 * STEP, pre_seconds=4.0, post_seconds=2.0)
            profile = load_profile("perp_perp_maker_taker_v1_10")
            cfg = apply_profile(profile, dataset="mar2024", data_root=str(slices), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir=str(Path(d) / "out"))
            replay = sr.SessionReplay(cfg, profile, recorded=True, mark_from_mid=True)
            result = replay.run_recorded(sr.iter_recorded_events(cut))
            self.assertEqual(result["events"]["book"], 123)                          # the 121 rows of the window and the two seed snapshots
            self.assertEqual(replay.first_ns, T0 + 120 * STEP)                       # the replay's clock starts at the window start, on the seed
            with self.assertRaisesRegex(ValueError, "REPLAY_INTERLOCK"):
                sr.write_result(result, cut)
            sr.write_result(result, Path(d) / "out")
            record = sr.write_run_record(Path(d) / "out", replay)
            self.assertEqual(sorted(Path(r["path"]).name for r in record["inputs"]), ["canonical.jsonl", "trace.jsonl", "trades.jsonl"])


if __name__ == "__main__":
    unittest.main()
