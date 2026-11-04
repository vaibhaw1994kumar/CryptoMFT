"""Builds /Users/vaibhav/Documents/Newproject/v1_1_step9_report.html: the five corrections of 2026-11-04 (hedge move from market data, tick-rounded quotes, cross fills capped at the print,
Bybit-only makers in March, one hedge arrival book), the January re-run (runs_step9) and the March runs (runs_mar2024). Every P&L is EXPLORATORY. usage: python3.12 build_step9_report.py"""
import glob, html, json
from pathlib import Path

X = Path("/Users/vaibhav/Documents/Newproject/out/queue_v2")
RL = X / "runs_ladder" / "perp_perp_maker_taker_v1"
R8 = X / "runs_step8" / "perp_perp_maker_taker_v1"
R9 = X / "runs_step9" / "perp_perp_maker_taker_v1_1"
RM = X / "runs_mar2024" / "mar2024_v1_1" / "v1_1"
OUT = Path("/Users/vaibhav/Documents/Newproject/v1_1_step9_report.html")
esc = html.escape
JAN = ("2022-01-05", "2022-01-13")
MAR = ["2024-03-%02d" % d for d in range(8, 16)]
SELECT = set(MAR[:5])


def load(root, tag, day):
    f = glob.glob(str(root / f"*_{tag}" / day / "summary.json"))
    return json.loads(Path(f[0]).read_text()) if f else None


def load_mar(day):
    p = RM / day / "summary.json"
    return json.loads(p.read_text()) if p.exists() else None


def f(x, d=3):
    return "–" if x is None else (f"{x:.{d}f}" if isinstance(x, float) else esc(str(x)))


def row(s):
    q = s["qv2"]; c = q["maker_observations"]["maker_fill_causes"]; md = q["maker_diagnostics"]
    return {"sig": s["signals"], "fills": s["fills"], "trade": c.get("trade", 0), "xb": c.get("trade_confirmed_cross_before", 0), "xa": c.get("trade_confirmed_cross_after", 0),
            "cross": c.get("opposite_cross", 0), "vanish": c.get("delta_vanish", 0), "snap": c.get("snapshot", 0), "hedges": int(s["counters"].get("hedges_filled", 0)),
            "cash": s["cash_pnl"], "eq": s["ending_equity_common_timestamp"], "unh": s["exposure"]["time_net_nonzero_s"], "brk": len(s["breaker_trips"]),
            "armed": md.get("cross_armed", 0), "dropped": md.get("cross_dropped", 0), "small": md.get("cross_print_smaller_than_fill", 0),
            "uqfb": md.get("unknown_queue_fit_missing_venue_used_floor", 0), "uqn": md.get("unknown_queue_assigned_total", 0)}


COLS = [("sig", "signals", 0), ("fills", "maker fills", 0), ("trade", "by trade", 0), ("xb", "cross + print before", 0), ("xa", "cross + print after", 0), ("cross", "unconfirmed cross", 0),
        ("vanish", "delta-vanish", 0), ("hedges", "hedges filled", 0), ("cash", "cash P&amp;L", 4), ("eq", "equity, common ts", 4), ("unh", "net-unhedged (s)", 1), ("brk", "breaker trips", 0),
        ("armed", "crosses armed", 0), ("dropped", "crosses dropped", 0)]


def table(rows, cols=COLS):
    h = "<table><thead><tr><th>run</th>" + "".join(f"<th>{n}</th>" for _, n, _ in cols) + "</tr></thead><tbody>"
    for lab, v in rows:
        h += f"<tr><td class=l>{lab}</td>" + "".join(f"<td>{f(float(v[k]) if d else int(v[k]), d)}</td>" for k, _, d in cols) + "</tr>"
    return h + "</tbody></table>"


def mk_rows(lab, s, keys=None):
    d = s["qv2"]["markout_by_venue_role"] or {}
    out = ""
    for k in sorted(d):
        if keys and k not in keys:
            continue
        m = d[k]
        out += f"<tr><td class=l>{lab}</td><td class=l>{esc(k)}</td><td>{int(m['1s']['count'])}</td>" + "".join(f"<td>{m[h_]['qty_weighted_markout_bps']:.2f}</td>" for h_ in ("1s", "5s", "30s")) + "</tr>"
        lab = ""
    return out


