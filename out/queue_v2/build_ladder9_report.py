"""/Users/vaibhav/Documents/Newproject/v1_1_fix_ladder_report.html: the hedge-move probe, the one-fix-at-a-time ladder on four 2-hour March slices, the tick check. EXPLORATORY."""
import html, json
from pathlib import Path
X = Path("/Users/vaibhav/Documents/Newproject/out/queue_v2")
R = X / "runs_ladder9" / "ladder9"
OUT = Path("/Users/vaibhav/Documents/Newproject/v1_1_fix_ladder_report.html")
DAYS = ["2024-03-08", "2024-03-09", "2024-03-10", "2024-03-11"]
ROWS = [("L0_step8", "8: v1.1 steps 1-8 (legacy-trained move −4.29, unrounded, uncapped, both venues, hedge on view book)"), ("L1_hedge_move_probe", "+ fix 1: hedge move = probe number"),
        ("L2_tick_rounding", "+ fix 2: tick rounding"), ("L3_cross_cap", "+ fix 3: cross fill capped at the print"), ("L4_bybit_only_makers", "+ fix 4: Bybit-only makers"),
        ("L5_hedge_on_arrival_book", "+ fix 5: hedge priced on the book at arrival")]
esc = html.escape
probe = json.loads((X / "hedge_move" / "probe_result.json").read_text())
tick = json.loads((X / "tick_check_step9.json").read_text())
mk = json.loads((X / "hedge_move" / "market_data_mar.json").read_text()) if (X / "hedge_move" / "market_data_mar.json").exists() else None
def S(tag, d):
    p = R / tag / d / "summary.json"
    return json.loads(p.read_text()) if p.exists() else None
def info(s):
    c = s["qv2"]["maker_observations"]["maker_fill_causes"]; m = (s["qv2"]["markout_by_venue_role"] or {}).get("bybit/maker")
    return {"sig": s["signals"], "fills": s["fills"], "causes": c, "ref": (s.get("strategy_rejects") or {}).get("tick_rounding_no_improve", 0), "cash": s["cash_pnl"],
            "mo_n": int(m["30s"]["count"]) if m else 0, "mo": m["30s"]["qty_weighted_markout_bps"] if m and m["30s"]["count"] else None,
            "venues": sorted({k.split("/")[0] for k in (s["qv2"]["markout_by_venue_role"] or {}) if k.endswith("/maker")}), "unh": s["exposure"]["time_net_nonzero_s"]}
def cstr(c): return ", ".join(f"{k} {v}" for k, v in sorted(c.items())) or "–"
tot_rows, det_rows = "", ""
for tag, lab in ROWS:
    infos = [(d, S(tag, d)) for d in DAYS]
    done = [(d, info(s)) for d, s in infos if s]
    if not done:
        tot_rows += f"<tr><td class=l>{esc(lab)}</td><td class=l colspan=7>not finished</td></tr>"; continue
    cs = {}
    for _, i in done:
        for k, v in i["causes"].items(): cs[k] = cs.get(k, 0) + v
    n30 = sum(i["mo_n"] for _, i in done); w = sum(i["mo"] * i["mo_n"] for _, i in done if i["mo"] is not None)
    tot_rows += (f"<tr><td class=l>{esc(lab)}</td><td>{len(done)}/4</td><td>{sum(i['sig'] for _, i in done)}</td><td>{sum(i['fills'] for _, i in done)}</td><td class=l>{esc(cstr(cs))}</td>"
                 f"<td>{sum(i['ref'] for _, i in done)}</td><td>{sum(i['cash'] for _, i in done):.4f}</td><td>{(w / n30 if n30 else float('nan')):.2f} (n={n30})</td></tr>")
    for d, i in done:
        det_rows += (f"<tr><td class=l>{tag}</td><td>{d}</td><td>{i['sig']}</td><td>{i['fills']}</td><td class=l>{esc(cstr(i['causes']))}</td><td>{i['ref']}</td><td>{i['cash']:.4f}</td>"
                     f"<td>{'–' if i['mo'] is None else f'{i[chr(109)+chr(111)]:.2f}'} (n={i['mo_n']})</td><td class=l>{esc(', '.join(i['venues']))}</td></tr>")
tk = "".join(f"<tr><td class=l>{r['venue']}</td><td>{r['day']}</td><td>{r['declared_tick']}</td><td>{r['min_adjacent_gap']:.4f}</td><td>{r['gcd_snapshot_prices']}</td><td>{r['gcd_event_prices'] if r['gcd_event_prices'] else '–'}</td></tr>"
             if r['min_adjacent_gap'] else f"<tr><td class=l>{r['venue']}</td><td>{r['day']}</td><td>{r['declared_tick']}</td><td>– (L1)</td><td>{r['gcd_snapshot_prices']}</td><td>–</td></tr>" for r in tick)
