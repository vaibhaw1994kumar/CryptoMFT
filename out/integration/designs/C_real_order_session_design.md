# (c) Profile -> real-order session: order books from the live feed (design, 17 Nov 2026)

Owner decision (17 Nov): a real-order session takes its order books from the live feed, through the same book builder paper uses; the queue simulator's books stay replay-only (and paper's simulated venue). This note is the design; the tests come with the code. Real orders stay OFF: nothing here sends an order to any venue.

## The gap today

- `LivePaperSession` builds the profile's strategy only with the queue simulator: `strategy_builder` reads `paper._views` (float view books) and `paper._states` (`VenueState`), and those exist only when `queue_simulation` is given (`build_queue_simulation`). Without it the session refuses ("a strategy builder needs queue_simulation").
- `queue_simulation` is refused with real adapters ("INTERNAL_PAPER only, with no adapters supplied").
- So a session with venue adapters (testnet) cannot run the profile's arb strategy at all.

## Design

1. **One view builder, two users.** The per-event view update lives in `SimulatedVenueFeeder` (`simulation/queue_adapter.py`): for every live book event it updates a persistent float view book (`SimBook`, `delta_semantics="absolute"`, `tape_repair=False`, `message_atomic=True`) and its `VenueState`, seeding the view from the core book when a live feed starts with a REST snapshot. The simulated-venue part (feeding the maker executor, delivering simulated fills) is a second step in the same method.
   - Change: the feeder takes `adapters` per venue as OPTIONAL. A venue with no simulated adapter gets the view and state update only; nothing is fed to an executor and no simulated fill can be produced. With every adapter present (replay, simulated paper) the code path is unchanged, so every replay is byte-identical.
   - A new function `build_live_views(session_cfg)` next to `build_queue_simulation` returns `(views, states)` with exactly the constructor arguments `build_queue_simulation` uses; `build_queue_simulation` calls it, so the two cannot drift.
2. **The paper session with venue adapters.** When `adapters` are given (a real-order mode) and a `strategy_builder` is given, `LivePaperSession` builds `views, states = build_live_views(cfg)` and a `SimulatedVenueFeeder` with NO simulated adapters, fed from the live service's own books (`apply_core=False`, the same call paper makes today). The strategy builder then reads the same `_views` / `_states` attributes as in simulated paper. Orders go to the session's router, i.e. the venue adapters.
3. **Queue simulator books stay replay-only.** `queue_simulation` together with real adapters stays refused; in a real-order session there is no `MakerExecutor`, no `QueueAwareVenueAdapter`, and the feeder holds no adapter: tested (a fill can only come from the venue adapter).
4. **Real orders stay off.** A real-order strategy session needs an explicit constructor flag `real_order_strategy=True` (default False: refused with a message naming this design). The CLI keeps refusing `--execution-mode venue_testnet`; nothing sets the flag. It is turned on only with the testnet drill (queue item e), on the owner's word, testnet only.
5. **The profile's real-order requirements.** `live_paper_setup` returns `real_order_problems = check_real_order_profile(profile)`; a real-order strategy session refuses to start while it is not empty (v1_10 and later meet every row; v1_9 does not). The A3 clock gate stays as it is: a real-order session refuses to start without a clock source.

## Tests (to be written with the code, after (d) is committed)

- Replay and simulated paper unchanged: the existing feeder tests, `PaperWiringEqualsReplayWiring`, and the v1_9 identity replay (byte-identical, G3).
- Same view builder: one recorded event sequence through (a) a simulated-paper feeder and (b) a views-only feeder gives equal view books (levels, best bid and ask, ready flag) and equal `VenueState` readings after every event.
- Real-order session wiring with FAKE venue adapters (no network): with `real_order_strategy=True` the profile's strategy is built, reads views fed from the live books, and its orders reach the fake adapters; the feeder has no simulated adapter, and no fill appears unless the fake venue reports one.
- Refusals: no flag -> refused; `queue_simulation` with adapters -> refused; a profile with `real_order_problems` -> refused; the CLI still refuses `venue_testnet`.

## Files touched (extend, no new source file)

`simulation/queue_adapter.py` (feeder adapters optional, `build_live_views`), `paper_live.py` (views-only feeder, the flag, the refusals), `new_backtest/session_replay.py` (`real_order_problems` in the setup), tests in `tests/test_paper_queue_sim.py`, contract section in `docs/strategy_contract.md`.
