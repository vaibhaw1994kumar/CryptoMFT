"""Day-1 end-to-end replay, step B0 (a run wrapper, READ ONLY on data/): verify every present file of the recorder's capture against catalog.json.
The catalog's sha256 is that of the UNCOMPRESSED file; the local parts hold gzip copies, so each is decompressed as a stream and hashed. Reports mismatches, the file types the catalog lists but the disk lacks, and the parts missing.
usage: intake.py SESSION_DIR OUT_JSON"""
import gzip, hashlib, json, sys, time
from pathlib import Path
D, OUT = Path(sys.argv[1]), Path(sys.argv[2])
cat = json.loads((D / "catalog.json").read_text())
res = {"session_id": cat["session_id"], "parts_in_catalog": len(cat["parts"]), "checked": 0, "ok": 0, "mismatch": [], "missing_on_disk_by_type": {}, "present_by_type": {}, "parts_missing": [], "extra_files": []}
t0 = time.time()
for i, p in enumerate(cat["parts"], 1):
    name = p["blob"].split("/")[-1].replace(".tar.gz", "")
    pd = D / name
    if not pd.is_dir():
        res["parts_missing"].append(name); continue
    present = {f.name for f in pd.iterdir()}
    for fname, want in p["files"].items():
        cands = [pd / fname, pd / (fname + ".gz")]
        f = next((c for c in cands if c.exists()), None)
        if f is None:
            res["missing_on_disk_by_type"][fname] = res["missing_on_disk_by_type"].get(fname, 0) + 1
            continue
        h = hashlib.sha256()
        with (gzip.open(f, "rb") if f.suffix == ".gz" else f.open("rb")) as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        res["checked"] += 1
        res["present_by_type"][fname] = res["present_by_type"].get(fname, 0) + 1
        if h.hexdigest() == want:
            res["ok"] += 1
        else:
            res["mismatch"].append({"part": name, "file": fname, "got": h.hexdigest(), "want": want})
    known = set(p["files"]) | {n + ".gz" for n in p["files"]}
    for x in present - known:
        res["extra_files"].append(f"{name}/{x}")
    if i % 100 == 0:
        print(time.strftime("%T"), i, "parts", res["checked"], "files", len(res["mismatch"]), "mismatches", flush=True)
res["seconds"] = round(time.time() - t0, 1)
OUT.write_text(json.dumps(res, indent=1))
print(json.dumps({k: (v if not isinstance(v, (list, dict)) or len(json.dumps(v)) < 400 else f"{len(v)} entries") for k, v in res.items()}, indent=1))
