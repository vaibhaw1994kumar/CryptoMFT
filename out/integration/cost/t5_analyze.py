"""Step G, T5 analysis (a run wrapper; rules G_RULES.md plus the owner's additions of 14 Nov): E's split against the current all-market hedge, hedge cost, unhedged time, the risk-adjusted net at risk weight 0, 0.5 and 1 sigma, and the
markout of the rested fills reported apart (the hedge is flat once it fills, so the markout is information about adverse selection, not part of the hedge cost). A difference whose interval includes 0 is NO EFFECT (the queue model is
perp-unvalidated). usage: t5_analyze.py select NAME... (SELECT: chooses (T, lambda_u) at S = 10 SOL and risk weight 1, writes t5b/chosen.json) | t5_analyze.py report GROUP NAME... (needs chosen.json)"""
import json, sys
from pathlib import Path
import numpy as np
_dirname = "t5b"
if "--dir" in sys.argv:                                                           # another output directory under out/integration/cost (chosen.json and result_*.json go there; default t5b = the G run)
    _i = sys.argv.index("--dir"); _dirname = sys.argv[_i + 1]; del sys.argv[_i:_i + 2]
T5 = Path("/Users/vaibhav/Documents/Newproject/out/integration/cost") / _dirname
SIG = Path("/Users/vaibhav/Documents/Newproject/out/integration/cost/t5/sigma_1s.json")
mode, group, names = sys.argv[1], sys.argv[2], sys.argv[3:]
if mode == "moments":
    # FOLLOWUP_RULES.md item 2: E's split at the PROBE's simulated maker-fill moments (a sell hedge on Binance), TRAIN 03-01..07 and SELECT 03-08..11, from t5c/; the random-moment comparison from t5b/ (same days, sell hedges only).
    # usage: t5_analyze.py moments ALL
    T5C, T5B = T5.parent / names[0], T5.parent / names[1]                         # usage: t5_analyze.py moments ALL FILL_DIR RANDOM_DIR   (FILL_DIR: the run at the fill moments; RANDOM_DIR: the 1 s grid run and its chosen.json)
    SIGMA_ = float(np.mean(list(json.load(open(SIG)).values())))
    TS__, LAMS__, W = (1.0, 2.0, 5.0), (1.0, 2.6, 5.0, 10.0), (0.0, 0.5, 1.0)
    cells = [(T, lam) for T in TS__ for lam in LAMS__]
    ch = json.load(open(T5B / "chosen.json"))["chosen"]
    kc = cells.index((ch["T"], ch["lambda_u"]))
    rng_ = np.random.default_rng(1)
    TRAIN_D = [f"2024-03-0{i}" for i in range(1, 8)]
    SELECT_D = ["2024-03-08", "2024-03-09", "2024-03-10", "2024-03-11"]
    def load_days(base, days, sell_only):
        arrs, blks = [], []
        for i, d in enumerate(days):
            z = np.load(base / f"t5_{d}.npz")["a"]
            if sell_only:
                z = z[z[:, 1] > 0]
            arrs.append(z); blks.append(np.floor((z[:, 0] - z[:, 0].min()) / 600.0).astype(int) + 1000 * i)
        return np.concatenate(arrs), np.concatenate(blks)
    def cis(x, blk, reps=2000):
        ub = np.unique(blk); ix = [np.flatnonzero(blk == b) for b in ub]
        v = []
        for _ in range(reps):
            pick = np.concatenate([ix[i] for i in rng_.integers(0, len(ub), len(ub))])
            v.append(np.nanmean(x[pick]) if np.isfinite(x[pick]).any() else np.nan)
        return [float(np.nanpercentile(v, 2.5)), float(np.nanpercentile(v, 97.5))]
    NC_ = 5
    def stats(A, blk, k, S, extra):
        m = A[:, 2] == S
        a, b = A[m], blk[m]
        cM, cE = a[:, 3], a[:, 4 + NC_ * k]
        tM, tE = np.full(len(a), 0.045), a[:, 5 + NC_ * k]
        share = a[:, 6 + NC_ * k]
        mk1, mk5 = a[:, 7 + NC_ * k], a[:, 8 + NC_ * k]
        out = {"n": int(m.sum()), "limit_share": float(share.mean()), "cost_M": [float(cM.mean()), *cis(cM, b)], "cost_E": [float(cE.mean()), *cis(cE, b)], "E_minus_M_cost": [float((cE - cM).mean()), *cis(cE - cM, b)],
               "unhedged_time_s": {"M": 0.045, "E": [float(tE.mean()), *cis(tE, b)]}, "net": {}, "rested_markout_bps": {"plus_1s": [float(np.nanmean(mk1)), *cis(mk1, b)], "plus_5s": [float(np.nanmean(mk5)), *cis(mk5, b)]}}
        if extra:
            ff = a[:, 4 + NC_ * len(cells) + k]
            Lq = share * S
            out["fill_rate_of_rested_part_volume_weighted"] = float(np.nansum(ff * Lq) / Lq[np.isfinite(ff)].sum()) if np.isfinite(ff).any() else None
            out["share_of_moments_with_a_rested_part"] = float((share > 0).mean())
            out["share_of_moments_with_a_rested_fill"] = float(np.isfinite(mk1).mean())
        for w in W:
            nM, nE = -cM - w * np.sqrt(tM) * SIGMA_, -cE - w * np.sqrt(tE) * SIGMA_
            d = nE - nM; lo, hi = cis(d, b)
            out["net"][str(w)] = {"M": float(nM.mean()), "E": float(nE.mean()), "E_minus_M": [float(d.mean()), lo, hi], "verdict": "NO EFFECT (interval includes 0)" if lo <= 0 <= hi else ("E better" if d.mean() > 0 else "E worse")}
        return out
    res = {"label": "EXPLORATORY PROXY: hypothetical SELL hedges on Binance at the probe's SIMULATED Bybit maker-fill moments (queue model perp-unvalidated); touch assumed to fit the size; HOLDOUT not run (no probe fills there)",
           "chosen_cell": ch, "sigma_1s_bps": SIGMA_, "groups": {}}
    TRs, TRb = load_days(T5C, TRAIN_D, False); SEs, SEb = load_days(T5C, SELECT_D, False)
    ALLs, ALLb = np.concatenate([TRs, SEs]), np.concatenate([TRb, SEb + 100000])
    RTr, RTb = load_days(T5B, TRAIN_D, True); RSe, RSb = load_days(T5B, SELECT_D, True)
    RAll, RAb = np.concatenate([RTr, RSe]), np.concatenate([RTb, RSb + 100000])
    for gname, (A, B, RA, RB) in {"TRAIN": (TRs, TRb, RTr, RTb), "SELECT": (SEs, SEb, RSe, RSb), "ALL": (ALLs, ALLb, RAll, RAb)}.items():
        g = {"moments": int((A[:, 2] == 1.0).sum()), "by_size": {}}
        for S in (1.0, 10.0, 100.0):
            g["by_size"][str(S)] = {"at_fill_moments": stats(A, B, kc, S, True), "at_random_moments_sell_hedges_same_days": stats(RA, RB, kc, S, False)}
        res["groups"][gname] = g
    # the 12-cell grid at S = 10 on the fill moments: would TRAIN choose the same cell as G did on random moments?
    grid = {}
    for gname, (A, B) in {"TRAIN": (TRs, TRb), "SELECT": (SEs, SEb)}.items():
        m = A[:, 2] == 10.0
        grid[gname] = [{"T": T, "lambda_u": lam, "net_E_w1": float((-A[m, 4 + NC_ * k] - np.sqrt(A[m, 5 + NC_ * k]) * SIGMA_).mean()), "cost_E": float(A[m, 4 + NC_ * k].mean()), "limit_share": float(A[m, 6 + NC_ * k].mean()),
                        "fill_rate": float(np.nansum(A[m, 4 + NC_ * len(cells) + k] * A[m, 6 + NC_ * k] * 10.0) / (A[m, 6 + NC_ * k] * 10.0).sum()) if (A[m, 6 + NC_ * k] > 0).any() else None} for k, (T, lam) in enumerate(cells)]
    res["grid_at_S10_on_fill_moments"] = grid
    res["best_cell_TRAIN_fill_moments"] = max(grid["TRAIN"], key=lambda r: r["net_E_w1"])
    # adverse-move terciles (TRAIN terciles of the 5 s move before the decision, positive = against the hedge)
    pre5 = lambda A: A[:, 4 + NC_ * len(cells) + len(cells) + 1]
    edges = np.percentile(pre5(TRs[TRs[:, 2] == 1.0]), [100 / 3, 200 / 3])
    terc = {"edges_bps": [float(x) for x in edges], "mean_adverse_5s_move_at_fill_moments_bps": float(pre5(ALLs[ALLs[:, 2] == 1.0]).mean()), "by_tercile": {}}
    for S in (1.0, 10.0, 100.0):
        mS = ALLs[:, 2] == S
        a, b = ALLs[mS], ALLb[mS]
        p = pre5(a)
        for name, mm in (("least adverse", p <= edges[0]), ("middle", (p > edges[0]) & (p <= edges[1])), ("most adverse", p > edges[1])):
            cE, cM = a[mm, 4 + NC_ * kc], a[mm, 3]
            tE = a[mm, 5 + NC_ * kc]
            d = (-cE - W[2] * np.sqrt(tE) * SIGMA_) - (-cM - W[2] * np.sqrt(0.045) * SIGMA_)
            terc["by_tercile"][f"S={S:g} {name}"] = {"n": int(mm.sum()), "E_minus_M_cost": [float((cE - cM).mean()), *cis(cE - cM, b[mm])], "net_w1_E_minus_M": [float(d.mean()), *cis(d, b[mm])], "limit_share": float(a[mm, 6 + NC_ * kc].mean())}
    res["adverse_move_terciles"] = terc
    (T5C / "result_moments.json").write_text(json.dumps(res, indent=1))
    (T5C / "report_moments_source.txt").write_text(f"fill moments: {T5C}  random moments and chosen cell: {T5B}")
    f3 = lambda x: "%.3f [%.3f, %.3f]" % tuple(x)
    for gname, g in res["groups"].items():
        for S, v in g["by_size"].items():
            fm, rm = v["at_fill_moments"], v["at_random_moments_sell_hedges_same_days"]
            print(f"{gname} S={S} moments {fm['n']}: share rested {fm['limit_share']:.3f} fill-rate {fm.get('fill_rate_of_rested_part_volume_weighted')} | E-M cost fill-moments {f3(fm['E_minus_M_cost'])} random {f3(rm['E_minus_M_cost'])}")
            for w, r in fm["net"].items():
                print(f"      w={w}: net E-M fill-moments {f3(r['E_minus_M'])} {r['verdict']} | random {f3(rm['net'][w]['E_minus_M'])}")
    print("best cell on TRAIN fill moments", res["best_cell_TRAIN_fill_moments"])
    print(json.dumps(terc, indent=0))
    sys.exit(0)


