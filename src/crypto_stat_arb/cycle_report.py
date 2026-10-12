"""Consolidated audit report across every paper-watcher cycle.

Reads ``<root>/live/cycle-*`` (paper artifacts, live manifests),
``<root>/audits/cycle-*/session_audit.json``, ``alerts.jsonl`` and
``status.json`` and writes ``AUDIT_REPORT.md`` plus ``audit_report.json``.
It is regenerated automatically after each cycle's ``analyze-session``.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


COMPONENTS = (
    "feed", "order_books", "time_integrity", "historical_storage",
    "orderbook_replay", "backtest_replay", "simulation", "dry_run",
    "strategies", "security_master", "errors",
)
RANK = {"OK": 0, "WARN": 1, "FAIL": 2}


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except json.JSONDecodeError:
        return None


def _utc(ns: int | None) -> str:
    if not ns:
        return "-"
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _num(value: Any, digits: int = 4) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _cycle_row(session_dir: Path, audit_dir: Path, current: dict | None, alerts: list[dict]) -> dict[str, Any]:
    session_id = session_dir.name
    manifest = _json(session_dir / "live_manifest.json") or {}
    service = manifest.get("service", {})
    summary = _json(session_dir / "paper" / "paper_summary.json") or {}
    audit = _json(audit_dir / "session_audit.json")
    running = bool(current and current.get("session_id") == session_id)
    if audit:
        status = "AUDITED"
    elif running:
        status = "RUNNING"
    elif summary:
        status = "AUDIT_PENDING"
    else:
        status = "INCOMPLETE"
    components = (audit or {}).get("components", {})

    def metrics(name: str) -> dict[str, Any]:
        return components.get(name, {}).get("metrics", {}) or {}

    venues = service.get("venues", {})
    strategies = (summary.get("strategies") or {}).get("strategies", {})
    maker = summary.get("maker_dry_run") or {}
    simulation = metrics("simulation")
    comparison = metrics("backtest_replay").get("comparison", {})
    time_metrics = metrics("time_integrity")
    storage = metrics("historical_storage")
    cycle_alerts = [row for row in alerts if row.get("cycle") == session_id]
    started = service.get("started_ts_ns")
    stopped = service.get("stopped_ts_ns")
    return {
        "session_id": session_id,
        "status": status,
        "overall": (audit or {}).get("overall_status", "-"),
        "started_utc": _utc(started) if started else (current or {}).get("started", "-"),
        "stopped_utc": _utc(stopped),
        "minutes": round((stopped - started) / 60e9, 2) if started and stopped else None,
        "events_by_venue": {venue: row.get("canonical_events") for venue, row in venues.items()},
        "trades_by_venue": {venue: row.get("trade_ticks") for venue, row in venues.items()},
        "reconnects_by_venue": {
            venue: (row.get("reconnects") or 0) + (row.get("stale_disconnects") or 0)
            for venue, row in venues.items()
        },
        "last_error_by_venue": {venue: row.get("last_error") for venue, row in venues.items()},
        "opportunities_by_strategy": {sid: row.get("opportunities") for sid, row in strategies.items()},
        "plans_by_strategy": {
            sid: f"{row.get('closed_plans')}/{row.get('plans')}" for sid, row in strategies.items()
        },
        "net_pnl_by_strategy": {sid: row.get("net_pnl") for sid, row in strategies.items()},
        "closed_plans": summary.get("closed_plans"),
        "net_pnl": summary.get("net_pnl"),
        "fees": summary.get("fees"),
        "quality_gate": (summary.get("data_quality_approval") or {}).get("decision"),
        "quality_flags": (summary.get("data_quality_approval") or {}).get("active_flag_codes", []),
        "maker_dry_run": {
            venue: {
                "fills": row.get("fills"), "quotes": row.get("quotes"),
                "pnl": row.get("mark_to_market_pnl_quote"), "inventory": row.get("inventory"),
            }
            for venue, row in maker.get("venues", {}).items()
        },
        "simulation_replay": {
            "engine": simulation.get("engine"),
            "venues": {
                venue: {"fills": row.get("fills"), "pnl": row.get("mark_to_market_pnl_quote")}
                for venue, row in (simulation.get("venues") or {}).items()
            },
        },
        "replay_vs_paper": comparison.get("decision"),
        "replay_opportunities": (metrics("backtest_replay").get("replay") or {}).get("opportunities"),
        "host_offset_ms": time_metrics.get("clock_offset_estimate_ms"),
        "blocked_events": time_metrics.get("blocked_events"),
        "time_events": time_metrics.get("events"),
        "storage_parts": storage.get("parts"),
        "persistence_lag_ms": storage.get("max_persistence_lag_ms"),
        "components": {name: components.get(name, {}).get("status", "-") for name in COMPONENTS},
        "issues": (audit or {}).get("issues", []),
        "component_metrics": {name: metrics(name) for name in COMPONENTS} if audit else {},
        "alerts": {
            "ERROR": sum(row.get("level") == "ERROR" for row in cycle_alerts),
            "WARN": sum(row.get("level") == "WARN" for row in cycle_alerts),
        },
        "progress": (current or {}) if running else None,
    }


def build_cycle_report(root: Path) -> dict[str, Any]:
    root = Path(root)
    status = _json(root / "status.json") or {}
    current = status.get("current_cycle")
    alerts = []
    if (root / "alerts.jsonl").exists():
        for line in (root / "alerts.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                alerts.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    cycles = [
        _cycle_row(session_dir, root / "audits" / session_dir.name, current, alerts)
        for session_dir in sorted((root / "live").glob("cycle-*"))
    ]
    audited = [row for row in cycles if row["status"] == "AUDITED"]
    recurring: dict[tuple[str, str], dict[str, Any]] = {}
    for row in audited:
        for issue in row["issues"]:
            key = (f"{issue['component']}.{issue['check']}", issue["status"])
            entry = recurring.setdefault(key, {"cycles": [], "latest_detail": ""})
            entry["cycles"].append(row["session_id"])
            entry["latest_detail"] = issue["detail"]
    cumulative = 0.0
    for row in audited:
        try:
            cumulative += float(row["net_pnl"] or 0)
        except ValueError:
            pass
        row["cumulative_net_pnl"] = cumulative
    totals = {
        "cycles_total": len(cycles),
        "cycles_audited": len(audited),
        "cycles_running": sum(row["status"] == "RUNNING" for row in cycles),
        "worst_audit_status": max((row["overall"] for row in audited), key=lambda s: RANK.get(s, -1), default="-"),
        "events": sum(sum(v or 0 for v in row["events_by_venue"].values()) for row in audited),
        "trades": sum(sum(v or 0 for v in row["trades_by_venue"].values()) for row in audited),
        "opportunities": defaultdict(int),
        "closed_plans": sum(row["closed_plans"] or 0 for row in audited),
        "cumulative_net_pnl": cumulative,
        "maker_dry_run_fills": defaultdict(int),
        "alerts": {
            "ERROR": sum(row.get("level") == "ERROR" for row in alerts),
            "WARN": sum(row.get("level") == "WARN" for row in alerts),
        },
    }
    for row in audited:
        for sid, count in row["opportunities_by_strategy"].items():
            totals["opportunities"][sid] += count or 0
        for venue, item in row["maker_dry_run"].items():
            totals["maker_dry_run_fills"][venue] += item.get("fills") or 0
    totals["opportunities"] = dict(totals["opportunities"])
    totals["maker_dry_run_fills"] = dict(totals["maker_dry_run_fills"])
    report = {
        "schema_version": "1.0.0",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "mode": "INTERNAL_PAPER on live Binance + Bybit public data (no orders, no credentials)",
        "totals": totals,
        "cycles": cycles,
        "recurring_issues": [
            {"check": check, "severity": severity, "cycles": len(entry["cycles"]),
             "cycle_ids": entry["cycles"], "latest_detail": entry["latest_detail"]}
            for (check, severity), entry in sorted(
                recurring.items(), key=lambda item: (-RANK[item[0][1]], -len(item[1]["cycles"]), item[0][0])
            )
        ],
    }
    (root / "audit_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (root / "AUDIT_REPORT.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def _fmt_map(values: dict[str, Any]) -> str:
    return " / ".join(f"{key.split('_')[0]}:{value}" for key, value in values.items()) or "-"


def render_markdown(report: dict[str, Any]) -> str:
    totals = report["totals"]
    cycles = report["cycles"]
    lines = [
        "# Paper trading audit report", "",
        f"Generated {report['generated_utc']}", "",
        f"Mode: {report['mode']}", "",
        "## Summary", "",
        "| Metric | Value |", "| --- | --- |",
        f"| Cycles (audited / running / total) | {totals['cycles_audited']} / {totals['cycles_running']} / {totals['cycles_total']} |",
        f"| Worst audit status | {totals['worst_audit_status']} |",
        f"| Book events / trades stored (audited) | {totals['events']:,} / {totals['trades']:,} |",
        f"| Opportunities by strategy | {_fmt_map(totals['opportunities'])} |",
        f"| Closed plans | {totals['closed_plans']} |",
        f"| Cumulative net PnL (USDT, paper) | {_num(totals['cumulative_net_pnl'], 6)} |",
        f"| sim_engine maker dry-run fills | {_fmt_map(totals['maker_dry_run_fills'])} |",
        f"| Alerts ERROR / WARN | {totals['alerts']['ERROR']} / {totals['alerts']['WARN']} |",
        "", "## Cycles", "",
        "| Cycle | Start (UTC) | Min | Status | Audit | Book events | Trades | Opportunities | Closed | Net PnL | Maker dry-run fills | Sim replay fills | Quality gate | Replay vs paper |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in cycles:
        lines.append(
            f"| {row['session_id']} | {row['started_utc']} | {row['minutes'] if row['minutes'] is not None else '-'} | "
            f"{row['status']} | {row['overall']} | {_fmt_map(row['events_by_venue'])} | "
            f"{_fmt_map(row['trades_by_venue'])} | {_fmt_map(row['opportunities_by_strategy'])} | "
            f"{row['closed_plans'] if row['closed_plans'] is not None else '-'} | {row['net_pnl'] or '-'} | "
            f"{_fmt_map({v: i['fills'] for v, i in row['maker_dry_run'].items()})} | "
            f"{_fmt_map({v: i['fills'] for v, i in row['simulation_replay']['venues'].items()})} | "
            f"{row['quality_gate'] or '-'} | {row['replay_vs_paper'] or '-'} |"
        )
    audited = [row for row in cycles if row["status"] == "AUDITED"]
    lines += ["", "## Component status by cycle", "",
              "| Component | " + " | ".join(row["session_id"][:10] for row in audited) + " |",
              "| --- |" + " --- |" * len(audited)]
    for name in COMPONENTS:
        lines.append(f"| {name} | " + " | ".join(row["components"][name] for row in audited) + " |")
    lines += ["", "## Recurring issues", ""]
    if report["recurring_issues"]:
        lines += ["| Severity | Check | Cycles | Latest detail |", "| --- | --- | --- | --- |"]
        for issue in report["recurring_issues"]:
            detail = str(issue["latest_detail"]).replace("|", "/")[:220]
            lines.append(f"| {issue['severity']} | `{issue['check']}` | {issue['cycles']}/{len(audited)} | {detail} |")
    else:
        lines.append("No issues in audited cycles.")
    lines += ["", "## Cycle details", ""]
    for row in cycles:
        lines += [f"### {row['session_id']} — {row['status']} / audit {row['overall']}", ""]
        if row["status"] == "RUNNING":
            progress = row["progress"] or {}
            lines += [
                f"Live since {progress.get('started')}, planned {progress.get('minutes')} min; "
                f"stored canonical bytes {progress.get('stored_canonical_bytes')}, heartbeat {progress.get('heartbeat')}. "
                "The audit runs when the cycle ends.", "",
            ]
            continue
        if row["status"] != "AUDITED":
            lines += ["No audit yet.", ""]
            continue
        metrics = row["component_metrics"]
        mean_reversion = metrics.get("strategies", {}).get("basis_mean_reversion_v1", {})
        dry = metrics.get("dry_run", {})
        replay = metrics.get("orderbook_replay", {})
        lines += [
            f"- **Window:** {row['started_utc']} → {row['stopped_utc']} UTC ({row['minutes']} min)",
            f"- **Feed:** book events {_fmt_map(row['events_by_venue'])}; trades {_fmt_map(row['trades_by_venue'])}; "
            f"reconnects/stale {_fmt_map(row['reconnects_by_venue'])}",
            f"- **Books / quality gate:** {row['quality_gate']} {row['quality_flags'] or ''}",
            f"- **Time:** host clock offset {_num(row['host_offset_ms'], 1)} ms; blocked {row['blocked_events']} of {row['time_events']} events",
            f"- **Storage:** {row['storage_parts']} part(s), persistence lag {_num(row['persistence_lag_ms'], 1)} ms "
            f"({row['components']['historical_storage']})",
            f"- **Order-book replay:** {replay.get('events')} stored events rebuild live books ({row['components']['orderbook_replay']})",
            f"- **Backtest replay:** {row['replay_opportunities']} replay opportunities; comparison {row['replay_vs_paper']}",
            f"- **Simulation (sim_engine replay):** engine {row['simulation_replay']['engine']}; "
            + ", ".join(f"{v} fills {i['fills']} pnl {_num(i['pnl'])}" for v, i in row["simulation_replay"]["venues"].items()),
            f"- **Dry run:** opportunities {_fmt_map(row['opportunities_by_strategy'])}; plans closed "
            f"{_fmt_map(row['plans_by_strategy'])}; net PnL {row['net_pnl']}; kill switch {dry.get('kill_switch')}; "
            f"open residual {dry.get('open_residual_base')}; maker dry run "
            + ", ".join(f"{v} fills {i['fills']}/{i['quotes']} quotes pnl {_num(i['pnl'])}" for v, i in row["maker_dry_run"].items()),
            f"- **Mean reversion:** samples {mean_reversion.get('samples_taken')}, warmed up {mean_reversion.get('warmed_up')}, "
            f"basis mean {_num(mean_reversion.get('basis_mean_bps'), 3)} bps sd {_num(mean_reversion.get('basis_stdev_bps'), 3)}, "
            f"max |z| {_num(mean_reversion.get('max_abs_zscore'), 2)}, stance {mean_reversion.get('stance')}, actions {mean_reversion.get('actions')}",
            f"- **Alerts:** ERROR {row['alerts']['ERROR']}, WARN {row['alerts']['WARN']}",
            "",
        ]
        if row["issues"]:
            lines += ["| Component | Check | Status | Detail |", "| --- | --- | --- | --- |"]
            for issue in row["issues"]:
                lines.append(
                    f"| {issue['component']} | {issue['check']} | {issue['status']} | "
                    f"{str(issue['detail']).replace('|', '/')[:240]} |"
                )
            lines.append("")
    return "\n".join(lines) + "\n"