MK_HEAD = "<table><thead><tr><th>run</th><th>venue / role</th><th>fills</th><th>1 s (bps)</th><th>5 s</th><th>30 s</th></tr></thead><tbody>"

hm = {k: json.loads(p.read_text()) for k, p in (("jan", X / "hedge_move" / "jan_train_for_2022-01-13.json"), ("mar", X / "hedge_move" / "mar_train_2024-03-01_07.json")) if p.exists()}
leg = json.loads((X / "hedge_move" / "legacy_trained_values.json").read_text())


def hm_row(name, d, legacy):
    b, a = d["stats"]["bid"], d["stats"]["ask"]
    lb = legacy["bid"]
    return (f"<tr><td class=l>{name}</td><td class=l>{esc(d['days'][0])} … {esc(d['days'][-1])} ({len(d['days'])} d)</td><td>{b['n']}</td><td>{b['mean_bps']:.3f} &plusmn; {b['se_bps']:.3f}</td><td>{b['median_bps']:.2f}</td>"
            f"<td>{a['n']}</td><td>{a['mean_bps']:.3f} &plusmn; {a['se_bps']:.3f}</td><td>{lb[0]:.3f} (n={lb[1]})</td></tr>")


hm_table = ("<table><thead><tr><th>dataset</th><th>TRAIN days</th><th>bid events</th><th>bid move, bps (mean &plusmn; s.e.)</th><th>bid median</th><th>ask events</th><th>ask move (mean &plusmn; s.e.)</th>"
            "<th>old legacy-trained bid move</th></tr></thead><tbody>"
            + (hm_row("January 2022", hm["jan"], leg["jan_legacy"]) if "jan" in hm else "") + (hm_row("March 2024", hm["mar"], leg["mar_legacy"]) if "mar" in hm else "") + "</tbody></table>")

# ---- January
jan_blocks = ""
for day in JAN:
    s0, s7, s8, s9 = load(RL, "s0_v1_baseline", day), load(RL, "s7_evidence_required", day), load(R8, "s8_cross_confirmed", day), load(R9, "s9_v1_1", day)
    if not s9:
        jan_blocks += f"<h3>{day}</h3><p><b>Not finished.</b></p>"
        continue
    rows = [("v1 baseline (frozen)", row(s0)), ("v1.1 step 7: trade-confirmed", row(s7)), ("v1.1 step 8: cross needs a print", row(s8)), ("v1.1 step 9: all corrections", row(s9))]
    rej = {k: v for k, v in (s9.get("strategy_rejects") or {}).items() if v}
    tr = s9["training"]
    jan_blocks += (f"<h3>{day}</h3>{table(rows)}"
                   f"<p><small>Step 9 settings: hedge move used bid {s9['expected_hedge_move_bps_used']['bid']:.3f} / ask {s9['expected_hedge_move_bps_used']['ask']:.3f} bps ({esc(tr['status'])}); "
                   f"ticks declared {esc(json.dumps(s9['venue_ticks_declared']))}, engine used {esc(json.dumps(s9['tick_size_used']))}; hedge arrival book <b>{esc(s9['hedge_arrival_book'])}</b>; "
                   f"makers on {esc(', '.join(s9['maker_venues']) or 'both venues')}; quotes refused after rounding onto the touch: {rej.get('tick_rounding_no_improve', 0)}; "
                   f"crosses where the print was smaller than the order: {row(s9)['small']:.0f}.</small></p>"
                   f"<h4>Markouts by venue and role</h4>{MK_HEAD}{mk_rows('step 8', s8)}{mk_rows('step 9', s9)}</tbody></table>")