if mode == "large":
    # FOLLOWUP_RULES.md 1c: T5 at 100 / 500 SOL on the capture (Binance, real depth, PRICE-AWARE fills); the FIRST half chooses, the SECOND half is the answer. usage: t5_analyze.py large ALL
    Z = np.load(T5.parent / "t5c" / "large_rows.npz")
    A, split = Z["a"], float(Z["split_ts"])
    prep = json.load(open(T5.parent / "t5c" / "large_prep.json"))
    SIG_ = prep["sigma_1s_bps"]
    TS__, LAMS__, W = (1.0, 2.0, 5.0), (1.0, 2.6, 5.0, 10.0), (0.0, 0.5, 1.0)
    cells = [(T, lam) for T in TS__ for lam in LAMS__]
    sl = [tuple(x) for x in Z["sl_cfgs"].tolist()]
    OWN = ("refill", "off")
    rng_ = np.random.default_rng(1)
    t_min = A[:, 0].min()
    blk = np.floor((A[:, 0] - t_min) / 600.0).astype(int)
    first = A[:, 0] < split
    def colE(k): return 6 + 5 * k
    def colS(j, o): return 6 + 5 * len(cells) + (j * 2 + o) * 3
    def net(c, vw, w): return -c - w * np.sqrt(vw) * SIG_
    def cis(x, b, reps=2000):
        ub = np.unique(b); ix = [np.flatnonzero(b == u) for u in ub]
        v = []
        for _ in range(reps):
            pick = np.concatenate([ix[i] for i in rng_.integers(0, len(ub), len(ub))])
            v.append(np.nanmean(x[pick]) if np.isfinite(x[pick]).any() else np.nan)
        return [float(np.nanpercentile(v, 2.5)), float(np.nanpercentile(v, 97.5))]
    def arm_rows(m, S):
        """per arm: cost, vw time, risk time columns restricted to rows m with size S (arrays aligned)."""
        a = A[m & (A[:, 2] == S)]
        arms = {"M": (a[:, 3], a[:, 4], a[:, 5])}
        for k, (T, lam) in enumerate(cells):
            arms[f"E T={T:g} lam={lam:g}"] = (a[:, colE(k)], a[:, colE(k) + 1], a[:, colE(k) + 2])
        for j, (n, h, kk) in enumerate(sl):
            for o, on in enumerate(OWN):
                arms[f"TS {on} N={int(n)} H={h:g} kH={kk:g}"] = (a[:, colS(j, o)], a[:, colS(j, o) + 1], a[:, colS(j, o) + 2])
        return a, arms
    res = {"label": "EXPLORATORY PROXY: hypothetical hedges on the 2026-10-29 capture (Binance real depth, price-aware fills; queue model perp-unvalidated); chronological halves of ONE day: the first half chooses, the second half is the answer", "sigma_1s_bps": SIG_, "split_ts": split,
           "decisions": {"first_half": int((first & (A[:, 2] == 100.0)).sum()), "second_half": int((~first & (A[:, 2] == 100.0)).sum())}, "chosen": {}, "second_half": {}, "grid": {"first_half": {}, "second_half": {}}}
    # choice on the first half at S = 100, highest net (weight 1, vw time); E over its 12 cells, TS refill and TS off over their 12 configs each
    _, arms1 = arm_rows(first, 100.0)
    def best(prefix):
        c = {k: float(np.nanmean(net(*[arms1[k][0], arms1[k][1], None][:2], 1.0))) for k in arms1 if k.startswith(prefix)}
        return max(c, key=c.get), c
    chosen = {}
    for name, prefix in (("E", "E "), ("TS_refill", "TS refill"), ("TS_off", "TS off")):
        chosen[name], res["grid"]["first_half"][name] = best(prefix)
    res["chosen"] = chosen
    for S in (100.0, 500.0):
        a, arms = arm_rows(~first, S)
        b = blk[~first & (A[:, 2] == S)]
        row = {"n": int(len(a)), "arms": {}}
        Mc, Mv, Mr = arms["M"]
        for label, key in (("M", "M"), ("E_chosen", chosen["E"]), ("TS_refill_chosen", chosen["TS_refill"]), ("TS_off_chosen_bound", chosen["TS_off"])):
            c, vw, rk = arms[key]
            ok = np.isfinite(c) & np.isfinite(Mc)
            d = {"arm": key, "n_finite": int(ok.sum()), "cost_bps": [float(np.nanmean(c)), *cis(c, b)], "vw_time_s": float(np.nanmean(vw)), "risk_time_sqrt_s": float(np.nanmean(rk)),
                 "cost_minus_M": [float(np.nanmean(c - Mc)), *cis(c - Mc, b)], "net_by_weight_vw_time": {}, "net_by_weight_inventory_risk": {}}
            if key.startswith("E "):
                kk = cells.index(tuple(float(x.split("=")[1]) for x in key.split()[1:3]))
                d["limit_share"] = float(np.nanmean(a[:, colE(kk) + 3])); d["fill_rate_of_rested"] = float(np.nanmean(a[:, colE(kk) + 4]))
            for w in W:
                nM, nA, nB = net(Mc, Mv, w), net(c, vw, w), -c - w * rk * SIG_
                nMB = -Mc - w * Mr * SIG_
                dA, dB = nA - nM, nB - nMB
                la, ha = cis(dA, b); lb, hb = cis(dB, b)
                d["net_by_weight_vw_time"][str(w)] = {"net": float(np.nanmean(nA)), "minus_M": [float(np.nanmean(dA)), la, ha], "verdict": "NO EFFECT (interval includes 0)" if la <= 0 <= ha else ("better than M" if np.nanmean(dA) > 0 else "worse than M")}
                d["net_by_weight_inventory_risk"][str(w)] = {"net": float(np.nanmean(nB)), "minus_M": [float(np.nanmean(dB)), lb, hb], "verdict": "NO EFFECT (interval includes 0)" if lb <= 0 <= hb else ("better than M" if np.nanmean(dB) > 0 else "worse than M")}
            row["arms"][label] = d
        res["second_half"][str(S)] = row
        res["grid"]["second_half"][str(S)] = {k: {"cost": float(np.nanmean(v[0])), "net_w1": float(np.nanmean(net(v[0], v[1], 1.0))), "vw_time": float(np.nanmean(v[1]))} for k, v in arms.items()}
    (T5.parent / "t5c" / "result_large.json").write_text(json.dumps(res, indent=1))
    f3 = lambda x: "%.3f [%.3f, %.3f]" % tuple(x)
    print("chosen on the first half (S=100):", chosen)
    for S, row in res["second_half"].items():
        print(f"== S={S}: second half, {row['n']} decisions")
        for lab, d in row["arms"].items():
            print(f"  {lab:20s} {d['arm']:28s} cost {f3(d['cost_bps'])} vw-time {d['vw_time_s']:.2f}s risk-time {d['risk_time_sqrt_s']:.2f} | cost-M {f3(d['cost_minus_M'])}" + (f" | share {d['limit_share']:.2f} fill {d['fill_rate_of_rested']:.2f}" if "limit_share" in d else ""))
            for w, r in d["net_by_weight_vw_time"].items():
                print(f"       w={w}: net-M (vw time) {f3(r['minus_M'])} {r['verdict']} | (inventory risk) {f3(d['net_by_weight_inventory_risk'][w]['minus_M'])} {d['net_by_weight_inventory_risk'][w]['verdict']}")
    sys.exit(0)


