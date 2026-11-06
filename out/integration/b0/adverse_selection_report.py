"""Owner 7 Nov, report only (no strategy change, no freeze impact): adverse selection on the existing probe, 7 TRAIN days 03-01..03-07 00:00-02:00 UTC, v1_2 settings, through rule off.
Reads probe_adverse/<day>.json + run_<day>/maker_observations.jsonl (run_probes_adverse.sh) and the slice data; writes probe_adverse/analysis.json. EXPLORATORY: Bybit-only probe, 1 SOL bid every 15 s at the strategy's
quote depth, no hedge, no fees. Origin of every markout = the fill's report time (the moment the strategy learns of it). Markout = mid move after a BUY fill in bps: negative = the mid fell (adverse).
Bybit mid: rebuilt from the Bybit book events (hybrid delta semantics) on the exchange clock; Binance mid: bookTicker L1 on the local clock (March local stamps are constructed: exchange + 2 ms).
Day-block SE = standard deviation of the per-day means / sqrt(days with >= 5 fills)."""
import bisect, calendar, glob, gzip, json, math, statistics as st, sys, time
from collections import defaultdict
from pathlib import Path

ROOT = Path("/Users/vaibhav/Documents/Newproject")
sys.path.insert(0, str(ROOT / "crypto_MFT_arb_platform" / "src"))
from new_backtest.event_loader import BOOK, DELTA, TRADE, delta_fields, iter_venue_events, snapshot_levels, trade_fields  # noqa: E402

P = ROOT / "out" / "integration" / "b0" / "probe_adverse"
SL = ROOT / "out" / "queue_v2" / "slices_mar2024"
DAYS = [f"2024-03-0{i}" for i in range(1, 8)]
H = (0.030, 0.045, 0.060, 0.100, 0.250, 1.0, 5.0, 30.0)
HN = ("30ms", "45ms", "60ms", "100ms", "250ms", "1s", "5s", "30s")
TICK = 0.001
MIN_N = 100


def binance_l1(day):
    ts, bid, ask = [], [], []
    for fp in sorted(glob.glob(f"{SL}/binance/book_snapshots/pair=SOLUSDT/date={day}/*.gz")):
        with gzip.open(fp, "rt") as fh:
            for line in fh:
                r = json.loads(line)
                ts.append(float(r.get("local_ts") or r["timestamp"])); bid.append(float(r["best_bid"])); ask.append(float(r["best_ask"]))
    return ts, bid, ask


def levels(side_list):
    for lv in side_list:
        if isinstance(lv, dict):
            yield float(lv.get("price", 0.0)), float(lv.get("size", lv.get("volume", 0.0)))
        else:
            yield float(lv[0]), float(lv[1])


