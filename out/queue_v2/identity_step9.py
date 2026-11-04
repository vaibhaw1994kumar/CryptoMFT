"""Switch-off identity (owner, 2026-11-04): the frozen v1 profile with step 8 on and every step-9 switch OFF must give byte-identical trades / fill events / bucket stats / model settings on
snap_qv2h and on snap_qv2i, on the 4-hour January slice."""
import json, os, subprocess, sys, hashlib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import qv2_ladder_pool as L
X = L.X
SP = Path("/private/tmp/claude-501/-Users-vaibhav-Documents-Newproject-FAST-MM/05e290d4-c27d-4bcb-abe7-e02ddda9e697/scratchpad")
_, s7 = L.steps("2022-01-13")
mk = {**s7[1], "opposite_cross_confirm_ms": 500.0}
procs = {}
for snap in ("snap_qv2h", "snap_qv2i"):
    env = dict(os.environ, EXEC_SNAP=str(X / snap), EXEC_OUT=str(SP / "ident_out" / snap), EXEC_TAG="ident", QV2_DATA_ROOT=str(SP / "mini_jan4"),
               QV2_MAKER=json.dumps(mk), QV2_CFG=json.dumps(s7[2]))
    procs[snap] = subprocess.Popen(["nice", "-n", "5", "python3.12", str(X / "qv2_run.py"), "perp_perp_maker_taker_v1", "2022-01-13", "--allow-rejected-data", "smoke slice"],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
res = {}
for snap, p in procs.items():
    out = p.communicate()[0]; print(snap, p.returncode, out[-300:])
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
d = {s: SP / "ident_out" / s / "perp_perp_maker_taker_v1" / "singapore_bybit_perp_mm_clean_s1_ident" / "2022-01-13" for s in procs}
files = {f: sha(d["snap_qv2h"] / f) == sha(d["snap_qv2i"] / f) for f in ("trades.jsonl", "fill_events.jsonl", "fill_bucket_stats.json", "model_settings.json")}
sa, sb = (json.loads((d[s] / "summary.json").read_text()) for s in ("snap_qv2h", "snap_qv2i"))
keys = ("signals", "fills", "orders_sent", "cash_pnl", "gross_pnl", "fees_paid", "ending_equity_common_timestamp", "counters")
summ = {k: sa[k] == sb[k] for k in keys}
res = {"files_identical": files, "summary_fields_identical": summ, "ok": all(files.values()) and all(summ.values()), "fills": sb["fills"], "signals": sb["signals"]}
print(json.dumps(res, indent=1)); (X / "identity_step9.json").write_text(json.dumps(res, indent=1))
