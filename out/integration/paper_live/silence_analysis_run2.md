# D7d run 2: offline silence analysis (12 Nov 2026, EXPLORATORY, Mac shakedown, wire frames only)

Source: the decompressed recording of run 2 (3,282.8 s, 350,693 wire frames; script `silence_analysis_run2.py`). For each venue's book channel
(Bybit `orderbook.50`, Binance `depth@100ms`) every gap of 1 s or more between consecutive frames was compared with the EXCHANGE timestamps
inside the two frames, and with what else arrived during the gap (the venue's other channels, the other venue).

## Result
22 book-frame gaps of 1 s or more.

| kind | how it shows | count | gap lengths | silences fired |
|---|---|---|---|---|
| Exchange-side quiet | exchange time moved as much as the receive clock; first frame after has lag about 0; other channels and the other venue kept flowing | 12 (6 Bybit, 6 Binance) | 2.5 to 6.5 s | all 12 |
| Delivery stall (client side) | consecutive exchange timestamps only 20 to 120 ms apart, but received 1.05 to 1.94 s apart; first frame after arrives with lag about equal to the gap | 10 (4 Bybit, 6 Binance) | 1.05 to 1.94 s | none |

* The 12 `book_stream_silences` of the run (6 per venue) are exactly the 12 genuine quiets. Not one delivery stall produced a silence.
* In 3 of the 10 stalls EVERY channel on BOTH venues froze at once (Bybit 1,594 ms and 1,941 ms, Binance 1,302 ms). Two independent connections to two exchanges do not stall together by chance: the cause is on our side (event loop, host, or both).
* The recorder reported `max_persistence_lag_ns` = 1.34 s in the same run: the recorder lagged the loop by more than a second at its worst. This fits a busy loop; it does not prove it.

## What this says about the "late silence timer" (owner 9 Nov blocker)
1. A quiet shorter than 2 s never fired a silence in this run, on either venue, although the age limit is 0.5 s: of 22 gaps of 1 s or more, only the 12 of 2.5 s or more fired. The receive timeout is armed from the last frame of ANY kind (the core's book age rule, owner 8 Nov), so a socket that still sends something does not time out. The lateness of 1.3 to 4.3 s seen live is partly that and partly real client stalls.
2. The exchange-side quiet is genuine on change-driven streams: Binance depth@100ms went quiet for up to 6.5 s while trades and the other venue flowed. That is a data fact, not a fault.
3. Not separable offline: an event-loop stall from a host or network stall (both look like a delivery stall). The live event-loop lag probe (lateness of each scheduled wake-up) is still needed for that.
4. The recording also holds Binance markPrice frames (3,282 ticks, with the funding rate `r`): derivative data exist for this run (relevant to the funding line of the shadow reconciliation).

## Proposal for the silence alarm (not built)
Two different alarms, told apart by the exchange timestamp of the first frame after the quiet:
* `STREAM_QUIET`: exchange time advanced with the receive clock. The book gate already blocks new risk at 0.5 s; this is information.
* `LOOP_STALL`: receive gap large, exchange gap small. A client-side fault: new risk stays blocked until the stream is caught up, and it is logged and counted.
Plus a watchdog that does NOT depend on the event loop: a thread on the monotonic clock that checks the loop's heartbeat stamp, so a blocked loop is detected while it is blocked and not afterwards.