def bybit_l1_and_tape(day):
    """(times, mids) on every change of the Bybit best bid/ask, and the Bybit tape [(exchange_ts, signed volume)] (+ = buy aggressor)."""
    book = {"bid": {}, "ask": {}}
    best = [0.0, 0.0]
    T, M, tape = [], [], []
    t_end = calendar.timegm(time.strptime(day, "%Y-%m-%d")) + 7300
    for ev in iter_venue_events(ROOT / "out" / "queue_v2" / "slices_mar2024", "bybit", "SOLUSDT", day, day, include_trades=True, trade_dedupe="multiset"):
        if ev.exchange_ts > t_end:
            break
        if ev.kind == TRADE:
            price, vol, sign = trade_fields(ev.payload)
            if price > 0 and vol > 0:
                tape.append((ev.exchange_ts, vol * (1 if sign > 0 else -1)))
            continue
        if ev.kind == BOOK:
            bids, asks = snapshot_levels(ev.payload)
            book = {"bid": dict(levels(bids)), "ask": dict(levels(asks))}
            b = max(book["bid"]) if book["bid"] else 0.0
            a = min(book["ask"]) if book["ask"] else 0.0
        elif ev.kind == DELTA:
            side, price, vol, et = delta_fields(ev.payload)
            if price <= 0:
                continue
            s = "bid" if side.startswith("b") else "ask"
            cur = book[s].get(price, 0.0)
            new = 0.0 if et in ("delete", "remove") else (max(0.0, cur - vol) if et == "cancel" else max(0.0, vol))
            if new > 0:
                book[s][price] = new
            else:
                book[s].pop(price, None)
            crossed = False
            if new > 0:                                                  # a level that would cross the other side removes the stale opposite levels (no tape repair here, so a missed delete never freezes the series)
                if s == "bid":
                    stale = [q for q in book["ask"] if q <= price]
                    for q in stale:
                        del book["ask"][q]
                else:
                    stale = [q for q in book["bid"] if q >= price]
                    for q in stale:
                        del book["bid"][q]
                crossed = bool(stale)
            b, a = best                                                  # incremental best: only a better price or the removal of the best level needs work
            if crossed:
                b = max(book["bid"]) if book["bid"] else 0.0
                a = min(book["ask"]) if book["ask"] else 0.0
            elif s == "bid":
                if new > 0 and price > b:
                    b = price
                elif new <= 0 and price == b:
                    b = max(book["bid"]) if book["bid"] else 0.0
            else:
                if new > 0 and (a == 0.0 or price < a):
                    a = price
                elif new <= 0 and price == a:
                    a = min(book["ask"]) if book["ask"] else 0.0
        else:
            continue
        if (b, a) != (best[0], best[1]) and b > 0 and a > 0 and a > b:
            best[0], best[1] = b, a
            T.append(ev.exchange_ts); M.append((b + a) / 2)
    tape.sort()
    return T, M, tape


def at(ts, vals, t):
    i = bisect.bisect_right(ts, t) - 1
    return vals[i] if i >= 0 else None


def build_rows():
    rows = []
    for day in DAYS:
        d = json.load(open(P / f"{day}.json"))
        obs = [json.loads(l) for l in open(P / f"run_{day}" / "maker_observations.jsonl")]
        place = {o["signal_id"]: o for o in obs if o["obs"] == "maker_place"}
        fill_o = {(o["signal_id"], round(o["ts"], 6)): o for o in obs if o["obs"] == "maker_fill"}
        dec = d.get("decision_state", {})
        bt, bb, ba = binance_l1(day)
        bmid = [(x + y) / 2 for x, y in zip(bb, ba)]
        yT, yM, tape = bybit_l1_and_tape(day)
        tape_t = [x[0] for x in tape]
        cum = []
        c = 0.0
        for _, v in tape:
            c += abs(v); cum.append(c)
        for r in d["rows"]:
            pl = place.get(r["signal_id"]); fo = fill_o.get((r["signal_id"], round(r["ts"], 6)))
            if not pl or not fo:
                continue
            t0 = r["ts"]
            m0b, m0y = at(bt, bmid, t0), at(yT, yM, t0)
            if m0b is None or m0y is None:
                continue
            row = {"day": day, "signal_id": r["signal_id"], "price": r["price"], "qty": r["qty"], "cause": r["cause"], "empty": bool(pl["level_was_absent"]), "queue_ahead": pl["queue_ahead"]}
            tb = pl["touch"]["bybit"]
            row["dist_ticks"] = (tb["bid"] - pl["price"]) / TICK
            row["spread_ticks"] = (tb["ask"] - tb["bid"]) / TICK
            for h, n in zip(H, HN):
                mb, my = at(bt, bmid, t0 + h) if t0 + h <= bt[-1] else None, at(yT, yM, t0 + h) if t0 + h <= yT[-1] else None
                row["bin_" + n] = (mb - m0b) / m0b * 1e4 if mb is not None else None
                row["byb_" + n] = (my - m0y) / m0y * 1e4 if my is not None else None
                bbid = at(bt, bb, t0 + h) if t0 + h <= bt[-1] else None
                row["edge_" + n] = (bbid - r["price"]) / r["price"] * 1e4 if bbid is not None else None      # sell hedge on Binance at its bid h after the fill, less the Bybit price we paid, before fees
            # features before the filling print (match_ts): Bybit signed flow over the last 1 s, Binance 1 s range
            mt = fo["fill"].get("match_ts") or t0
            lo, hi = bisect.bisect_left(tape_t, mt - 1.0), bisect.bisect_left(tape_t, mt)
            sv = sum(v for _, v in tape[lo:hi]); av = sum(abs(v) for _, v in tape[lo:hi])
            row["flow_sign_1s"] = sv / av if av > 0 else 0.0
            row["flow_vol_1s"] = av
            i0, i1 = bisect.bisect_left(bt, mt - 1.0), bisect.bisect_right(bt, mt)
            seg = bmid[i0:i1] or [m0b]
            row["bin_range_1s_bps"] = (max(seg) - min(seg)) / m0b * 1e4
            ds = dec.get(str(r["signal_id"]))
            if ds:
                row.update(toxic=ds["toxic"], regime=ds["regime"], trade_sign=ds["trade_sign"], depth_collapse=ds["depth_collapse"])
            rows.append(row)
        # all placed orders' decision state (for the block_on_toxic count)
        for sid, ds in dec.items():
            rows.append({"day": day, "decision_only": True, "signal_id": int(sid), "filled": int(sid) in {r["signal_id"] for r in d["rows"]}, **{k: ds[k] for k in ("toxic", "regime", "trade_sign", "depth_collapse")}})
    return rows


