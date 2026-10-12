# Reproducible cross-venue alignment evidence

The Binance and Bybit input streams contain two events each. `merged.jsonl` contains all four original immutable canonical events in recorded-time order. `merged.jsonl.eligibility.jsonl` records one blocked initial decision while the second venue is still syncing, followed by three eligible decisions after both required books are valid and within the one-second age limit.

`merged.jsonl.alignment.json` records input/output checksums, per-event native-state hashes and deterministic alignment SHA-256 `47f30858bb681ab460b60d58598429bc44b4cfa09eba3f964b9295284f40526e`.

The `run/` replay produces one closed opportunity with no alignment routing rejection, gross PnL `0.60000`, fees `0.48024`, net PnL `0.11976` and an `APPROVED/HEALTHY` data-quality report. Repeating the merge and replay is covered by automated byte/hash equality tests.