if mode == "lamsrc":
    # LARGE_GAPS_RULES.md steps 3 and 5 on the March runs: E's cells T x lambda_u source (sigma_1s sqrt(T), CVaR(5%) random, CVaR(5%) random + sharp; t5_split.py run --lam-file) at the hedge moments of a run dir, against all-market M.
    # usage: t5_analyze.py lamsrc ALL RUN_DIR [--fill]   (RUN_DIR holds t5_DAY.npz with 9 cells; --fill: the run used --times, the probe's simulated maker-fill moments, sell hedges only)
    FILL = "--fill" in sys.argv
    RUN = T5.parent / names[0]
    lamf = json.load(open(T5.parent / "t5w/lambdas.json"))["lambdas"]["TRAIN"]
    SIG1 = lamf["sigma_1s_bps"]
    hh = np.array(sorted(float(h) for h in lamf["cvar_bc"])); cv = np.array([lamf["cvar_bc"][str(h)] for h in hh])
    cvar_of = lambda t: np.where(t < hh[0], cv[0] * np.sqrt(np.maximum(t, 0) / hh[0]), np.interp(t, hh, cv))
    TS3, SRC3, W = (1.0, 2.0, 5.0), ("sigma", "cvar_b", "cvar_bc"), (0.0, 0.5, 1.0)
    cells = [(T, src) for T in TS3 for src in SRC3]
    rng3 = np.random.default_rng(1)
    def load_days(days):
        arrs, blks = [], []
        for i, d in enumerate(days):
            z = np.load(RUN / f"t5_{d}.npz")["a"]
            if FILL:
                z = z[z[:, 1] > 0]
            arrs.append(z); blks.append(np.floor((z[:, 0] - z[:, 0].min()) / 600.0).astype(int) + 1000 * i)
        return np.concatenate(arrs), np.concatenate(blks)
    def boot(x, blk, reps=2000):
        ub, inv = np.unique(blk, return_inverse=True)
        sm, ct = np.bincount(inv, weights=np.where(np.isfinite(x), x, 0.0), minlength=len(ub)), np.bincount(inv, weights=np.isfinite(x).astype(float), minlength=len(ub))
        pick = rng3.integers(0, len(ub), (reps, len(ub)))
        r = sm[pick].sum(1) / np.maximum(ct[pick].sum(1), 1)
        return [float(sm.sum() / max(ct.sum(), 1)), float(np.percentile(r, 2.5)), float(np.percentile(r, 97.5))]
    groups = {"SELECT": ["2024-03-08", "2024-03-09", "2024-03-10", "2024-03-11"] + ([] if FILL else ["2024-03-12"]), "TRAIN": [f"2024-03-0{i}" for i in range(1, 8)]}
    res = {"fill_moments": FILL, "sigma_1s_bps": SIG1, "label": "EXPLORATORY PROXY; lambda_u sources from the TRAIN Binance moments (lambdas.json TRAIN); the answer is SELECT only (HOLDOUT unread), so the cell choice is selected on the data it is shown on", "groups": {}}
    for gname, days in groups.items():
        A, blk = load_days(days)
        out = {}
        for S in (1.0, 10.0, 100.0):
            m = A[:, 2] == S
            cm = A[m, 3]
            tbl = {}
            for k, (T, src) in enumerate(cells):
                c0 = 4 + 5 * k
                cost, tm = A[m, c0], A[m, c0 + 1]
                row = {"cost_minus_M": boot(cost - cm, blk[m]), "rested_share": float(np.nanmean(A[m, c0 + 2])), "unhedged_s": float(np.nanmean(tm))}
                if FILL:
                    row["fill_rate_rested"] = float(np.nanmean(A[m, 4 + 5 * len(cells) + k]))
                for w in W:
                    row[f"net_sigma_w{w:g}_minus_M"] = boot((-cost - w * SIG1 * np.sqrt(tm)) - (-cm - w * SIG1 * np.sqrt(0.045)), blk[m])
                    row[f"net_cvar_w{w:g}_minus_M"] = boot((-cost - w * cvar_of(tm)) - (-cm - w * cvar_of(np.full(len(tm), 0.045))), blk[m])
                tbl[f"T{T:g}_{src}"] = row
            out[f"S={S:g}"] = {"n": int(m.sum()), "M_cost": float(np.nanmean(cm)), "cells": tbl}
        res["groups"][gname] = out
    json.dump(res, open(RUN / ("result_lamsrc_fill.json" if FILL else "result_lamsrc.json"), "w"), indent=1)
    lines = []
    for gname, out in res["groups"].items():
        for sk, v in out.items():
            lines.append(f"== {gname} {sk} n={v['n']} M cost {v['M_cost']:.3f}")
            for ck, r in v["cells"].items():
                lines.append(f"  {ck:14s} cost-M {r['cost_minus_M'][0]:+.3f} [{r['cost_minus_M'][1]:+.3f},{r['cost_minus_M'][2]:+.3f}] rested {r['rested_share']:.2f}" + (f" fill {r['fill_rate_rested']:.2f}" if FILL else "") + "  net(sigma)-M w0 %+.3f w.5 %+.3f w1 %+.3f  net(cvar)-M w.5 %+.3f w1 %+.3f" % (r["net_sigma_w0_minus_M"][0], r["net_sigma_w0.5_minus_M"][0], r["net_sigma_w1_minus_M"][0], r["net_cvar_w0.5_minus_M"][0], r["net_cvar_w1_minus_M"][0]))
    (RUN / ("report_lamsrc_fill.txt" if FILL else "report_lamsrc.txt")).write_text("\n".join(lines))
    print("\n".join(lines)[:5000])
    sys.exit(0)

