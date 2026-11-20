"""Phase 2 F02 a (owner 20 Nov): the per-symbol size envelope table. Run wrapper: the venue filters and Bybit tiers come from crypto_stat_arb.contract_master.perp_envelope_filters (the public dump of
20 Nov 2026, MEASURED: current rules, not historical); the SOLUSDT market cells come from the size ladder (ladder_summary.json, MEASURED on the 29 Oct 2026 capture, EXPLORATORY: that capture is
REJECTED / CRITICAL, mechanics only). Every cell is labelled ASSUMED, MEASURED, APPROVED or NO DATA. Nothing is extrapolated from SOL to another symbol.
Writes envelope.json and ENVELOPE.md next to this file."""
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path("/Users/vaibhav/Documents/Newproject")
sys.path.insert(0, str(ROOT / "crypto_MFT_arb_platform/src"))
from crypto_stat_arb.contract_master import perp_envelope_filters  # noqa: E402

HERE = Path(__file__).resolve().parent
RAW = ROOT / "crypto_MFT_arb_platform/config/contract_master/raw/20261120T083931Z"
SPOT22 = ["AAVE", "ADA", "APT", "ARB", "ATOM", "AVAX", "BNB", "BTC", "DOGE", "DOT", "ENA", "ETH", "FIL", "INJ", "LTC", "MNT", "NEAR", "OP", "SOL", "SUI", "UNI", "XRP"]
EXTRA8 = ["LDO", "LINK", "LIT", "TON", "TRX", "VVV", "WLD", "ONDO"]          # new_backtest/config.py ALL_PAIRS beyond the 22; the owner names the remaining 10-15
PARENTS = (20_000, 30_000, 50_000)
LEVERAGE = Decimal("10")                                                       # A8 account leverage (ASSUMED, the instrument rule used in v1_16/v1_17)


def cell(value, label, note=None):
    return {"value": value, "label": label, **({"note": note} if note else {})}


def bybit_margin(tiers, notional):
    """Initial and maintenance margin at `notional` on Bybit's public tiers, the initial rate raised to 1/account leverage (as with_account_leverage does)."""
    for t in tiers:
        if Decimal(str(notional)) <= Decimal(t["max_notional"]):
            im_rate = max(Decimal(t["initial_rate"]), 1 / LEVERAGE)
            mm = Decimal(str(notional)) * Decimal(t["maintenance_rate"]) - Decimal(t["mm_deduction"] or "0")
            return {"tier_id": t["id"], "initial_usdt": float(Decimal(str(notional)) * im_rate), "maintenance_usdt": float(mm), "tier_max_notional": t["max_notional"]}
    return None


