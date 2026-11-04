"""Builds /Users/vaibhav/Documents/Newproject/v1_1_next_steps_report.html from out/queue_v2/runs_step8 (+ runs_ladder for v1 and the old step 7).
Every P&L is EXPLORATORY (2 partial days). usage: python3.12 build_step8_report.py"""
import glob, html, json
from pathlib import Path

X = Path("/Users/vaibhav/Documents/Newproject/out/queue_v2")
R8 = X / "runs_step8" / "perp_perp_maker_taker_v1"
RL = X / "runs_ladder" / "perp_perp_maker_taker_v1"
OUT = Path("/Users/vaibhav/Documents/Newproject/v1_1_next_steps_report.html")
esc = html.escape
DAYS = ("2022-01-05", "2022-01-13")


def load(root, tag, day):
    f = glob.glob(str(root / f"*_{tag}" / day / "summary.json"))
    return json.loads(Path(f[0]).read_text()) if f else None


def run_dir(root, tag, day):
    f = glob.glob(str(root / f"*_{tag}" / day))
    return Path(f[0]) if f else None


def row(s):
    q = s["qv2"]; c = q["maker_observations"]["maker_fill_causes"]; md = q["maker_diagnostics"]
    return {"fills": s["fills"], "trade": c.get("trade", 0), "xb": c.get("trade_confirmed_cross_before", 0), "xa": c.get("trade_confirmed_cross_after", 0),
            "cross": c.get("opposite_cross", 0), "vanish": c.get("delta_vanish", 0), "snap": c.get("snapshot", 0), "hedges": int(s["counters"].get("hedges_filled", 0)),
            "cash": s["cash_pnl"], "eq": s["ending_equity_common_timestamp"], "unh": s["exposure"]["time_net_nonzero_s"], "brk": len(s["breaker_trips"]),
            "armed": md.get("cross_armed", 0), "dropped": md.get("cross_dropped", 0), "small": md.get("cross_print_smaller_than_fill", 0)}


def f(x, d=3):
    return "–" if x is None else (f"{x:.{d}f}" if isinstance(x, float) else esc(str(x)))


COLS = [("fills", "maker fills", 0), ("trade", "by trade", 0), ("xb", "by cross + print before", 0), ("xa", "by cross + print after", 0), ("cross", "by unconfirmed cross", 0),
        ("vanish", "by delta-vanish", 0), ("hedges", "hedges filled", 0), ("cash", "cash P&amp;L", 4), ("eq", "equity, common ts", 4), ("unh", "time net-unhedged (s)", 2), ("brk", "breaker trips", 0),
        ("armed", "crosses armed", 0), ("dropped", "crosses dropped", 0)]


def table(rows, cols=COLS):
    h = "<table><thead><tr><th>run</th>" + "".join(f"<th>{n}</th>" for _, n, _ in cols) + "</tr></thead><tbody>"
    for lab, v in rows:
        h += f"<tr><td class=l>{lab}</td>" + "".join(f"<td>{f(float(v[k]) if d else int(v[k]), d)}</td>" for k, _, d in cols) + "</tr>"
    return h + "</tbody></table>"


def mk_table(summ_by_label):
    h = "<table><thead><tr><th>run</th><th>venue / role</th><th>fills</th><th>1 s (bps)</th><th>5 s</th><th>30 s</th></tr></thead><tbody>"
    for lab, s in summ_by_label:
        d = s["qv2"]["markout_by_venue_role"]
        for k in sorted(d):
            m = d[k]
            h += f"<tr><td class=l>{lab}</td><td class=l>{esc(k)}</td><td>{int(m['1s']['count'])}</td>" + "".join(f"<td>{m[h_]['qty_weighted_markout_bps']:.2f}</td>" for h_ in ("1s", "5s", "30s")) + "</tr>"
            lab = ""
        p = s["qv2"]["markout_summary"]
        h += f"<tr><td></td><td class=l><i>pooled (as before)</i></td><td>{int(p['1s']['count'])}</td>" + "".join(f"<td>{p[h_]['qty_weighted_markout_bps']:.2f}</td>" for h_ in ("1s", "5s", "30s")) + "</tr>"
    return h + "</tbody></table>"