if mode == "large2":
    # LARGE_GAPS_RULES.md steps 4c / 5: the arms of t5_split.py large2 (capture; second half = the answer; the first half chooses E's cell at S = 100 by the highest net at weight 1). usage: t5_analyze.py large2 ALL --dir t5l2
    z = np.load(T5 / "large2_rows.npz")
    base, A, names, split_ts = z["base"], z["arms"].astype(np.float64), [str(x) for x in z["names"]], float(z["split_ts"])
    lamf = json.load(open(T5.parent / "t5w/lambdas.json"))["lambdas"]["CAPFIRST"]
    SIGMA1 = lamf["sigma_1s_bps"]
    hh = np.array(sorted(float(h) for h in lamf["cvar_bc"])); cv = np.array([lamf["cvar_bc"][str(h)] for h in hh])
    def cvar_of(t):                                                                  # CVaR(5%) of the adverse move over t unhedged seconds, interpolated on the horizon grid; below the first horizon scaled by sqrt(t / h0) (ASSUMED)
        t = np.asarray(t, float)
        return np.where(t < hh[0], cv[0] * np.sqrt(np.maximum(t, 0) / hh[0]), np.interp(t, hh, cv))
    W = (0.0, 0.5, 1.0)
    ix = {k: i for i, k in enumerate(names)}
    cost, vw, risk, drift = A[:, :, 0], A[:, :, 1], A[:, :, 2], A[:, :, 3]
    net_s = {w: -cost - w * SIGMA1 * risk for w in W}                                # -cost - w sigma_1s sqrt(unhedged time)
    net_c = {w: -cost - w * cvar_of(vw) for w in W}                                  # -cost - w CVaR(5%)(volume-weighted unhedged time)
    first = base[:, 0] < split_ts
    rng2 = np.random.default_rng(1)
    S_ = base[:, 2]
    # choose E's cell on the first half (S = 100, price-aware, overlay off), highest mean net at weight 1 (sigma version)
    cells = sorted({k.rsplit("_pa_off", 1)[0] for k in names if k.startswith("E_") and k.endswith("_pa_off")})
    best, bestv = None, -1e9
    for c in cells:
        j = ix[c + "_pa_off"]
        m = first & np.isfinite(cost[:, j])
        if m.sum() > 50:
            v = np.nanmean(net_s[1.0][m, j] - (net_s[1.0][m, ix["M"]] if True else 0))
            if v > bestv:
                best, bestv = c, v
    print("chosen E cell (first half):", best, round(bestv, 4))
    def boot_mean(vec, blocks, reps=2000):
        ub, inv = np.unique(blocks, return_inverse=True)
        okm = np.isfinite(vec)
        sm = np.bincount(inv, weights=np.where(okm, vec, 0.0), minlength=len(ub)); ct = np.bincount(inv, weights=okm.astype(float), minlength=len(ub))
        pick = rng2.integers(0, len(ub), (reps, len(ub)))
        num, den = sm[pick].sum(1), ct[pick].sum(1)
        r = num / np.maximum(den, 1)
        return [float(sm.sum() / max(ct.sum(), 1)), float(np.percentile(r, 2.5)), float(np.percentile(r, 97.5))]
    def tail(x):
        x = x[np.isfinite(x)]
        if len(x) < 20:
            return [None, None]
        k = max(1, int(round(0.05 * len(x))))
        return [float(np.percentile(x, 99)), float(np.sort(x)[-k:].mean())]
    arms_E = [best + "_pa_off", best + "_pa_default", best + "_pa_upper", best + "_pess_off"] if best else []
    sel_names = ["M"] + arms_E + [k for k in names if k[0] in "PL"]
    res = {"chosen_E_cell": best, "sigma_1s_bps": SIGMA1, "label": "EXPLORATORY PROXY, capture second half; own consumption refill tau 3.5 s ASSUMED; overlay MODELLED (default = isolated-run fit, upper = all-runs bound; whole ticks); rule 1 survival %.3f x 0.8 / 1 / 1.2" % 0.95, "sizes": {}}
    for tag, mfilter in (("all", np.ones(len(base), bool)), ("after_adverse_move_ge_3bps", base[:, 3] >= 3.0)):
        for S in (100.0, 500.0, 1000.0):
            m = (~first) & (S_ == S) & mfilter
            if m.sum() < 30:
                continue
            blk = np.floor((base[m, 0] - split_ts) / 600.0).astype(int)
            jM = ix["M"]
            tbl = {}
            for k in sel_names:
                j = ix[k]
                okrow = np.isfinite(cost[m, j]) & np.isfinite(cost[m, jM])
                r = {"n": int(okrow.sum()), "cost": boot_mean(np.where(okrow, cost[m, j], np.nan), blk), "unhedged_s": float(np.nanmean(vw[m, j][okrow])) if okrow.any() else None,
                     "drift_mean": float(np.nanmean(drift[m, j][okrow])) if okrow.any() else None, "drift_p99_cvar5": tail(drift[m, j][okrow])}
                if k != "M":
                    r["cost_minus_M"] = boot_mean(np.where(okrow, cost[m, j] - cost[m, jM], np.nan), blk)
                    for w in W:
                        r[f"net_sigma_w{w:g}_minus_M"] = boot_mean(np.where(okrow, net_s[w][m, j] - net_s[w][m, jM], np.nan), blk)
                        r[f"net_cvar_w{w:g}_minus_M"] = boot_mean(np.where(okrow, net_c[w][m, j] - net_c[w][m, jM], np.nan), blk)
                    if k.startswith("E_"):
                        r["fill_rate_rested"] = float(np.nanmean(A[m, j, 5][okrow])); r["rested_share"] = float(np.nanmean(A[m, j, 4][okrow]))
                    if k[0] in "PL":
                        r["carry_left_mean"] = float(np.nanmean(A[m, j, 4][okrow])) if okrow.any() else None
                tbl[k] = r
            res["sizes"][f"{tag}|{int(S)}"] = tbl
    json.dump(res, open(T5 / "result_large2.json", "w"), indent=1)
    # a short text report
    out = []
    for key, tbl in res["sizes"].items():
        out.append(f"== {key}: n(M) = {tbl['M']['n']}, M cost {tbl['M']['cost'][0]:.3f} bps [{tbl['M']['cost'][1]:.3f}, {tbl['M']['cost'][2]:.3f}]; M unhedged {tbl['M']['unhedged_s']:.3f} s; M drift p99 / CVaR5 {tbl['M']['drift_p99_cvar5']}")
        rows = [(k, v) for k, v in tbl.items() if k != "M"]
        for k, v in rows:
            if k.startswith("E_") or "_refill_off" in k or "_refill_default" in k or "_off_off" in k and k.startswith("P_n10_H30_k0"):
                nm = lambda w: v[f"net_sigma_w{w:g}_minus_M"][0]
                out.append(f"  {k:36s} cost-M {v['cost_minus_M'][0]:+.3f} [{v['cost_minus_M'][1]:+.3f},{v['cost_minus_M'][2]:+.3f}] unh {v['unhedged_s']:.2f}s  net-M(sigma) w0 {nm(0):+.3f} w.5 {nm(0.5):+.3f} w1 {nm(1):+.3f}  net-M(cvar) w.5 {v['net_cvar_w0.5_minus_M'][0]:+.3f} w1 {v['net_cvar_w1_minus_M'][0]:+.3f}  drift p99/cvar5 {v['drift_p99_cvar5']}" + (f" fill {v.get('fill_rate_rested', float('nan')):.2f}" if k.startswith('E_') else ""))
    (T5 / "report_large2.txt").write_text("\n".join(out))
    print("\n".join(out)[:6000])
    sys.exit(0)