def main():
    symbols = [s + "USDT" for s in SPOT22 + EXTRA8]
    filters = perp_envelope_filters(RAW, symbols)
    ladder = json.loads((HERE / "ladder_summary.json").read_text()) if (HERE / "ladder_summary.json").exists() else None
    rows = []
    for sym in symbols:
        f = filters[sym]
        both = f["BINANCE"] is not None and f["BYBIT"] is not None
        r = {"symbol": sym, "spot22": sym[:-4] in SPOT22, "listed_both_perp_venues": cell(both, "MEASURED", "public dump 20 Nov 2026")}
        r["parent_max_usdt"] = cell("20,000-30,000 (up to 50,000)", "APPROVED", "owner target 20 Nov")
        if both:
            r["child_max"] = cell({"clip_cap_usdt": 25000, "bybit_postonly_max_qty": f["BYBIT"]["max_postonly_qty"], "binance_limit_max_qty": f["BINANCE"]["max_limit_qty"],
                                   "binance_market_max_qty": f["BINANCE"]["max_market_qty"], "bybit_market_max_qty": f["BYBIT"]["max_market_qty"]},
                                  "ASSUMED cap / MEASURED venue maxima", "child = min(25,000 USDT clip cap (arb.max_clip_notional, ASSUMED), the venue maxima in base); the USDT value of a venue maximum needs a price (not read)")
            r["venue_filters"] = cell({v: {k: f[v][k] for k in ("tick", "step", "min_qty", "min_notional")} for v in ("BYBIT", "BINANCE")}, "MEASURED", "public dump 20 Nov 2026 (current rules)")
        else:
            r["child_max"] = cell(None, "NO DATA", "not listed as a trading USDT perpetual on both venues")
            r["venue_filters"] = cell({v: (None if f[v] is None else {k: f[v][k] for k in ("tick", "step", "min_qty", "min_notional")}) for v in ("BYBIT", "BINANCE")}, "MEASURED")
        r["worst_outstanding_maker_fill_during_cancel"] = cell("the whole resting child", "ASSUMED", "a cancel races the fill: the full child can fill before the cancel lands")
        r["caps"] = cell({"net_ceiling": "1.25 x parent", "gross_per_venue": "2 x parent"}, "ASSUMED", "the v1_16 multiples (net_ceiling_multiple 1.0 + fill_headroom 0.25; gross 2 x clip with clip = parent)")
        r["hedge_deadline"] = cell("hedge IOC; 2 retries at 0.25 s, then unwind (3 attempts, 15 bps band); maker ack deadline on", "ASSUMED", "hedge_faults of v1_17")
        tiers = f["bybit_risk_tiers"]
        r["margin_bybit"] = cell({p: bybit_margin(tiers, p) for p in PARENTS} if tiers else None, "MEASURED tiers, ASSUMED leverage 10" if tiers else "NO DATA",
                                 "public /v5/market/risk-limit; initial rate raised to 1/10 (account leverage A8)")
        r["margin_binance"] = cell("v1_17 tiers: 2% / 1% to 50k, 5% / 2.5% to 500k" if sym == "SOLUSDT" else None, "ASSUMED" if sym == "SOLUSDT" else "NO DATA",
                                   "Binance leverageBracket is a signed endpoint (account keys): not read")
        r["burst_throughput"] = cell("2 new maker orders or amends per s per venue, burst 5; hedges, unwinds and cancels never refused", "APPROVED", "A1, owner 16 Nov")
        if sym == "SOLUSDT" and ladder is not None:
            r["within_cap_arrival_depth_hedge_venue"] = cell(ladder.get("hedge_depth_by_rung"), "MEASURED (29 Oct 2026, EXPLORATORY)", "from the size ladder")
            r["exit_capacity"] = cell(ladder.get("exit_by_rung"), "MEASURED (29 Oct 2026, EXPLORATORY)", "from the size ladder (pair close on)")
        else:
            why = "two-venue depth data exists only for SOLUSDT" + (" (ADAUSDT: about one hour of top-20, 15 Nov fillprobe capture: too short)" if sym == "ADAUSDT" else "")
            r["within_cap_arrival_depth_hedge_venue"] = cell(None, "NO DATA", why)
            r["exit_capacity"] = cell(None, "NO DATA", why)
        rows.append(r)
    out = {"label": "Phase 2 F02 a size envelope (owner 20 Nov). Venue filters and Bybit tiers MEASURED from the public dump of 20 Nov 2026 (current rules); SOL market cells MEASURED on 29 Oct 2026 (EXPLORATORY).",
           "symbols_listed": len(rows), "owner_to_name": "10-15 more symbols (the target is 40-45)", "rows": rows}
    (HERE / "envelope.json").write_text(json.dumps(out, indent=1, default=str))
    lines = ["# Size envelope per symbol (Phase 2 F02 a)", "", out["label"], "",
             "| Symbol | Both venues | Bybit step / min | Binance step / min | Bybit IM / MM at 30k (tier) | Binance margin | Child max | Depth / exit |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        vf = r["venue_filters"]["value"]
        by = vf.get("BYBIT") if vf else None
        bn = vf.get("BINANCE") if vf else None
        m = (r["margin_bybit"]["value"] or {}).get(30000) if r["margin_bybit"]["value"] else None
        lines.append(f"| {r['symbol']}{' (spot22)' if r['spot22'] else ''} | {'yes' if r['listed_both_perp_venues']['value'] else 'NO'} | "
                     f"{(by['step'] + ' / ' + by['min_notional'] + ' USDT') if by else 'NO DATA'} | {(bn['step'] + ' / ' + bn['min_notional'] + ' USDT') if bn else 'NO DATA'} | "
                     f"{('%.0f / %.0f (id %s)' % (m['initial_usdt'], m['maintenance_usdt'], m['tier_id'])) if m else 'NO DATA'} MEASURED | {r['margin_binance']['label']} | "
                     f"{r['child_max']['label']} | {r['within_cap_arrival_depth_hedge_venue']['label']} |")
    (HERE / "ENVELOPE.md").write_text("\n".join(lines) + "\n")
    print(len(rows), "rows")


if __name__ == "__main__":
    main()
