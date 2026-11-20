"""Phase 2 F02a (owner yes, 20 Nov): one read of the PUBLIC venue rule endpoints, no keys. Run wrapper only; parsing is in crypto_stat_arb.contract_master.
Saves raw responses under crypto_MFT_arb_platform/config/contract_master/raw/<UTC stamp>/:
  binance_usd_m_futures.json  (GET fapi/v1/exchangeInfo)
  bybit_linear.json           (GET v5/market/instruments-info?category=linear, every page)
  bybit_linear_risk_limit.json(GET v5/market/risk-limit?category=linear, every page)"""
import json, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from crypto_stat_arb.contract_master import BINANCE_ENDPOINTS, PublicHttpClient, _fetch_bybit_category
import subprocess
from urllib.parse import urlencode


class CurlClient(PublicHttpClient):
    """Same GET as PublicHttpClient, through curl: this Mac's Python TLS store does not hold the CA of the HTTPS path here (urllib: self-signed certificate in chain); curl uses the system store.
    Verification stays ON (no -k)."""
    def get_json(self, url, params=None):
        target = f"{url}?{urlencode(params)}" if params else url
        r = subprocess.run(["curl", "-sS", "--fail", "--max-time", "60", "-A", "crypto-stat-arb-contract-master/1.0", target], capture_output=True, check=True)
        return json.loads(r.stdout)

ROOT = Path("/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/config/contract_master")
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = ROOT / "raw" / stamp
out.mkdir(parents=True, exist_ok=True)
c = CurlClient()
(out / "binance_usd_m_futures.json").write_text(json.dumps(c.get_json(BINANCE_ENDPOINTS["usd_m_futures"])))
_, pages = _fetch_bybit_category(c, "linear")
(out / "bybit_linear.json").write_text(json.dumps(pages))
pages, cursor, seen = [], "", set()
while True:
    p = c.get_json("https://api.bybit.com/v5/market/risk-limit", {"category": "linear", **({"cursor": cursor} if cursor else {})})
    if p.get("retCode") != 0:
        raise RuntimeError(f"bybit risk-limit {p.get('retCode')} {p.get('retMsg')}")
    pages.append(p)
    cursor = (p.get("result") or {}).get("nextPageCursor", "")
    if not cursor or cursor in seen:
        break
    seen.add(cursor)
(out / "bybit_linear_risk_limit.json").write_text(json.dumps(pages))
meta = {"fetched_utc": stamp, "endpoints": [BINANCE_ENDPOINTS["usd_m_futures"], "https://api.bybit.com/v5/market/instruments-info?category=linear", "https://api.bybit.com/v5/market/risk-limit?category=linear"],
        "keys": "none (public)", "label": "current rules at the fetch time, not historical"}
(out / "fetch_meta.json").write_text(json.dumps(meta, indent=1))
print(out, {f.name: f.stat().st_size for f in out.iterdir()})