def cross_section(day):
    d = run_dir(R8, "s8_cross_confirmed", day)
    cl = json.loads((d / "cross_log.json").read_text()) if d and (d / "cross_log.json").exists() else []
    byo = {}
    for r in cl:
        byo.setdefault((r["venue"], r["order_id"], r["cross_ts"]), []).append(r)
    out = []
    for k, rs in sorted(byo.items(), key=lambda kv: kv[0][2]):
        res = [r for r in rs if r["outcome"] != "armed"]
        o = res[0]["outcome"] if res else "unresolved (order left / run ended)"
        pv = res[0].get("print_volume") if res else None
        out.append((k, o, pv))
    return out


pre = {(o["day"], o["venue"], round(o["cross_event_ts"], 3)): o for o in json.loads((X / "cross_confirm" / "precheck.json").read_text())}

blocks = []
for day in DAYS:
    s7o, s7r, s8, s0 = load(RL, "s7_evidence_required", day), load(R8, "s7r_markout_split", day), load(R8, "s8_cross_confirmed", day), load(RL, "s0_v1_baseline", day)
    if not (s7r and s8):
        blocks.append(f"<h2>{day}</h2><p>replays not finished.</p>")
        continue
    rows = [("v1 baseline (frozen)", row(s0)), ("v1.1 step 7 (ladder run)", row(s7o)), ("v1.1 step 7 re-run, new snapshot", row(s7r)), ("v1.1 step 8: cross needs a print", row(s8))]
    ident = ""
    d7o, d7r = run_dir(RL, "s7_evidence_required", day), run_dir(R8, "s7r_markout_split", day)
    same = all((d7o / n).read_bytes() == (d7r / n).read_bytes() for n in ("trades.jsonl", "fill_events.jsonl", "fill_bucket_stats.json", "model_settings.json"))
    ident = f"<p>Switch-off identity: the step-7 re-run on the new snapshot ({'<b>byte-identical</b>' if same else '<b>DIFFERENT</b>'}) to the ladder's step 7 in <code>trades.jsonl</code>, <code>fill_events.jsonl</code>, <code>fill_bucket_stats.json</code> and <code>model_settings.json</code>. The new switch and the markout split change nothing when off.</p>"
    cr = ""
    for (venue, oid, cts), outc, pv in cross_section(day):
        p = pre.get((day, venue, round(cts, 3)))
        pp = "–" if not p else ("print before" if p["reaching_before"] else ("print after" if p["reaching_after"] else "no print"))
        cr += f"<tr><td class=l>{venue}</td><td>{cts:.3f}</td><td class=l>{pp}</td><td class=l>{esc(outc)}</td><td>{f(pv, 2)}</td></tr>"
    blocks.append(f"<h2>{day}</h2>{table(rows)}{ident}<h3>Each cross of the step-8 run</h3><table><thead><tr><th>venue</th><th>cross event time</th><th>raw-tape pre-check</th><th>engine outcome</th><th>confirming print size</th></tr></thead><tbody>{cr}</tbody></table>"
                  f"<p><small>The pre-check looked at the raw tape around each of the step-7 crosses. The engine then ran the new rule; later crosses can differ from the pre-check because a dropped cross changes the path of the run (the order keeps resting and may fill by a trade, or be cancelled at its 15 s limit).</small></p>"
                  f"<h3>Markouts by venue and role</h3>{mk_table([('step 7 re-run', s7r), ('step 8', s8)])}")

css = """body{font:15px/1.5 -apple-system,Segoe UI,sans-serif;max-width:1180px;margin:2rem auto;padding:0 1rem;color:#1b1f24}h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:2rem;border-bottom:1px solid #ddd}h3{font-size:1rem;margin-top:1.2rem}
table{border-collapse:collapse;font-size:12.5px;margin:.6rem 0 1rem;display:block;overflow-x:auto}th,td{border:1px solid #d5d9de;padding:3px 7px;text-align:right;white-space:nowrap}th{background:#f2f4f7}td.l,th:first-child{text-align:left}
.tag{display:inline-block;background:#fff3cd;border:1px solid #e6c200;border-radius:4px;padding:1px 8px;font-size:12px;font-weight:600}.note{background:#f6f8fa;border-left:4px solid #8a94a3;padding:.5rem .8rem;margin:.8rem 0}
code{background:#f1f3f5;padding:0 3px}small{color:#6b7280}@media(prefers-color-scheme:dark){body{background:#14171b;color:#e6e8eb}th{background:#222831}td,th{border-color:#363d47}.note{background:#1d2229}code{background:#232a33}.tag{background:#4a3f10;color:#fff}}"""