def stats(rs, key):
    v = [r[key] for r in rs if r.get(key) is not None]
    if not v:
        return {"n": 0}
    byday = defaultdict(list)
    for r in rs:
        if r.get(key) is not None:
            byday[r["day"]].append(r[key])
    dm = [sum(x) / len(x) for x in byday.values() if len(x) >= 5]
    return {"n": len(v), "mean": sum(v) / len(v), "median": st.median(v), "se_fill": st.pstdev(v) / math.sqrt(len(v)) if len(v) > 1 else None,
            "se_day_block": (st.pstdev(dm) / math.sqrt(len(dm))) if len(dm) > 1 else None, "days": len(dm)}


def curve(rs, pre):
    return {n: stats(rs, pre + n) for n in HN}


def buckets(rs, key, cuts=4):
    """equal-count buckets on a numeric key, ties never split, adjacent buckets merged until each has >= MIN_N fills"""
    xs = sorted(rs, key=lambda r: r[key])
    n = len(xs)
    groups, cur = [], []
    for i, r in enumerate(xs):
        if cur and cur[-1][key] != r[key] and len(cur) >= n / cuts:
            groups.append(cur); cur = []
        cur.append(r)
    if cur:
        groups.append(cur)
    merged = True
    while merged and len(groups) > 1:
        merged = False
        for i, g in enumerate(groups):
            if len(g) < MIN_N:
                j = i - 1 if i > 0 else 1
                a, b = min(i, j), max(i, j)
                groups[a:b + 1] = [groups[a] + groups[b]]
                merged = True
                break
    return [(f"{g[0][key]:.4g} .. {g[-1][key]:.4g}", g) for g in groups]


def merged_categories(rs, f):
    cats = defaultdict(list)
    for r in rs:
        cats[f(r)].append(r)
    small = [k for k, v in cats.items() if len(v) < MIN_N]
    if small:
        cats["other (merged, n<%d)" % MIN_N] = [r for k in small for r in cats.pop(k)]
    return sorted(cats.items(), key=lambda kv: -len(kv[1]))


def summ(label, rs):
    return {"bucket": label, "fills": len(rs), "qty": sum(r["qty"] for r in rs), "binance_mid": {n: stats(rs, "bin_" + n) for n in ("100ms", "1s", "5s", "30s")},
            "bybit_mid": {n: stats(rs, "byb_" + n) for n in ("100ms", "1s", "5s", "30s")}, "hedge_edge_bps_before_fees": {n: stats(rs, "edge_" + n) for n in ("45ms", "100ms", "1s")}}