if mode == "select":
    names = [group] + names; group = "SELECT"
SIGMA = float(np.mean(list(json.load(open(SIG)).values())))            # TRAIN mean of the 1 s sigma, bps per sqrt(s)
TS_, LAMS, WEIGHTS = (1.0, 2.0, 5.0), (1.0, 2.6, 5.0, 10.0), (0.0, 0.5, 1.0)
REPS, BLOCK_S, NC = 2000, 600.0, 5
rng = np.random.default_rng(1)
arrs, blocks = [], []
for i, n in enumerate(names):
    z = np.load(T5 / f"t5_{n}.npz")
    arrs.append(z["a"]); blocks.append(np.floor((z["a"][:, 0] - z["a"][:, 0].min()) / BLOCK_S).astype(int) + 1000 * i)
A, Bk = np.concatenate(arrs), np.concatenate(blocks)
size = A[:, 2]
cfgs = [(T, lam) for T in TS_ for lam in LAMS]
col = lambda k: 4 + NC * k
costM, timeM = A[:, 3], np.full(len(A), 0.045)
net = lambda c, t, w: -c - w * np.sqrt(t) * SIGMA


def intervals(mask):
    bk = Bk[mask]; ub = np.unique(bk); ix = [np.flatnonzero(bk == b) for b in ub]
    boots = [np.concatenate([ix[i] for i in rng.integers(0, len(ub), len(ub))]) for _ in range(REPS)]
    def ci(x):
        v = np.array([np.nanmean(x[p]) if np.isfinite(x[p]).any() else np.nan for p in boots])
        return [float(np.nanpercentile(v, 2.5)), float(np.nanpercentile(v, 97.5))]
    return ci