# ---- March
mar_rows, mk_html, done = [], "", 0
tot = {"sig": 0, "fills": 0, "cash": 0.0, "eq": 0.0, "hedges": 0, "uqfb": 0, "uqn": 0}
for day in MAR:
    s = load_mar(day)
    if not s:
        mar_rows.append((f"{day} ({'SELECT' if day in SELECT else 'HOLDOUT'})", None))
        continue
    r = row(s)
    done += 1
    for k in tot:
        tot[k] += r[k]
    mar_rows.append((f"{day} ({'SELECT' if day in SELECT else 'HOLDOUT'})", r))
    mk_html += mk_rows(day, s)
    s_any = s
mh = "<table><thead><tr><th>day</th>" + "".join(f"<th>{n}</th>" for _, n, _ in COLS) + "<th>unknown queues: fallback 25 used / assigned</th></tr></thead><tbody>"
for lab, r in mar_rows:
    if r is None:
        mh += f"<tr><td class=l>{lab}</td><td class=l colspan={len(COLS) + 1}>not finished</td></tr>"
    else:
        mh += f"<tr><td class=l>{lab}</td>" + "".join(f"<td>{f(float(r[k]) if d else int(r[k]), d)}</td>" for k, _, d in COLS) + f"<td>{r['uqfb']:.0f} / {r['uqn']:.0f}</td></tr>"
mh += (f"<tr><td class=l><b>total, {done} of {len(MAR)} days</b></td><td>{tot['sig']}</td><td>{tot['fills']}</td>" + "<td></td>" * 5 + f"<td>{tot['hedges']}</td><td>{tot['cash']:.4f}</td><td>{tot['eq']:.4f}</td>"
       + "<td></td>" * 4 + f"<td>{tot['uqfb']:.0f} / {tot['uqn']:.0f}</td></tr></tbody></table>")
march_sec = (f"<p>{done} of {len(MAR)} days finished. SELECT = 03-08..03-12, HOLDOUT = 03-13..03-15 (nothing is chosen from the holdout days). Bybit is the only maker venue; the hedge goes to Binance.</p>{mh}"
             f"<h4>Markouts by venue and role (per day)</h4>{MK_HEAD}{mk_html}</tbody></table>" if done else "<p><b>Not finished.</b></p>")

css = """body{font:15px/1.5 -apple-system,Segoe UI,sans-serif;max-width:1180px;margin:2rem auto;padding:0 1rem;color:#1b1f24}h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:2rem;border-bottom:1px solid #ddd}h3{font-size:1rem;margin-top:1.2rem}h4{font-size:.95rem;margin:.8rem 0 0}
table{border-collapse:collapse;font-size:12.5px;margin:.6rem 0 1rem;display:block;overflow-x:auto}th,td{border:1px solid #d5d9de;padding:3px 7px;text-align:right;white-space:nowrap}th{background:#f2f4f7}td.l,th:first-child{text-align:left}
.tag{display:inline-block;background:#fff3cd;border:1px solid #e6c200;border-radius:4px;padding:1px 8px;font-size:12px;font-weight:600}.note{background:#f6f8fa;border-left:4px solid #8a94a3;padding:.5rem .8rem;margin:.8rem 0}
.warn{background:#fff4e5;border-left:4px solid #e08a00;padding:.5rem .8rem;margin:.8rem 0}
code{background:#f1f3f5;padding:0 3px}small{color:#6b7280}@media(prefers-color-scheme:dark){body{background:#14171b;color:#e6e8eb}th{background:#222831}td,th{border-color:#363d47}.note{background:#1d2229}.warn{background:#2c2417}code{background:#232a33}.tag{background:#4a3f10;color:#fff}}"""

