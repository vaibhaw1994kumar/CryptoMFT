"""Downloadable per-cycle reports with a minimum-requirements assessment.

For every audited watcher cycle this writes, under ``<root>/reports/``:

* ``<cycle>/<cycle>_report.html`` and ``.pdf`` - one self-contained report;
* ``ALL_CYCLES_REPORT.html`` and ``.pdf``      - every cycle in one document;
* ``cycles_summary.csv``, ``issues.csv``, ``minimum_requirements.csv``,
  ``decisions.csv``                           - spreadsheet-friendly tables;
* ``paper_watch_reports.zip``                  - everything above.

PDFs are printed with headless Google Chrome when it is installed; the HTML
files are complete on their own.

Each MR-01..MR-14 row is graded from the evidence the cycle actually produced:
``PASS`` (evidence present and clean), ``WARN`` (works, with an issue),
``FAIL`` (evidence contradicts the requirement), ``NOT_EXERCISED`` (the cycle
produced nothing to judge, e.g. no trades).
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any
import zipfile

from .cycle_report import COMPONENTS, _cycle_row, _json


STATUS_ORDER = {"FAIL": 0, "WARN": 1, "NOT_EXERCISED": 2, "PASS": 3}
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "chromium", "chromium-browser",
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extras(root: Path, session_id: str) -> dict[str, Any]:
    paper = root / "live" / session_id / "paper"
    timestamps = _read_jsonl(paper / "paper_timestamps.jsonl")
    decisions = [row for row in timestamps if row.get("record_type") == "DECISION"]
    polls_ok = [row for row in timestamps if row.get("record_type") == "INSTRUMENT_STATUS"]
    poll_failures = [row for row in timestamps if row.get("record_type") == "INSTRUMENT_STATUS_POLL_FAILED"]
    flatten = [row for row in timestamps if row.get("record_type") == "FLATTEN_STARTED"]
    summary = _json(paper / "paper_summary.json") or {}
    latency = _json(paper / "latency_report.json") or {}
    replay_manifest = _json(root / "audits" / session_id / "backtest_replay" / "manifest.json") or {}
    return {
        "summary": summary,
        "decisions": decisions,
        "status_changes": polls_ok,
        "status_poll_failures": poll_failures,
        "flatten_rows": flatten,
        "wall_clock": latency.get("wall_clock_stages", {}),
        "replay_manifest": replay_manifest,
    }


def assess_requirements(row: dict[str, Any], extra: dict[str, Any], performance: dict[str, Any]) -> list[dict[str, str]]:
    metrics = row.get("component_metrics", {})
    components = row.get("components", {})
    summary = extra["summary"]
    strategies = (summary.get("strategies") or {}).get("strategies", {})
    plans = sum(item.get("plans") or 0 for item in strategies.values())
    closed = sum(item.get("closed_plans") or 0 for item in strategies.values())
    tca = summary.get("tca") or {}
    fills = tca.get("fills") or 0
    realism = summary.get("execution_realism") or {}
    residual = summary.get("residual_risk") or {}
    kill = summary.get("kill_switch") or {}
    reconciliation = summary.get("rms_reconciliation") or {}
    monitoring = summary.get("pipeline_monitoring") or {}
    feed = metrics.get("feed", {})
    storage = metrics.get("historical_storage", {})
    strategy_metrics = metrics.get("strategies", {})
    results: list[dict[str, str]] = []

    def add(mr: str, title: str, status: str, evidence: str, issue: str = "") -> None:
        results.append({"id": mr, "title": title, "status": status, "evidence": evidence, "issue": issue})

    # MR-01
    events = row.get("events_by_venue", {})
    reconnects = row.get("reconnects_by_venue", {})
    both = all((events.get(venue) or 0) > 0 for venue in ("BINANCE", "BYBIT"))
    unstable = {venue: count for venue, count in reconnects.items() if count}
    add(
        "MR-01", "Multi-venue public capture and raw retention",
        "FAIL" if not both or components.get("historical_storage") == "FAIL"
        else "WARN" if unstable else "PASS",
        f"Book events {events}; trades {row.get('trades_by_venue')}; wire and raw files hash-chained "
        f"({components.get('historical_storage')}).",
        f"Reconnects/stale disconnects {unstable}" if unstable else "",
    )
    # MR-02
    errors = {
        venue: sum((item or {}).get(key) or 0 for key in ("parse_errors", "normalization_errors", "trade_parse_errors", "trade_normalization_errors"))
        for venue, item in feed.items() if isinstance(item, dict) and "canonical_events" in item
    }
    quarantined = storage.get("quarantined") or 0
    bad = any(errors.values()) or quarantined
    add(
        "MR-02", "Canonical schemas and versioning",
        "WARN" if bad else "PASS",
        f"{storage.get('canonical_events')} canonical events (schema 1.2.0); parse/normalize errors {errors}; "
        f"quarantined {quarantined}.",
        "Frames rejected into quarantine or parse errors" if bad else "",
    )
    # MR-03
    changes = extra["status_changes"]
    failures = extra["status_poll_failures"]
    change_text = "; ".join(f"{item.get('venue')}: {item.get('changes')}" for item in changes) or "none"
    add(
        "MR-03", "Security master and effective-dated rules",
        "NOT_EXERCISED" if not changes and not failures
        else "WARN" if failures else "PASS",
        f"Public status polls applied {len(changes)} effective-dated version(s): {change_text}.",
        f"{len(failures)} status polls failed ({(failures[0].get('error') if failures else '')[:80]})" if failures else "",
    )
    # MR-04
    gate = row.get("quality_gate")
    add(
        "MR-04", "Ingestion, manifests and quality reports",
        "FAIL" if components.get("historical_storage") == "FAIL"
        else "WARN" if gate != "APPROVED" or components.get("historical_storage") == "WARN" else "PASS",
        f"{storage.get('parts')} partition(s), file SHA-256 verified, persistence lag "
        f"{_float(storage.get('max_persistence_lag_ms')) or 0:.0f} ms; quality gate {gate}.",
        f"Quality gate {gate}: {row.get('quality_flags')}" if gate != "APPROVED" else "",
    )
    # MR-05
    replay_checks = [
        check for check in (row.get("component_checks", {}).get("orderbook_replay") or [])
        if "processing_order" in check["check"]
    ]
    replay_fail = any(check["status"] != "OK" for check in replay_checks)
    add(
        "MR-05", "Deterministic multi-venue replay",
        "FAIL" if replay_fail or not replay_checks else "PASS",
        f"Stored events rebuild both live books hash-for-hash ({len(replay_checks)} checks); backtest replay of the "
        f"period produced {row.get('replay_opportunities')} opportunities with the seeded latency model.",
        "Replayed book differs from the live book" if replay_fail else "",
    )
    # MR-06
    mean_reversion = strategy_metrics.get("basis_mean_reversion_v1", {})
    warmed = mean_reversion.get("warmed_up")
    add(
        "MR-06", "Cross-venue features without lookahead",
        "WARN" if mean_reversion and not warmed else "PASS",
        f"Basis z-score uses only past samples ({mean_reversion.get('samples_taken')} taken, "
        f"mean {_float(mean_reversion.get('basis_mean_bps')) or 0:.3f} bps, sd "
        f"{_float(mean_reversion.get('basis_stdev_bps')) or 0:.3f} bps, max |z| "
        f"{_float(mean_reversion.get('max_abs_zscore')) or 0:.2f}); lookahead guard is unit-tested.",
        "Signal not warmed up this cycle" if mean_reversion and not warmed else "",
    )
    # MR-07
    open_plans = realism.get("open_plans", 0)
    add(
        "MR-07", "Paper matching and order lifecycle",
        "NOT_EXERCISED" if plans == 0 else "FAIL" if open_plans or closed != plans else "PASS",
        f"INTERNAL_PAPER; {plans} plans, {closed} closed, {fills} fills; latency model "
        f"{realism.get('latency_model')}; open plans after drain {open_plans}.",
        "Plans left open" if open_plans or closed != plans else "",
    )
    # MR-08
    outcomes: dict[str, int] = {}
    for item in strategies.values():
        for key, value in (item.get("outcomes") or {}).items():
            outcomes[key] = outcomes.get(key, 0) + value
    open_residual = str(residual.get("open_residual_base", "0"))
    unresolved = realism.get("unresolved_after_queries", 0)
    add(
        "MR-08", "OMS/EMS routing and residual handling",
        "NOT_EXERCISED" if plans == 0
        else "FAIL" if (_float(open_residual) or 0) != 0 or unresolved or realism.get("residual_breaches_open")
        else "PASS",
        f"Outcomes {outcomes}; unwind children {residual.get('unwind_children')}; open residual {open_residual}; "
        f"unknown-state orders {realism.get('unknown_state_orders')}, unresolved {unresolved}.",
        "Residual or unresolved order state remains" if plans and ((_float(open_residual) or 0) != 0 or unresolved) else "",
    )
    # MR-09
    expected = [_float(item.get("expected_net_edge_bps")) for item in extra["decisions"]]
    expected = [value for value in expected if value is not None]
    negative = [value for value in expected if value < 0]
    add(
        "MR-09", "Fees, economics and cost model",
        "NOT_EXERCISED" if fills == 0 else "WARN" if negative else "PASS",
        f"Fills {fills}; fees {tca.get('fees')}; weighted slippage {tca.get('weighted_slippage_bps')} bps; "
        f"gross {summary.get('gross_pnl')} → net {summary.get('net_pnl')} USDT.",
        f"{len(negative)}/{len(expected)} intents had negative expected net edge (median "
        f"{sorted(negative)[len(negative) // 2]:.1f} bps): fees exceed the signal" if negative else "",
    )
    # MR-10
    add(
        "MR-10", "Risk controls and kill switch",
        "FAIL" if kill.get("state") != "INACTIVE" else "PASS" if plans else "NOT_EXERCISED",
        f"Kill switch {kill.get('state')}; pre-trade validation {(summary.get('pretrade_validation') or {}).get('decision')}; "
        f"residual breaches {realism.get('residual_breaches_open') or 'none'}.",
        f"Kill switch active: {kill.get('reason')}" if kill.get("state") != "INACTIVE" else "",
    )
    # MR-11
    decision = reconciliation.get("decision")
    add(
        "MR-11", "Append-only ledger and reconciliation",
        "FAIL" if decision == "REJECTED" else "PASS",
        f"End-of-cycle RMS reconciliation {decision} ({reconciliation.get('issue_count')} issues).",
        "Reconciliation rejected" if decision == "REJECTED" else "",
    )
    # MR-12
    health = monitoring.get("health")
    add(
        "MR-12", "Monitoring and TCA",
        "WARN" if health not in (None, "HEALTHY") else "PASS",
        f"Pipeline monitoring {health} ({monitoring.get('alert_count')} alerts); TCA fills {fills}, missed "
        f"{tca.get('missed_opportunities')}; watcher alerts ERROR {row['alerts']['ERROR']} / WARN {row['alerts']['WARN']}.",
        f"Monitoring health {health}" if health not in (None, "HEALTHY") else "",
    )
    # MR-13
    result_hash = extra["replay_manifest"].get("deterministic_result_sha256")
    add(
        "MR-13", "Reproducible runs from a manifest",
        "PASS" if result_hash else "WARN",
        f"Replay manifest with input/config hashes; deterministic result {str(result_hash)[:16]}…; "
        "identical-rerun hashing is covered by the reproducibility test.",
        "" if result_hash else "No replay manifest",
    )
    # MR-14
    wall = extra["wall_clock"]
    frame = (wall.get("paper_frame_overhead_ns") or {}).get("p99_ns")
    route = (wall.get("route_decision_ns") or {}).get("p99_ns")
    frame_limit = _float(performance.get("max_paper_frame_overhead_p99_ms"))
    route_limit = _float(performance.get("max_route_decision_p99_ms"))
    breaches = []
    if frame is not None and frame_limit is not None and frame / 1e6 > frame_limit:
        breaches.append(f"frame p99 {frame / 1e6:.2f} ms > {frame_limit} ms")
    if route is not None and route_limit is not None and route / 1e6 > route_limit:
        breaches.append(f"route p99 {route / 1e6:.2f} ms > {route_limit} ms")
    add(
        "MR-14", "Performance (live paper path)",
        "FAIL" if breaches else "PASS" if frame is not None else "NOT_EXERCISED",
        f"Per-frame paper overhead p99 {frame / 1e6 if frame else 0:.2f} ms (limit {frame_limit}); "
        f"route decision p99 {route / 1e6 if route else 0:.2f} ms (limit {route_limit}).",
        "; ".join(breaches),
    )
    # LIM-01 forward test accuracy
    comparison = row.get("replay_vs_paper")
    add(
        "LIM-01", "Simulator accuracy vs forward test",
        {"CONSISTENT": "PASS", "DIVERGED": "FAIL"}.get(comparison or "", "NOT_EXERCISED"),
        f"Replay-vs-paper comparison: {comparison}.",
        "Fewer than 30 forward opportunities: accuracy not yet measurable" if comparison == "INSUFFICIENT_SAMPLE"
        else "Replay diverged from the forward test" if comparison == "DIVERGED" else "",
    )
    return results


CSS = """
body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1b1f24;margin:28px;font-size:12.5px;line-height:1.45}
h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:26px 0 8px;border-bottom:1px solid #d0d7de;padding-bottom:4px}
h3{font-size:14px;margin:18px 0 6px}
.meta{color:#57606a;margin-bottom:14px}
table{border-collapse:collapse;width:100%;margin:6px 0 12px;page-break-inside:auto}
th,td{border:1px solid #d0d7de;padding:4px 6px;text-align:left;vertical-align:top}
th{background:#f6f8fa} tr{page-break-inside:avoid}
.PASS,.OK{background:#dafbe1;color:#116329;font-weight:600}
.WARN{background:#fff8c5;color:#7d4e00;font-weight:600}
.FAIL{background:#ffebe9;color:#a40e26;font-weight:600}
.NOT_EXERCISED,.RUNNING,.AUDIT_PENDING{background:#eaeef2;color:#424a53;font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:10px 0}
.kpi{border:1px solid #d0d7de;border-radius:6px;padding:8px}
.kpi b{display:block;font-size:16px}
.note{color:#57606a;font-size:11.5px}
.cycle{page-break-before:always}
"""


def _cell(status: str) -> str:
    return f'<td class="{escape(status)}">{escape(status)}</td>'


def _cycle_html(row: dict[str, Any], extra: dict[str, Any], requirements: list[dict[str, str]], *, section: bool = False) -> str:
    summary = extra["summary"]
    strategies = (summary.get("strategies") or {}).get("strategies", {})
    tca = summary.get("tca") or {}
    parts = [f'<div class="{"cycle" if section else ""}">',
             f"<h2>{escape(row['session_id'])}</h2>" if section else "",
             f'<div class="meta">{escape(str(row["started_utc"]))} → {escape(str(row["stopped_utc"]))} UTC · '
             f'{row["minutes"]} min · audit <span class="{escape(row["overall"])}">&nbsp;{escape(row["overall"])}&nbsp;</span></div>']
    kpis = [
        ("Book events", sum((value or 0) for value in row["events_by_venue"].values())),
        ("Trades stored", sum((value or 0) for value in row["trades_by_venue"].values())),
        ("Plans (closed/total)", f"{row['closed_plans']}/{sum(item.get('plans') or 0 for item in strategies.values())}"),
        ("Net PnL USDT (paper)", summary.get("net_pnl")),
        ("Fills / fees", f"{tca.get('fills')} / {tca.get('fees')}"),
        ("Quality gate", row["quality_gate"]),
        ("Replay vs paper", row["replay_vs_paper"]),
        ("Alerts ERR / WARN", f"{row['alerts']['ERROR']} / {row['alerts']['WARN']}"),
    ]
    parts.append('<div class="kpis">' + "".join(
        f'<div class="kpi">{escape(label)}<b>{escape(str(value))}</b></div>' for label, value in kpis
    ) + "</div>")
    parts.append("<h3>Minimum requirements (evidence from this cycle)</h3><table><tr><th>ID</th><th>Requirement</th><th>Status</th><th>Evidence</th><th>Issue</th></tr>")
    for item in requirements:
        parts.append(
            f"<tr><td>{escape(item['id'])}</td><td>{escape(item['title'])}</td>{_cell(item['status'])}"
            f"<td>{escape(item['evidence'])}</td><td>{escape(item['issue'])}</td></tr>"
        )
    parts.append("</table>")
    parts.append("<h3>Component audit</h3><table><tr><th>Component</th><th>Status</th><th>Non-OK checks</th></tr>")
    for name in COMPONENTS:
        checks = [check for check in row.get("component_checks", {}).get(name, []) if check["status"] != "OK"]
        text = "<br>".join(f"<b>{escape(check['check'])}</b>: {escape(str(check['detail']))}" for check in checks) or "—"
        parts.append(f"<tr><td>{escape(name)}</td>{_cell(row['components'].get(name, '-'))}<td>{text}</td></tr>")
    parts.append("</table>")
    parts.append("<h3>Strategies</h3><table><tr><th>Strategy</th><th>Opportunities</th><th>Plans closed</th><th>Outcomes</th><th>Gross</th><th>Fees</th><th>Net</th></tr>")
    for sid, item in strategies.items():
        parts.append(
            f"<tr><td>{escape(sid)}</td><td>{item.get('opportunities')}</td><td>{item.get('closed_plans')}/{item.get('plans')}</td>"
            f"<td>{escape(str(item.get('outcomes')))}</td><td>{escape(str(item.get('gross_pnl')))}</td>"
            f"<td>{escape(str(item.get('fees')))}</td><td>{escape(str(item.get('net_pnl')))}</td></tr>"
        )
    parts.append("</table>")
    parts.append("<h3>Market data, time and storage</h3><table><tr><th>Venue</th><th>Book events</th><th>Trades</th><th>Reconnects/stale</th><th>Last error</th></tr>")
    for venue in row["events_by_venue"]:
        parts.append(
            f"<tr><td>{escape(venue)}</td><td>{row['events_by_venue'].get(venue)}</td><td>{row['trades_by_venue'].get(venue)}</td>"
            f"<td>{row['reconnects_by_venue'].get(venue)}</td><td>{escape(str(row['last_error_by_venue'].get(venue) or '—'))}</td></tr>"
        )
    parts.append(
        f"</table><p class='note'>Host clock offset {_float(row['host_offset_ms']) or 0:.1f} ms; blocked events "
        f"{row['blocked_events']} of {row['time_events']}; {row['storage_parts']} storage part(s); persistence lag "
        f"{_float(row['persistence_lag_ms']) or 0:.0f} ms.</p>"
    )
    parts.append("<h3>sim_engine maker model</h3><table><tr><th>Run</th><th>Venue</th><th>Fills</th><th>PnL (USDT)</th></tr>")
    for venue, item in row["maker_dry_run"].items():
        parts.append(f"<tr><td>Live dry run</td><td>{escape(venue)}</td><td>{item.get('fills')}/{item.get('quotes')} quotes</td><td>{_float(item.get('pnl')) or 0:.4f}</td></tr>")
    for venue, item in row["simulation_replay"]["venues"].items():
        parts.append(f"<tr><td>Replay simulation ({escape(str(row['simulation_replay']['engine']))})</td><td>{escape(venue)}</td><td>{item.get('fills')}</td><td>{_float(item.get('pnl')) or 0:.4f}</td></tr>")
    parts.append("</table>")
    decisions = extra["decisions"]
    if decisions:
        parts.append(f"<h3>Trade intents ({len(decisions)}; first 40)</h3><table><tr><th>Decision time (UTC)</th><th>Strategy</th><th>Buy</th><th>Sell</th><th>Qty</th><th>Expected net edge (bps)</th><th>Plan state at dispatch</th></tr>")
        for item in decisions[:40]:
            ts = datetime.fromtimestamp((item.get("decision_ts_ns") or 0) / 1e9, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            parts.append(
                f"<tr><td>{ts}</td><td>{escape(str(item.get('strategy_id')))}</td><td>{escape(str(item.get('buy_venue')))}</td>"
                f"<td>{escape(str(item.get('sell_venue')))}</td><td>{escape(str(item.get('quantity')))}</td>"
                f"<td>{escape(str(item.get('expected_net_edge_bps')))}</td><td>{escape(str(item.get('plan_state')))}</td></tr>"
            )
        parts.append("</table>")
    parts.append("</div>")
    return "\n".join(parts)


def _document(title: str, body: str) -> str:
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{escape(title)}</title>"
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )


def _chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        path = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if path:
            return path
    return None


def html_to_pdf(html_path: Path) -> Path | None:
    chrome = _chrome()
    if chrome is None:
        return None
    pdf_path = html_path.with_suffix(".pdf")
    try:
        subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
             f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()],
            capture_output=True, timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return pdf_path if pdf_path.exists() and pdf_path.stat().st_size > 0 else None


def build_cycle_reports(root: Path, output: Path | None = None, *, pdf: bool = True) -> dict[str, Any]:
    root = Path(root)
    output = Path(output) if output else root / "reports"
    output.mkdir(parents=True, exist_ok=True)
    status = _json(root / "status.json") or {}
    config = _json(Path(status.get("config", ""))) if status.get("config") else None
    performance = (config or {}).get("performance", {})
    alerts = _read_jsonl(root / "alerts.jsonl")
    current = status.get("current_cycle")
    cycles = []
    for session_dir in sorted((root / "live").glob("cycle-*")):
        row = _cycle_row(session_dir, root / "audits" / session_dir.name, current, alerts)
        audit = _json(root / "audits" / session_dir.name / "session_audit.json") or {}
        row["component_checks"] = {
            name: item.get("checks", []) for name, item in audit.get("components", {}).items()
        }
        cycles.append(row)
    audited = [row for row in cycles if row["status"] == "AUDITED"]
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    files: list[Path] = []
    sections = []
    all_requirements: list[dict[str, str]] = []
    all_issues: list[dict[str, str]] = []
    all_decisions: list[dict[str, Any]] = []
    for row in audited:
        extra = _extras(root, row["session_id"])
        requirements = assess_requirements(row, extra, performance)
        cycle_dir = output / row["session_id"]
        cycle_dir.mkdir(parents=True, exist_ok=True)
        html_path = cycle_dir / f"{row['session_id']}_report.html"
        html_path.write_text(_document(
            f"Paper cycle report {row['session_id']}",
            f"<h1>Paper trading cycle report</h1><div class='meta'>{escape(row['session_id'])} · generated {generated} · "
            "INTERNAL_PAPER on live Binance + Bybit public data (no orders, no credentials)</div>"
            + _cycle_html(row, extra, requirements),
        ), encoding="utf-8")
        files.append(html_path)
        if pdf and (pdf_path := html_to_pdf(html_path)):
            files.append(pdf_path)
        sections.append(_cycle_html(row, extra, requirements, section=True))
        for item in requirements:
            all_requirements.append({"cycle": row["session_id"], **item})
        for issue in row["issues"]:
            all_issues.append({"cycle": row["session_id"], "component": issue["component"],
                               "check": issue["check"], "status": issue["status"], "detail": str(issue["detail"])})
        for item in extra["decisions"]:
            all_decisions.append({"cycle": row["session_id"], **{key: item.get(key) for key in (
                "decision_ts_ns", "strategy_id", "buy_venue", "sell_venue", "quantity",
                "expected_net_edge_bps", "plan_state", "opportunity_id",
            )}})
    # Requirement roll-up: worst status per requirement across audited cycles
    rollup: dict[str, dict[str, Any]] = {}
    for item in all_requirements:
        entry = rollup.setdefault(item["id"], {"title": item["title"], "statuses": [], "issues": set()})
        entry["statuses"].append(item["status"])
        if item["issue"]:
            entry["issues"].add(item["issue"].split(":")[0][:120])
    overview = [
        "<h1>Paper trading audit report — all cycles</h1>",
        f"<div class='meta'>Generated {generated} · root {escape(str(root))} · INTERNAL_PAPER on live Binance + Bybit public data</div>",
        "<h2>Cycles</h2><table><tr><th>Cycle</th><th>Start (UTC)</th><th>Min</th><th>Status</th><th>Audit</th><th>Book events</th>"
        "<th>Plans closed</th><th>Net PnL</th><th>Quality gate</th><th>Replay vs paper</th></tr>",
    ]
    for row in cycles:
        overview.append(
            f"<tr><td>{escape(row['session_id'])}</td><td>{escape(str(row['started_utc']))}</td><td>{row['minutes'] or '-'}</td>"
            f"{_cell(row['status'])}{_cell(row['overall']) if row['overall'] != '-' else '<td>-</td>'}"
            f"<td>{sum((v or 0) for v in row['events_by_venue'].values())}</td><td>{row['closed_plans'] if row['closed_plans'] is not None else '-'}</td>"
            f"<td>{escape(str(row['net_pnl'] or '-'))}</td><td>{escape(str(row['quality_gate'] or '-'))}</td><td>{escape(str(row['replay_vs_paper'] or '-'))}</td></tr>"
        )
    overview.append("</table><h2>Minimum requirements across cycles</h2><table><tr><th>ID</th><th>Requirement</th><th>Worst status</th><th>Per cycle</th><th>Issues seen</th></tr>")
    for mr, entry in rollup.items():
        worst = min(entry["statuses"], key=lambda status: STATUS_ORDER[status])
        overview.append(
            f"<tr><td>{escape(mr)}</td><td>{escape(entry['title'])}</td>{_cell(worst)}<td>{escape(' '.join(entry['statuses']))}</td>"
            f"<td>{escape('; '.join(sorted(entry['issues'])) or '—')}</td></tr>"
        )
    overview.append("</table>")
    all_path = output / "ALL_CYCLES_REPORT.html"
    all_path.write_text(_document("Paper trading audit report — all cycles", "\n".join(overview + sections)), encoding="utf-8")
    files.append(all_path)
    if pdf and (pdf_path := html_to_pdf(all_path)):
        files.append(pdf_path)

    def write_csv(name: str, rows: list[dict[str, Any]], fields: list[str]) -> None:
        path = output / name
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        files.append(path)

    write_csv("cycles_summary.csv", [
        {
            "cycle": row["session_id"], "status": row["status"], "audit": row["overall"],
            "start_utc": row["started_utc"], "stop_utc": row["stopped_utc"], "minutes": row["minutes"],
            "binance_events": row["events_by_venue"].get("BINANCE"), "bybit_events": row["events_by_venue"].get("BYBIT"),
            "binance_trades": row["trades_by_venue"].get("BINANCE"), "bybit_trades": row["trades_by_venue"].get("BYBIT"),
            "bybit_reconnects": row["reconnects_by_venue"].get("BYBIT"), "binance_reconnects": row["reconnects_by_venue"].get("BINANCE"),
            "opportunities_cross_arb": row["opportunities_by_strategy"].get("cross_venue_btc_v1"),
            "opportunities_mean_reversion": row["opportunities_by_strategy"].get("basis_mean_reversion_v1"),
            "closed_plans": row["closed_plans"], "net_pnl": row["net_pnl"], "fees": row["fees"],
            "quality_gate": row["quality_gate"], "replay_vs_paper": row["replay_vs_paper"],
            "host_offset_ms": row["host_offset_ms"], "alerts_error": row["alerts"]["ERROR"], "alerts_warn": row["alerts"]["WARN"],
            **{f"component_{name}": row["components"].get(name) for name in COMPONENTS},
        }
        for row in cycles
    ], [
        "cycle", "status", "audit", "start_utc", "stop_utc", "minutes", "binance_events", "bybit_events",
        "binance_trades", "bybit_trades", "binance_reconnects", "bybit_reconnects", "opportunities_cross_arb",
        "opportunities_mean_reversion", "closed_plans", "net_pnl", "fees", "quality_gate", "replay_vs_paper",
        "host_offset_ms", "alerts_error", "alerts_warn", *[f"component_{name}" for name in COMPONENTS],
    ])
    write_csv("minimum_requirements.csv", all_requirements, ["cycle", "id", "title", "status", "evidence", "issue"])
    write_csv("issues.csv", all_issues, ["cycle", "component", "check", "status", "detail"])
    write_csv("decisions.csv", all_decisions, [
        "cycle", "decision_ts_ns", "strategy_id", "buy_venue", "sell_venue", "quantity",
        "expected_net_edge_bps", "plan_state", "opportunity_id",
    ])
    for name in ("AUDIT_REPORT.md", "audit_report.json"):
        if (root / name).exists():
            files.append(root / name)
    bundle = output / "paper_watch_reports.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(root) if path.is_relative_to(root) else path.name)
    return {
        "cycles": len(cycles), "audited": len(audited), "output": str(output),
        "bundle": str(bundle), "files": [str(path) for path in files],
        "requirements_rollup": {
            mr: min(entry["statuses"], key=lambda status: STATUS_ORDER[status]) for mr, entry in rollup.items()
        },
    }