if mode == "select":
    rows, m10 = [], size == 10.0
    for k, (T, lam) in enumerate(cfgs):
        c, t, sh = A[m10, col(k)], A[m10, col(k) + 1], A[m10, col(k) + 2]
        rows.append({"T": T, "lambda_u": lam, "net_E_w1_bps": float(net(c, t, 1.0).mean()), "net_E_w0_bps": float(net(c, t, 0.0).mean()), "cost_E_bps": float(c.mean()), "unhedged_time_s": float(t.mean()), "limit_share": float(sh.mean())})
    base = {"net_M_w1_bps": float(net(costM[m10], timeM[m10], 1.0).mean()), "cost_M_bps": float(costM[m10].mean())}
    best = max(rows, key=lambda r: r["net_E_w1_bps"])
    json.dump({"chosen": best, "criterion": "highest risk-adjusted net (weight 1 sigma) at S = 10 SOL on SELECT", "sigma_1s_bps": SIGMA, "grid_at_S10": rows, "all_market_at_S10": base, "windows": names}, open(T5 / "chosen.json", "w"), indent=1)
    for r in sorted(rows, key=lambda r: -r["net_E_w1_bps"]): print({k: round(v, 3) for k, v in r.items()})
    print("all market", {k: round(v, 3) for k, v in base.items()}, "CHOSEN T", best["T"], "lambda_u", best["lambda_u"])
