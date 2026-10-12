# Interrupted partition resume evidence

The representative raw session was first run with `--max-records 1`, producing `PAUSED` at byte 948 with one committed record and 1,062 unpersisted bytes. The same job and partition were then restarted against the same SQLite state. It completed at byte 2,010 with two unique canonical records, zero rejected records and zero unpersisted bytes.

`events.jsonl.partition.json` is the final partition state. `events.jsonl` contains the two committed canonical envelopes and `events.jsonl.quarantine.jsonl` is empty. `events.jsonl.quality.json` is `APPROVED` and its quality-defect log is empty. The large informational persistence-lag value is expected because this deterministic fixture intentionally uses synthetic timestamps near Unix epoch; persistence-lag approval is correctly not applicable to batch mode and production continuous sessions use real receive timestamps.
