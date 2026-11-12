"""Offline silence analysis of D7d run 2 (a run wrapper, read-only on the recording). Usage: cd <decompressed d7d-run2 folder> && python3 silence_analysis_run2.py. Wire frames only: receive_ts_ns against the exchange timestamp inside each book frame (Bybit ts, Binance E)."""
import json,glob,bisect,statistics,sys
rows=[]
for f in sorted(glob.glob("part-00000*/wire.jsonl")):
    for l in open(f):
        d=json.loads(l); fd=d["frame_data"]; ets=None
        if d["channel"] in ("orderbook.50.SOLUSDT","depth@100ms"):
            try:
                j=json.loads(fd); ets=j.get("ts") if d["venue"]=="BYBIT" else j.get("E")
            except Exception: pass
        rows.append((d["receive_ts_ns"],d["venue"],d["channel"],ets))
rows.sort()
t0=rows[0][0]
BOOK={"BYBIT":"orderbook.50.SOLUSDT","BINANCE":"depth@100ms"}
by={v:[r for r in rows if r[1]==v and r[2]==BOOK[v]] for v in BOOK}
other={v:[r[0] for r in rows if r[1]==v and r[2]!=BOOK[v]] for v in BOOK}
allts=[r[0] for r in rows]
print("recording span s",(rows[-1][0]-t0)/1e9,"frames",len(rows))
gaps=[]
for v,rs in by.items():
    for a,b in zip(rs,rs[1:]):
        g=(b[0]-a[0])/1e6
        if g>=1000: gaps.append((v,(a[0]-t0)/1e9,g,a,b))
print("book-frame gaps >= 1 s:",len(gaps))
for v,at,g,a,b in sorted(gaps,key=lambda x:x[1]):
    # frames of the same venue (other channels) and of the other venue during the gap
    same=sum(1 for t in other[v] if a[0]<t<b[0])
    ov="BINANCE" if v=="BYBIT" else "BYBIT"
    oth=sum(1 for r in by[ov] if a[0]<r[0]<b[0])
    othc=sum(1 for t in other[ov] if a[0]<t<b[0])
    lag_after=(b[0]/1e6-b[3]) if b[3] else None
    lag_before=(a[0]/1e6-a[3]) if a[3] else None
    exch_gap=(b[3]-a[3]) if (a[3] and b[3]) else None
    print(f"{v:7s} t={at:8.1f}s gap={g:7.0f}ms exch_ts_gap={exch_gap}ms lag_before={lag_before and round(lag_before)}ms lag_first_after={lag_after and round(lag_after)}ms | same-venue other-channel frames in gap={same} | other venue book frames={oth}, other frames={othc}")