elif mode == "report":
    ch = json.load(open(T5 / "chosen.json"))["chosen"]
    k = cfgs.index((ch["T"], ch["lambda_u"]))
    out = {"group": group, "windows": names, "chosen": ch, "sigma_1s_bps": SIGMA, "risk_weights": WEIGHTS, "label": "EXPLORATORY PROXY: hypothetical hedges on Binance March prints; the touch is assumed to fit the market size; the queue model is perp-unvalidated: a difference whose interval includes 0 is no effect", "by_size": {}}
    for S in (1.0, 10.0, 100.0):
        m = size == S
        cM, tM, cE, tE = costM[m], timeM[m], A[m, col(k)], A[m, col(k) + 1]
        mk1, mk5, share = A[m, col(k) + 3], A[m, col(k) + 4], A[m, col(k) + 2]
        ci = intervals(m)
        row = {"n": int(m.sum()), "limit_share_E": float(share.mean()), "share_of_samples_with_a_rested_fill": float(np.isfinite(mk1).mean()),
               "hedge_cost_bps": {"M": [float(cM.mean()), *ci(cM)], "E": [float(cE.mean()), *ci(cE)], "E_minus_M": [float((cE - cM).mean()), *ci(cE - cM)]},
               "unhedged_time_s": {"M": float(tM.mean()), "E": [float(tE.mean()), *ci(tE)]}, "net_by_risk_weight": {}}
        for w in WEIGHTS:
            nM, nE = net(cM, tM, w), net(cE, tE, w)
            d = nE - nM
            lo, hi = ci(d)
            row["net_by_risk_weight"][str(w)] = {"M": [float(nM.mean()), *ci(nM)], "E": [float(nE.mean()), *ci(nE)], "E_minus_M": [float(d.mean()), lo, hi], "verdict": "NO EFFECT (interval includes 0)" if lo <= 0 <= hi else ("E better" if d.mean() > 0 else "E worse")}
        row["rested_fill_markout_bps"] = {"plus_1s": [float(np.nanmean(mk1)), *ci(mk1)], "plus_5s": [float(np.nanmean(mk5)), *ci(mk5)],
                                          "note": "side-signed mid move after the fill against the fill price, positive = favours the resting order; the hedge is flat after the fill, so this is NOT in the hedge cost"}
        out["by_size"][str(S)] = row
    (T5 / f"result_{group}.json").write_text(json.dumps(out, indent=1))
    for S, v in out["by_size"].items():
        print(S, "n", v["n"], "limit share %.2f" % v["limit_share_E"], "| cost M %.3f E %.3f diff %.3f %s" % (v["hedge_cost_bps"]["M"][0], v["hedge_cost_bps"]["E"][0], v["hedge_cost_bps"]["E_minus_M"][0], [round(x, 3) for x in v["hedge_cost_bps"]["E_minus_M"][1:]]), "| time E %.2fs" % v["unhedged_time_s"]["E"][0])
        for w, r in v["net_by_risk_weight"].items():
            print("    w=%s net diff %.3f %s %s" % (w, r["E_minus_M"][0], [round(x, 3) for x in r["E_minus_M"][1:]], r["verdict"]))
        print("    markout of rested fills: +1s %.3f %s, +5s %.3f %s" % (v["rested_fill_markout_bps"]["plus_1s"][0], [round(x, 3) for x in v["rested_fill_markout_bps"]["plus_1s"][1:]], v["rested_fill_markout_bps"]["plus_5s"][0], [round(x, 3) for x in v["rested_fill_markout_bps"]["plus_5s"][1:]]))