pd = "".join(f"<tr><td>{d}</td><td>{v['orders']}</td><td>{v['fills']}</td><td class=l>{esc(cstr(v['causes']))}</td><td>{v['mean_bps']:.3f}</td></tr>" for d, v in probe["per_day"].items())
css = """body{font:15px/1.5 -apple-system,Segoe UI,sans-serif;max-width:1180px;margin:2rem auto;padding:0 1rem;color:#1b1f24}h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:2rem;border-bottom:1px solid #ddd}
table{border-collapse:collapse;font-size:12.5px;margin:.6rem 0 1rem;display:block;overflow-x:auto}th,td{border:1px solid #d5d9de;padding:3px 7px;text-align:right;white-space:nowrap}th{background:#f2f4f7}td.l,th:first-child{text-align:left}
.tag{display:inline-block;background:#fff3cd;border:1px solid #e6c200;border-radius:4px;padding:1px 8px;font-size:12px;font-weight:600}.note{background:#f6f8fa;border-left:4px solid #8a94a3;padding:.5rem .8rem;margin:.8rem 0}
small{color:#6b7280}@media(prefers-color-scheme:dark){body{background:#14171b;color:#e6e8eb}th{background:#222831}td,th{border-color:#363d47}.note{background:#1d2229}.tag{background:#4a3f10;color:#fff}}"""
body = f"""<h1>v1.1 fixes one at a time, the hedge-move probe, tick check</h1>
<p><span class=tag>EXPLORATORY</span> Four 2-hour March slices (00:00-02:00 UTC of 2024-03-08..11), Bybit ob500 book, Binance bookTicker (top of book). All P&amp;L is exploratory: a few fills per slice.</p>
<h2>1. Hedge move from the probe</h2>
<p>A 1 SOL Bybit bid is kept resting at the strategy's usual quote depth (Binance best bid less 6.546 bps, rounded down to 0.001; one order per 15 s slot, re-placed at the next slot after a fill or expiry), filled under the v1.1 rules. For each fill, the Binance mid move over the next 1 s. TRAIN slices 03-01..03-03.</p>
<table><thead><tr><th>measure</th><th>bid move, bps</th><th>n</th></tr></thead><tbody>
<tr><td class=l><b>probe (fills of a resting quote)</b></td><td><b>{probe['probe_bid_move_bps']:.3f}</b> (median {probe['median_bps']:.3f}, s.e. {probe['se_bps']:.3f})</td><td>{probe['n']}</td></tr>
<tr><td class=l>market data, every print on our side (full days 03-01..07)</td><td>−0.654</td><td>2,273,725</td></tr>
<tr><td class=l>old legacy_v0-trained</td><td>−4.285</td><td>510</td></tr></tbody></table>
<table><thead><tr><th>day</th><th>orders</th><th>fills</th><th>by cause</th><th>mean move</th></tr></thead><tbody>{pd}</tbody></table>
<p><small>The probe agrees with the market-data number and not with the legacy-trained one. Fills of the probe are real-rule fills, so adverse selection of the fill itself is inside the number; the 1 s window starts at the fill time, not after hedge latency.</small></p>
<h2>2. Ladder (cumulative, from step 8), sums over the finished slices</h2>
<table><thead><tr><th>row</th><th>slices</th><th>signals</th><th>maker fills</th><th>fills by cause</th><th>quotes refused by rounding</th><th>cash P&amp;L</th><th>Bybit maker markout 30 s, bps</th></tr></thead><tbody>{tot_rows}</tbody></table>
<h3>Per slice</h3>
<table><thead><tr><th>row</th><th>slice</th><th>signals</th><th>fills</th><th>by cause</th><th>refused</th><th>cash</th><th>Bybit maker markout 30 s</th><th>maker venues seen</th></tr></thead><tbody>{det_rows}</tbody></table>
<h2>3. Tick check</h2>
<table><thead><tr><th>venue</th><th>day</th><th>declared tick</th><th>smallest gap between adjacent levels</th><th>gcd of snapshot prices</th><th>gcd of book_events prices</th></tr></thead><tbody>{tk}</tbody></table>
<p><small>Bybit: declared 0.005 (Jan 2022) and 0.001 (Mar 2024) match the book exactly. Binance declared 0.001: March matches. January 2022 is not clean: on 01-05 the smallest step is 0.001, but on 01-13 book_events prices sit on a 0.01 grid and the smallest snapshot gap is 0.002, so the true tick there is probably 0.01; the 0.001 declaration is the finest grid and is not wrong for 01-05, but Binance-resting quotes on 01-13 can be 0.001-off a real 0.01 grid.</small></p>
"""
OUT.write_text(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>v1.1 fix ladder</title><style>{css}</style></head><body>{body}</body></html>')
print("wrote", OUT)
