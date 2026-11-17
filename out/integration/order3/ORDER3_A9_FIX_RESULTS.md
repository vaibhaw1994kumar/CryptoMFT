# Order 3, A9 fixes (build session, 17 Nov 2026): recorder cost and arrival stamps, and the rerun

EXPLORATORY: this Mac, one process, a synthetic top-of-book stream (`scripts/stress_feed.py`) through the live parse, capture, book, gate and session path on the wall clock. The pass criterion is unchanged: no dropped frame, 0 stale-book events, 0 gaps / out-of-order / duplicates / errors, no reconnect or backlog resync, the book gate open at the end, and the end-to-end lag p99 (the frame's SCHEDULED arrival to the end of its processing, independent of the service's own stamps) under 100 ms. One more check was added, not a weaker one: no reconnect or backlog resync.

## (a) Recorder per-frame cost (`live_feed.py`, `raw_capture.py`)

* The per-record flush is now a bounded-interval flush: `recorder_flush_writes` (default 200 write calls, about 100 frames) or `recorder_flush_interval_seconds` (default 0.25 s) since the last flush at the next write; a quiet feed is flushed by the monitor tick (`monitor_interval_seconds`, 1 s, so at the latest about 1.25 s after the last write); always at part rotation and at shutdown; fsync follows the same bound when `durable`. A hard crash can lose at most that window of the newest records. `recorder_flush_writes: 1` with interval 0 restores the old per-record flush (and it is the default of the `RotatingLiveRecorder` class itself).
* The per-frame `stat` of four files in `_should_rotate` is replaced by byte counters (the lines are ASCII, so the counter equals the file size): the same rotation points.
* The `asdict` copies (wire, raw, canonical event) and the per-frame deep copy of the event are gone: the wire and raw lines are built from the dict already in hand (`unsigned` plus `record_hash`), the event from its fields with the nested dataclasses turned into dicts by `json.dumps` itself.
* Byte-identity (tests/test_live_feed_a9.py `RecorderBytes`): the same synthetic input (300 frames on two venues, binary and text frames, control and rejected frames) written by the OLD recorder (HEAD) and the new one gives identical `wire`, `raw`, `canonical`, `trace`, `trades` and `quarantine` files, for the old per-record flush, the new bounded flush and a tiny flush count; and with `rotate_bytes 60000` the same 13 parts with the same contents (aggregate sha256 `7dc2d59f...` from the old code). The golden hashes in the test are those of the old code's files.

## (b) Arrival stamps (`live_feed.py`, `paper_live.py`, `config.py`)

* `LiveFeedSettings.arrival_stamp` (default False = the old behaviour, byte for byte). On: a reader task per book connection takes frames from the connection as they arrive (a buffered frame is taken without a pause; it yields every 64 frames) and stamps wall and monotonic time on each. The frame's `receive_ts_ns`, the book's last receive time, the stream-liveness evidence and the "last applied" mark are the ARRIVAL stamps; the processing time is kept (`arrival_stats`: frames, max backlog frames, max arrival-to-processing time). The book age the staleness gate reads is therefore the age of the newest applied frame's ARRIVAL, so a processor that falls behind shows as book age.
* A backlog beyond the book age limit (`max_book_age`, 1000 ms in the config) does not flip the book between STALE and VALID once per frame (my first version did: 25,850 flips, a 60 s run for a 15 s burst, because every flip logs). The queue is discarded, the book marked stale and the connection resynchronised from a snapshot, as a silent stream is: counted (`backlog_resyncs`, `backlog_frames_discarded`) and logged as `FEED_BACKLOG`.
* Test (`ArrivalStamp`): the same slow processor (3 ms per frame against frames every 1 ms): OFF the frames look fresh (max age under 200 ms, no resync); ON the age passes 500 ms, the backlog is discarded and the connection resynchronised; a processor that keeps up changes nothing (no resync, one connection, age under 200 ms).
* Where it belongs: it is the live feed service, not a strategy or a replay rule, so no replay output of any profile can change (replays do not use the live feed). It is a SWITCH, not a default: v1_10 turns it on with the profile key `oms.session_limits.feed_arrival_stamp: true` (a REAL_ORDER_REQUIRED row; `LivePaperSession` reads it); v1_9 and earlier have no key and stay as they are. Book frames only: the trade and derivative streams keep their processing stamps.

## Rerun (5 s per rate, per venue; `out/integration/stress/after_fix/`)

| Events/s per venue | Result | End-to-end lag p99 | Book age p99 | Notes |
|---|---|---|---|---|
| 2,000 | PASS | 3.1 ms | 1.9 ms | CPU 73% of one core |
| 3,000 | PASS | 22 ms | 20 ms | CPU 92% |
| 4,000 | FAIL | 825 ms | 715 ms | nothing dropped, no resync yet: the lag is just under the 1 s limit |
| 5,000 | FAIL | 994 ms | 993 ms | 2 backlog resyncs, 9,997 frames discarded, 4 stale |
| 7,000 | FAIL | 999 ms | 993 ms | 5 backlog resyncs, 28,814 discarded |
| **10,000** | **FAIL** | 989 ms | 991 ms | 6 backlog resyncs, 59,966 of 96,536 frames discarded, 8 stale, 4 connections per venue; processed about 18,300 frames per venue |
| 10,000, arrival stamps OFF | FAIL | 7,942 ms | 13 ms | all 50,000 processed but 24.6 s for a 5 s burst, age 13 ms and 1 stale event: the blind spot, as before |

So: measured throughput on this Mac is about **3,000 to 3,500 events/s per venue** (before the fixes the knee was between 2,000 and 3,000), CPU-bound at 95% of one core. At the 10,000/s the owner named the path does NOT keep up. What changed is that it now SAYS so: the backlog shows as book age (about 990 ms at the limit), as stale and resync events, and in `arrival_stats`, where before the same run reported 13 ms of book age and one stale event while 7.9 s behind.

## Where the time goes now (cProfile, 2 s at 10,000/s; the burst and its drain)

Per processed frame about 0.2 to 0.4 ms (CPU-bound). By own time the top is `json.dumps` and its encoder (87,000 calls for 5,900 frames: about 15 per frame, now mostly the hash computations of the wire, raw and trace records, and the canonical event), `time_integrity.observe` (the per-event delay statistics and the sorted offset window, about 12% of cumulative time), the raw writer's append (hashing, `uuid5`), `normalize` and `MarketEvent.from_dict`, and the asyncio loop itself (`wait_for` per frame in the reader, `kqueue`). The session part (`_on_event`) is small. Remaining levers, not taken (the owner said not to tune the strategy, and each changes more than the recorder): hash once instead of serialising the same record twice, batch the trace and wire records, a cheaper delay-statistics window.

## What the A9 result means for the limits (corrected after the fix)

With `feed_arrival_stamp` on, a live session that cannot keep up marks its book stale and resynchronises, so the quote gates (book age, the A3 and A4 stops) see the overload instead of trading on a backlog; the A1 bucket and the A2 loss stop run on the event clock and are not affected in their rule. Not done: the 10,000/s target. At or below about 3,000/s per venue (a normal single-symbol stream is far below that) none of this bites.