body = f"""<h1>v1.1 step 9: five corrections, January re-run, March 2024</h1>
<p><span class=tag>EXPLORATORY</span> Every P&amp;L here is exploratory: two partial January days and eight March days, a few fills per day. <span class=tag>queue parameters: SPOT prior, perp-unvalidated</span>
v1 stays frozen, nothing was synced to the VM, nothing was committed. Code snapshot <code>snap_qv2i</code>; the v1.1 profile file carries every switch below.</p>

<h2>What was fixed</h2>
<table><thead><tr><th>#</th><th>problem</th><th>fix (switch)</th></tr></thead><tbody>
<tr><td>1</td><td class=l>the expected hedge move was learned from <code>legacy_v0</code> replays</td><td class=l>estimated from market data only: the Binance mid move 0 to 1 s after each Bybit print on our side, TRAIN days only (<code>new_backtest/market_hedge_move.py</code>)</td></tr>
<tr><td>2</td><td class=l>quotes off the tick grid</td><td class=l><code>arb.round_quotes_to_tick</code>: bids down, asks up, before the improve-the-touch check and the edge; ticks declared per dataset; a quote that rounds onto the touch is refused, not joined</td></tr>
<tr><td>3</td><td class=l>a confirmed cross filled the full order</td><td class=l><code>maker.opposite_cross_cap_to_print</code>: fill = min(order remaining, unused volume of the reaching prints); each print is spent once; a part-filled order waits for more prints inside its window; the queue position is not reset by a part fill</td></tr>
<tr><td>4</td><td class=l>March makers on Binance (no depth there)</td><td class=l><code>arb.maker_venues = [bybit]</code>, set by the March runner only; January still rests makers on both venues (Binance has depth in January)</td></tr>
<tr><td>5</td><td class=l>hedge priced on a different book in January and March</td><td class=l><code>hedge_arrival_book: truth</code> in the v1.1 profile; the runners no longer override it</td></tr>
</tbody></table>
<p>With the new switches off the replay is byte-identical to the previous code (<code>trades.jsonl</code>, <code>fill_events.jsonl</code>, <code>fill_bucket_stats.json</code>, <code>model_settings.json</code>, 4-hour January slice, <code>identity_step9.json</code>). The first March pool had already started when the corrections were requested; its outputs (03-09 and 03-10 finished, three more killed) are in <code>runs_mar2024_SUPERSEDED_pre_fix</code> and are not used here.</p>

<h2>Expected hedge move: the new number is less adverse, not more</h2>
<div class=warn>The review assumed that the legacy-trained move was too optimistic because of fills no trade supports. The data say the opposite: the market-data estimate is <b>less</b> adverse than the legacy-trained one, so v1.1 now quotes <b>more</b> aggressively than before. Two reasons it may still be too optimistic: the legacy number includes the cost of executing the hedge (crossing the spread, latency) and real adverse selection after our fills, while the market-data number is the unconditional drift of the Binance mid after a Bybit print on our side. The conditional number needs real fills. Whether to add a slippage term or a haircut is your decision; it was not changed here.</div>
{hm_table}
<p><small>Bid = mid change after a sell-aggressor print (our passive bid is hit, the hedge sells; negative = adverse). One event per (exchange time, aggressor side). A pair counts only when the Binance book was updated within 2 s before the print and before t+1 s. January: most Bybit prints met a stale Binance book (for example 01-06: 14,860 of 16,229 events), so the January estimate rests on the moments the Binance feed was alive. 2022-01-05 has no earlier day with a usable Binance book, so its move stays 0 (no information), as before.</small></p>

<h2>January re-run</h2>
{jan_blocks}

<h2>March 2024</h2>
{march_sec}

<h2>Limits</h2>
<ul><li>Every P&amp;L is EXPLORATORY; fills per day are in single or low double digits.</li><li>Binance March data is top of book only: Binance unknown-queue draws use the counted fallback of 25 (column "unknown queues"); with Bybit as the only maker venue this applies to Bybit orders resting at levels absent from the ob500 book.</li>
<li>No funding is settled for March (no file). March rebuild acceptance was assessed for 03-01 only (Bybit 100%).</li>
<li>Cross fills cap at the reaching prints in the window; prints are spent once per fill, in order of arrival of the orders, not by price priority.</li>
<li>The January hedge move rests on a small, selected sample (stale Binance feed).</li></ul>
"""
OUT.write_text(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>v1.1 step 9</title><style>{css}</style></head><body>{body}</body></html>')
print("wrote", OUT, OUT.stat().st_size, "march days", done)