march = ""
mp = X / "logs" / "march_pool.log"
body = f"""<h1>v1.1 next steps: queue fix, cross-fill evidence, markouts by venue</h1>
<p><span class=tag>EXPLORATORY</span> 2022-01-05 and 2022-01-13 (two partial days), SOL perp, singapore latency, bybit_perp_mm fees, clean, size 1, seed 1. A handful of fills decides each row: read the direction and the cause mix, not the amounts.
<span class=tag>queue parameters: SPOT prior, perp-unvalidated</span> v1 stays frozen; nothing was synced to the VM.</p>

<h2>What was done</h2>
<ol>
<li><b>Queue fix.</b> Already v1.1 step 7 (<code>vanish_clears_queue=false</code>, <code>fill_evidence=trade_confirm</code>): a vanished level or a depth decrease moves the order to the front of the queue and never fills it; only a public print that reaches the order fills it. Step 7 shows no fills from a delta or a snapshot on either day. It is re-run below together with the new markout split, and the re-run is byte-identical to the ladder's step 7.</li>
<li><b>Book-cross fills.</b> New opt-in switch <code>maker.opposite_cross_confirm_ms = 500</code> (in the v1.1 profile; off in v1). A cross fills only if a print that reaches the order (sell aggressor at or below a bid) executed on the <b>same venue</b> within &plusmn;500 ms of the crossing event and after the order reached the exchange. A print before the cross fills it at the cross time; a print after fills it at that print's time; none: the cross is dropped and has no effect at all (no queue jump). The window is anchored at the first cross of a crossing episode. The fill keeps the cross's size; the print is evidence, not a size cap (counted when the print is smaller).</li>
<li><b>Markouts by venue and role.</b> The tracker now also keeps <code>venue/maker</code> and <code>venue/taker</code> blocks; the pooled numbers are unchanged.</li>
</ol>
<div class=note><b>Correction.</b> Maker orders rest on <b>both</b> venues (about half on Bybit, half on Binance) and the hedge goes to the other one, so "Bybit maker, Binance hedge" describes only half the chains. Each cross is therefore checked against the tape of the venue the order rests on. On both days the Binance trade tape is truncated at about 17 h; a cross after that point cannot be confirmed (none of the logged crosses falls there).<br><b>Finding.</b> Order prices are not on the tick grid (for example 168.23924536), so no print can be at exactly that price; &ldquo;a print at our price&rdquo; therefore means a print at or through it. The older <code>trade_after_front</code> rule asks for an exact price match and never fired in any run (0 confirmed quantity); it is left as is.</div>
{''.join(blocks)}
<h2>March 2024 (item 4)</h2>
<p id=march><b>Not run yet; queued.</b> v1.1 (the profile file, with the 500 ms cross rule) is set up for 2024-03-08 to 03-15 (SELECT 03-08..12, HOLDOUT 03-13..15; nothing is chosen from the holdout days). Each day needs all seven TRAIN-day <code>legacy_v0</code> replays (03-01..03-07) for the expected hedge move, and those belong to the Phase 1-lite harness and are still running (03-01..03 done). A waiting pool starts the days as soon as the last training replay lands, three at a time; one March day takes about 2.5 hours. Tested end to end on a 10-minute slice (summary, markouts by venue, profile-versus-config equality). Settings that differ from the January runs: hedge arrival on the truth book (the Phase 1-lite harness setting), no funding (no March file), Binance has top-of-book data only so unknown queues take the counted fallback of 25 (no fit), and the March rebuild acceptance was assessed for 03-01 only (Bybit 100%).</p>
<p>No v1 Phase 1-lite results existed to supersede: only the <code>legacy_v0</code> training replays, which stay valid as inputs. A SUPERSEDED note on the v1 grid plan is in <code>out/exec_layer_p1lite/SUPERSEDED_v1_grid.md</code>.</p>
<h2>Limits</h2>
<ul><li>Two partial days; a few fills per row; every P&amp;L is EXPLORATORY.</li><li>Markouts by venue rest on 2 to 11 fills per cell.</li><li>The fill size of a confirmed cross is the cross's, not the print's.</li></ul>
"""
OUT.write_text(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>v1.1 next steps</title><style>{css}</style></head><body>{body}</body></html>')
print("wrote", OUT, OUT.stat().st_size)
