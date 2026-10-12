#!/usr/bin/env python3
"""Continuous Binance + Bybit paper-trading watcher.

Runs the platform end to end, cycle after cycle, and reports every problem:

1. ``live-paper`` (INTERNAL_PAPER dry run / forward test) on live public books
   and trades with cross-venue arbitrage, basis mean reversion and the
   sim_engine maker dry run. Market data is stored (wire, raw, canonical,
   trades) as it arrives. No order is ever sent and no credential is used.
2. ``analyze-session`` on the finished cycle, in a separate process while the
   next cycle is already trading: storage hash chains, order-book replay, data
   sanity for every component, backtest replay of the same period and
   comparison with the forward test, and the sim_engine replay simulation.

Output (``--root``, default ``runs/paper_watch``):

* ``alerts.jsonl``   every ERROR / WARN with component and cycle
* ``status.json``    current cycle, progress and last audit
* ``report.md``      one row per cycle plus open issues
* ``watcher.log``    everything printed to stdout
* ``live/<cycle>/``  stored market data and paper artifacts
* ``audits/<cycle>/`` audit report, backtest replay, simulation

Stdout lines beginning ``ALERT`` / ``CYCLE`` / ``WATCHER`` are meant to be
streamed to an operator. Stop with Ctrl+C or SIGTERM (the running cycle
flattens, finalizes and is audited).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Watcher:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.root = Path(args.root).resolve()
        for name in ("live", "audits", "logs"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.config = Path(args.config).resolve()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.child: subprocess.Popen | None = None
        self.cycles: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self._recent: dict[str, float] = {}
        self.alert_counts = {"ERROR": 0, "WARN": 0}
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = str(ROOT / "src")
        self.env["PYTHONUNBUFFERED"] = "1"
        if args.ssl_cert_file:
            self.env["SSL_CERT_FILE"] = str(Path(args.ssl_cert_file).resolve())

    # -- output ------------------------------------------------------------------

    def emit(self, line: str) -> None:
        stamped = f"{datetime.now(timezone.utc).strftime('%H:%M:%SZ')} {line}"
        with self.lock:
            print(stamped, flush=True)
            with (self.root / "watcher.log").open("a", encoding="utf-8") as handle:
                handle.write(stamped + "\n")

    def alert(self, level: str, component: str, message: str, cycle: str = "") -> None:
        message = " ".join(message.split())[:400]
        key = f"{level}|{component}|{message[:120]}"
        now = time.time()
        with self.lock:
            last = self._recent.get(key)
            self._recent[key] = now
            self.alert_counts[level] = self.alert_counts.get(level, 0) + 1
            with (self.root / "alerts.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(), "level": level,
                    "component": component, "cycle": cycle, "message": message,
                }) + "\n")
        if last is None or now - last > self.args.alert_dedupe_seconds:
            self.emit(f"ALERT {level} [{component}] {cycle} {message}")

    def cli(self, *args: str) -> list[str]:
        return [self.args.python, "-m", "crypto_stat_arb.cli", *args]

    def write_status(self) -> None:
        status = {
            "updated": datetime.now(timezone.utc).isoformat(),
            "root": str(self.root),
            "config": str(self.config),
            "mode": "INTERNAL_PAPER (public market data only; no orders, no credentials)",
            "current_cycle": self.current,
            "completed_cycles": len(self.cycles),
            "alerts": self.alert_counts,
            "last_cycle": self.cycles[-1] if self.cycles else None,
        }
        path = self.root / "status.json"
        path.with_suffix(".tmp").write_text(json.dumps(status, indent=2, default=str))
        path.with_suffix(".tmp").replace(path)

    def write_report(self) -> None:
        with self.lock:
            cycles = list(self.cycles)
        cumulative = 0.0
        lines = [
            "# Paper trading watcher report", "",
            f"Updated {datetime.now(timezone.utc).isoformat()} - root `{self.root}`", "",
            "Mode: INTERNAL_PAPER on live Binance + Bybit public data. No orders are sent.", "",
            "| Cycle | Minutes | Events | Opportunities by strategy | Closed | Net PnL | "
            "Cum. PnL | Maker dry-run fills | Replay vs paper | Audit | Issues |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in cycles:
            cumulative += float(row.get("net_pnl") or 0)
            lines.append(
                f"| {row['session_id']} | {row['minutes']} | {row.get('events')} | "
                f"{row.get('by_strategy')} | {row.get('closed_plans')} | {row.get('net_pnl')} | "
                f"{cumulative:.6f} | {row.get('maker_fills')} | {row.get('comparison')} | "
                f"{row.get('audit_status')} | {row.get('issue_count')} |"
            )
        lines += ["", "## Latest issues", ""]
        latest = cycles[-1] if cycles else {}
        for issue in (latest.get("issues") or [])[:60]:
            lines.append(
                f"- **{issue['status']}** `{issue['component']}.{issue['check']}`: {issue['detail']}"
            )
        if latest.get("components"):
            lines += ["", "## Component status (latest cycle)", ""]
            lines += [f"- {name}: {status}" for name, status in latest["components"].items()]
        (self.root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # -- cycle ---------------------------------------------------------------------

    def _session_bytes(self, session_dir: Path) -> int:
        return sum(path.stat().st_size for path in session_dir.glob("part-*/canonical.jsonl"))

    def _read_stream(self, stream, sink: list[str] | None, log_path: Path | None, session_id: str) -> None:
        handle = log_path.open("a", encoding="utf-8") if log_path else None
        try:
            for line in stream:
                if handle:
                    handle.write(line)
                    handle.flush()
                if sink is not None:
                    sink.append(line)
                    continue
                if "Traceback" in line or " ERROR " in line or " CRITICAL " in line:
                    self.alert("ERROR", "live-paper", line.strip(), session_id)
                elif " WARNING " in line:
                    self.alert("WARN", "live-paper", line.strip(), session_id)
        finally:
            if handle:
                handle.close()

    def run_cycle(self, number: int, minutes: float):
        session_id = f"cycle-{number:04d}-{utc_stamp()}"
        session_dir = self.root / "live" / session_id
        stderr_path = self.root / "logs" / f"{session_id}.stderr.log"
        command = self.cli(
            "live-paper", "--config", str(self.config),
            "--output-dir", str(self.root / "live"), "--session-id", session_id,
            "--max-runtime-seconds", str(int(minutes * 60)), "--no-udp",
            "--status-poll-seconds", str(self.args.status_poll_seconds),
            "--maker-dry-run", "--flatten-lead-seconds", str(self.args.flatten_lead_seconds),
            "--log-level", "WARNING",
        )
        self.current = {
            "session_id": session_id, "cycle": number, "minutes": minutes,
            "started": datetime.now(timezone.utc).isoformat(), "phase": "LIVE_PAPER",
        }
        self.write_status()
        self.emit(f"CYCLE {number} START {session_id} ({minutes:g} min live paper)")
        process = subprocess.Popen(
            command, cwd=ROOT, env=self.env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self.child = process
        stdout: list[str] = []
        readers = [
            threading.Thread(target=self._read_stream, args=(process.stdout, stdout, None, session_id), daemon=True),
            threading.Thread(target=self._read_stream, args=(process.stderr, None, stderr_path, session_id), daemon=True),
        ]
        for reader in readers:
            reader.start()
        last_bytes, last_growth = 0, time.time()
        cycle_deadline = time.time() + minutes * 60
        overrun_sigint_sent = False
        overrun_sigint_at = None
        while process.poll() is None:
            if self.stop.wait(self.args.heartbeat_seconds):
                break
            size = self._session_bytes(session_dir)
            if size > last_bytes:
                last_bytes, last_growth = size, time.time()
            elif time.time() - last_growth > self.args.stall_seconds:
                self.alert("ERROR", "feed", f"no market data stored for {time.time() - last_growth:.0f}s", session_id)
                last_growth = time.time()
            self.current["stored_canonical_bytes"] = size
            self.current["heartbeat"] = datetime.now(timezone.utc).isoformat()
            self.write_status()
            # Defense-in-depth: the process should stop itself on schedule
            # (its own --max-runtime-seconds, with an in-process watchdog
            # behind that). This is the external, last-resort backstop so a
            # stuck cycle can never block the watcher's cadence indefinitely.
            now = time.time()
            if not overrun_sigint_sent and now > cycle_deadline + self.args.max_overrun_seconds:
                overrun_sigint_sent = True
                overrun_sigint_at = now
                self.alert(
                    "ERROR", "watcher",
                    f"cycle ran {now - cycle_deadline:.0f}s past its {minutes:g}min budget "
                    f"without stopping; sending SIGINT", session_id,
                )
                process.send_signal(signal.SIGINT)
            elif (
                overrun_sigint_sent
                and now > overrun_sigint_at + self.args.kill_grace_seconds
            ):
                self.alert(
                    "ERROR", "watcher",
                    f"cycle did not exit {self.args.kill_grace_seconds:.0f}s after SIGINT; "
                    "sending SIGKILL (data up to this point is preserved)", session_id,
                )
                process.kill()
        code = process.wait()
        for reader in readers:
            reader.join(timeout=10)
        self.child = None
        if code != 0:
            self.alert("ERROR", "live-paper", f"exited with code {code}; see {stderr_path}", session_id)
            return None
        try:
            summary = json.loads("".join(stdout))
        except json.JSONDecodeError as exc:
            self.alert("ERROR", "live-paper", f"unreadable summary: {exc}", session_id)
            return None
        strategies = (summary.get("strategies") or {}).get("strategies", {})
        by_strategy = {sid: row.get("opportunities") for sid, row in strategies.items()}
        maker = summary.get("maker_dry_run") or {}
        maker_fills = {venue: row.get("fills") for venue, row in maker.get("venues", {}).items()}
        self.emit(
            f"CYCLE {number} LIVE DONE events={summary.get('canonical_events_delivered')} "
            f"opportunities={by_strategy} closed={summary.get('closed_plans')} "
            f"net_pnl={summary.get('net_pnl')} maker_dry_run_fills={maker_fills} "
            f"quality={summary.get('data_quality_approval', {}).get('decision')}"
        )
        record = {
            "session_id": session_id, "cycle": number, "minutes": minutes,
            "events": summary.get("canonical_events_delivered"),
            "by_strategy": by_strategy, "closed_plans": summary.get("closed_plans"),
            "net_pnl": summary.get("net_pnl"), "maker_fills": maker_fills,
            "audit_status": "PENDING",
        }
        return record, session_dir, stderr_path

    def audit(self, record: dict[str, Any], session_dir: Path, stderr_path: Path) -> None:
        session_id = record["session_id"]
        output = self.root / "audits" / session_id
        try:
            completed = subprocess.run(
                self.cli(
                    "analyze-session", "--config", str(self.config),
                    "--session-dir", str(session_dir), "--output", str(output),
                    "--stderr-log", str(stderr_path),
                ),
                cwd=ROOT, env=self.env, capture_output=True, text=True,
                timeout=self.args.audit_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            self.alert("ERROR", "audit", "analyze-session timed out", session_id)
            record["audit_status"] = "TIMEOUT"
        else:
            if completed.returncode != 0:
                self.alert("ERROR", "audit", completed.stderr.strip()[-400:], session_id)
                record["audit_status"] = "CRASHED"
            else:
                audit = json.loads((output / "session_audit.json").read_text())
                record["audit_status"] = audit["overall_status"]
                record["components"] = {
                    name: row["status"] for name, row in audit["components"].items()
                }
                record["issues"] = audit["issues"]
                record["issue_count"] = len(audit["issues"])
                comparison = audit["components"].get("backtest_replay", {}).get("metrics", {}).get("comparison", {})
                record["comparison"] = comparison.get("decision")
                for issue in audit["issues"]:
                    self.alert(
                        "ERROR" if issue["status"] == "FAIL" else "WARN",
                        f"{issue['component']}.{issue['check']}", issue["detail"], session_id,
                    )
                self.emit(
                    f"CYCLE {record['cycle']} AUDIT {audit['overall_status']} "
                    + " ".join(f"{name}={status}" for name, status in record["components"].items())
                )
        with self.lock:
            self.cycles.append(record)
            self.cycles.sort(key=lambda row: row["cycle"])
        self.write_report()
        self.write_status()
        # Consolidated AUDIT_REPORT.md / audit_report.json across every cycle.
        subprocess.run(
            self.cli("cycle-report", "--root", str(self.root)), cwd=ROOT, env=self.env,
            capture_output=True, text=True, timeout=300,
        )
        self.emit(f"CYCLE {record['cycle']} REPORT {self.root / 'AUDIT_REPORT.md'}")

    # -- main loop ---------------------------------------------------------------------

    def request_stop(self, *_: Any) -> None:
        if self.stop.is_set():
            return
        self.stop.set()
        self.emit("WATCHER stopping: finishing the running cycle")
        if self.child is not None and self.child.poll() is None:
            self.child.send_signal(signal.SIGINT)

    def run(self) -> int:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        self.emit(
            f"WATCHER START root={self.root} config={self.config.name} "
            f"first_cycle={self.args.first_cycle_minutes}m cycle={self.args.cycle_minutes}m "
            "mode=INTERNAL_PAPER (no orders, no credentials)"
        )
        audits: list[threading.Thread] = []
        number = 0
        while not self.stop.is_set():
            number += 1
            minutes = self.args.first_cycle_minutes if number == 1 else self.args.cycle_minutes
            result = self.run_cycle(number, minutes)
            if result is not None:
                thread = threading.Thread(target=self.audit, args=result)
                thread.start()
                audits.append(thread)
            elif not self.stop.is_set():
                self.stop.wait(self.args.retry_seconds)
            if self.args.max_cycles and number >= self.args.max_cycles:
                break
        self.current = None
        for thread in audits:
            thread.join()
        self.write_report()
        self.write_status()
        self.emit(
            f"WATCHER STOPPED cycles={len(self.cycles)} alerts={self.alert_counts} "
            f"report={self.root / 'report.md'}"
        )
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default=str(ROOT / "config" / "paper_watch.json"))
    parser.add_argument("--root", default=str(ROOT / "runs" / "paper_watch"))
    parser.add_argument("--first-cycle-minutes", type=float, default=5)
    parser.add_argument("--cycle-minutes", type=float, default=30)
    parser.add_argument("--max-cycles", type=int, default=0, help="0 = run until stopped")
    parser.add_argument("--status-poll-seconds", type=float, default=60)
    parser.add_argument("--flatten-lead-seconds", type=float, default=20)
    parser.add_argument("--heartbeat-seconds", type=float, default=30)
    parser.add_argument("--stall-seconds", type=float, default=120)
    parser.add_argument("--retry-seconds", type=float, default=30)
    parser.add_argument("--audit-timeout-seconds", type=float, default=1800)
    parser.add_argument(
        "--max-overrun-seconds", type=float, default=180,
        help="grace period past a cycle's planned minutes before the watcher SIGINTs it",
    )
    parser.add_argument(
        "--kill-grace-seconds", type=float, default=120,
        help="grace period after that SIGINT before the watcher SIGKILLs the cycle",
    )
    parser.add_argument("--alert-dedupe-seconds", type=float, default=300)
    parser.add_argument("--ssl-cert-file", help="PEM trust store for exchange TLS (verification stays on)")
    parser.add_argument("--python", default=sys.executable)
    return Watcher(parser.parse_args()).run()


if __name__ == "__main__":
    sys.exit(main())