def main():
    allrows = build_rows()
    fills = [r for r in allrows if not r.get("decision_only")]
    res = {"label": "adverse selection on the probe; EXPLORATORY; v1_2 settings, March 2024 TRAIN 03-01..07 00:00-02:00 UTC, through rule off, no hedge, before fees", "fills": len(fills), "horizons": list(HN)}
    res["curve_binance_mid_bps"] = curve(fills, "bin_")
    res["curve_bybit_mid_bps"] = curve(fills, "byb_")
    res["curve_hedge_edge_bps_before_fees"] = curve(fills, "edge_")
    sp = {}
    sp["empty_vs_queue"] = [summ(k, v) for k, v in merged_categories(fills, lambda r: "empty level (queue ahead 0)" if r["empty"] else "behind a real queue")]
    def cg(r):
        c = r["cause"] or "unknown"
        return "cross_confirmed" if c.startswith("trade_confirmed_cross") else ("through" if False else ("trade" if c in ("trade", "trade_after_front") else c))
    sp["cause"] = [summ(k, v) for k, v in merged_categories(fills, cg)]
    sp["distance_from_touch_ticks"] = [summ(k, v) for k, v in buckets(fills, "dist_ticks")]
    sp["spread_at_placement_ticks"] = [summ(k, v) for k, v in buckets(fills, "spread_ticks", 3)]
    sp["bybit_flow_sign_last_1s_before_the_print"] = [summ(k, v) for k, v in buckets(fills, "flow_sign_1s")]
    sp["binance_1s_range_bps_before_the_print"] = [summ(k, v) for k, v in buckets(fills, "bin_range_1s_bps")]
    res["splits"] = sp
    # block_on_toxic
    dec = [r for r in allrows if r.get("decision_only")]
    tox = [r for r in fills if r.get("toxic")]
    ok = [r for r in fills if r.get("toxic") is False]
    res["block_on_toxic"] = {"thresholds": "|Bybit trade sign over 2 s| > 0.75 AND depth_collapse > 0.60, OR the lambda regime is TOXIC (venue_state.is_toxic); Bybit only: the probe has no Binance depth",
                             "orders_seen": len(dec), "would_block": sum(r["toxic"] for r in dec), "would_block_share": sum(r["toxic"] for r in dec) / len(dec) if dec else None,
                             "would_block_by_regime_toxic": sum(r["regime"] == "TOXIC" for r in dec), "sign_gt_0.75": sum(abs(r["trade_sign"]) > 0.75 for r in dec),
                             "depth_collapse_gt_0.60": sum(r["depth_collapse"] > 0.60 for r in dec), "regime_counts": {k: sum(r["regime"] == k for r in dec) for k in ("CALM", "ELEVATED", "TOXIC")},
                             "fills_of_blocked_orders": len(tox), "fills_of_the_rest": len(ok),
                             "fill_rate_blocked": (len(tox) / sum(r["toxic"] for r in dec)) if sum(r["toxic"] for r in dec) else None, "fill_rate_rest": len(ok) / max(1, sum(not r["toxic"] for r in dec)),
                             "markout_blocked": summ("blocked", tox) if tox else None, "markout_rest": summ("rest", ok)}
    alt = {}
    for lbl, f in (("regime_TOXIC_only", lambda r: r.get("regime") == "TOXIC"), ("regime_ELEVATED_or_TOXIC", lambda r: r.get("regime") in ("ELEVATED", "TOXIC")),
                   ("|sign|>0.5", lambda r: abs(r.get("trade_sign", 0)) > 0.5), ("depth_collapse>0.3", lambda r: r.get("depth_collapse", 0) > 0.3)):
        a = [r for r in fills if f(r)]; b = [r for r in fills if not f(r)]
        alt[lbl] = {"fills_flagged": len(a), "fills_other": len(b), "flagged": summ(lbl, a) if len(a) >= 1 else None, "other": summ("other", b)}
    res["block_on_toxic"]["what_if_other_cuts_no_new_thresholds_proposed"] = alt
    (P / "analysis.json").write_text(json.dumps(res, indent=1, default=str))
    print("fills", len(fills), "decisions", len(dec))
    for pre in ("curve_binance_mid_bps", "curve_bybit_mid_bps", "curve_hedge_edge_bps_before_fees"):
        print(pre, {n: (round(res[pre][n]["mean"], 3), res[pre][n]["n"], None if res[pre][n]["se_day_block"] is None else round(res[pre][n]["se_day_block"], 3)) for n in HN})


main()
