"""Queue item (e), owner 17 Nov: the TESTNET amend drill on the platform's own order manager (OMS), SOLUSDT linear perpetuals, Bybit and Binance.

TESTNET ONLY: the transports refuse any host that is not a testnet host (testnet.TESTNET_HOSTS), and this script adds its own check. Keys are read from the macOS Keychain (the items the
order_lifecycle_logger/testnet_keys.sh helper stores) inside the credential broker's read call, used for the request signature and wiped; they are never printed, logged or written.
The report holds aggregates only: no order ids, no raw venue responses.

Steps, per venue unless stated:
  A  amend moves the SAME order: a post-only buy far below the touch, amended one tick up through OMS.request_amend; the venue must show exactly ONE open drill order afterwards, and the OMS one
     live order at the new price (AMEND_CONFIRMED only after the venue showed it).
  B  a refused amend: the same, amended to a price that would cross the book (a post-only buy above the ask); the OMS must record AMEND_REJECTED and never show a live quote the venue does not have.
  C  an amend after a fill (Binance, where the testnet spread is wide): a post-only buy one tick under the ask, waited on for up to --fill-wait-s; if it fills, an amend must be refused as "replace
     target is not open". No fill in time: UNVERIFIED.
  D  a reduce-only close at the Binance minimum: with a long position (from C, or a market buy of the minimum notional), a reduce-only sell of the minimum QUANTITY (0.01 SOL, far below the 5 USDT
     minimum notional) shows whether a reduce-only order is exempt from the minimum notional; the rest is then closed reduce-only.
At the end every drill order is cancelled and both positions are checked flat (a non-flat position is closed reduce-only and reported).

Usage: python3.12 scripts/testnet_amend_drill.py OUT_DIR [--fill-wait-s 60]
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from crypto_stat_arb.credentials import CallbackSecretManager, CredentialBroker, CredentialEnvironment, CredentialPermission  # noqa: E402
from crypto_stat_arb.ems.config import ExecutionConfig  # noqa: E402
from crypto_stat_arb.execution import ExecutionEventType  # noqa: E402
from crypto_stat_arb.models import MarketEvent, OrderState, Side  # noqa: E402
from crypto_stat_arb.session import ExecutionMode, TradingSession  # noqa: E402
from crypto_stat_arb.testnet import BINANCE_FUTURES_TESTNET_URL, BYBIT_TESTNET_URL, TESTNET_HOSTS, build_testnet_adapters  # noqa: E402

SERVICE = "cryptomft-testnet"                      # the Keychain service of testnet_keys.sh
KEYS = {"BYBIT": ("BYBIT_TESTNET_KEY", "BYBIT_TESTNET_SECRET"), "BINANCE": ("BINANCE_FUT_TESTNET_KEY", "BINANCE_FUT_TESTNET_SECRET")}
PUBLIC = {"BYBIT": "https://api-testnet.bybit.com/v5/market/orderbook?category=linear&symbol=SOLUSDT&limit=50",
          "BINANCE": "https://testnet.binancefuture.com/fapi/v1/depth?symbol=SOLUSDT&limit=50"}
SIZE = {"BYBIT": Decimal("0.1"), "BINANCE": Decimal("0.07")}      # the venues' minimums (Bybit 0.1 SOL; Binance 5 USDT notional at about 72.6)
TICK = Decimal("0.01")
for url in (BYBIT_TESTNET_URL, BINANCE_FUTURES_TESTNET_URL, *PUBLIC.values()):
    if urlparse(url).hostname not in TESTNET_HOSTS:
        raise SystemExit(f"refusing a non-testnet host: {url}")


def keychain(account: str) -> str:
    out = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"], capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"the Keychain has no {account} (run: sh order_lifecycle_logger/testnet_keys.sh check)")
    return out.stdout.strip()


def read_secret(reference: str, version: int) -> bytearray:
    key_name, secret_name = KEYS[reference]
    return bytearray(f"{keychain(key_name)}:{keychain(secret_name)}".encode("ascii"))


def refuse(*args):
    raise RuntimeError("the drill's secret manager is read-only (keys live in the Keychain)")


def public_book(venue: str, instrument_id: str, seq: int) -> MarketEvent:
    from crypto_stat_arb.testnet import verified_ssl_context
    with urlopen(PUBLIC[venue], timeout=10, context=verified_ssl_context()) as response:
        doc = json.loads(response.read())
    ts_ns = time.time_ns()                                  # stamped when the book ARRIVED (a stamp taken before the request made a slow answer look stale)
    bids, asks = (doc["result"]["b"], doc["result"]["a"]) if venue == "BYBIT" else (doc["bids"], doc["asks"])
    return MarketEvent.from_dict({"event_type": "BOOK_SNAPSHOT", "venue": venue, "instrument_id": instrument_id, "exchange_ts_ns": ts_ns, "source_ts_ns": ts_ns, "receive_ts_ns": ts_ns,
                                  "sequence": seq, "bids": [{"price": p, "quantity": q} for p, q in bids], "asks": [{"price": p, "quantity": q} for p, q in asks]})


def build(tmp: Path):
    from new_backtest.profiles import apply_profile, load_profile
    from new_backtest.session_replay import SessionReplay, dated_ticks
    from crypto_stat_arb.config import config_from_raw
    profile = load_profile("perp_perp_maker_taker_v1_10")
    cfg = apply_profile(profile, dataset="mar2024", data_root="", pair="SOLUSDT", start_date="2026-10-29", end_date="2026-10-29", output_dir=str(tmp))
    raw = SessionReplay(cfg, profile, ticks=dated_ticks("2026-10-29"), recorded=True).raw_config
    app, _ = config_from_raw(raw)
    manager = CallbackSecretManager("macos-keychain", create=lambda *a: None, read=read_secret, rotate=refuse, revoke=refuse)
    broker = CredentialBroker(manager, tmp / "credentials.json", tmp / "credential_audit.jsonl")
    for venue in KEYS:
        broker.register(credential_id=f"drill-{venue.lower()}", venue=venue, environment=CredentialEnvironment.TESTNET,
                        permissions=(CredentialPermission.READ_MARKET, CredentialPermission.READ_ACCOUNT, CredentialPermission.TRADE, CredentialPermission.CANCEL),
                        secret_reference=venue, secret=b"keychain-reference", actor="testnet-amend-drill")
    adapters = build_testnet_adapters(broker, {v: f"drill-{v.lower()}" for v in KEYS}, {v: {app.instrument_id: "SOLUSDT"} for v in KEYS},
                                      markets={v: "linear" for v in KEYS}, margin_modes={"BYBIT": "CROSS"}, verify_accounts=True)
    session = TradingSession(app, execution_mode=ExecutionMode.VENUE_TESTNET, live_data=True, adapters=adapters, strategies=[], intent_journal=True, layer_risk_mode="count",
                             execution_config=ExecutionConfig(pairing="none", quote={"inventory_cap_base": "1"}))
    session.oms.amend_enabled = True             # the OMS's own switch, for this drill only; no profile's real-order amend guard is changed
    return app, session, adapters


class Drill:
    def __init__(self, app, session, adapters):
        self.app, self.session, self.adapters, self.seq, self.n = app, session, adapters, 0, 0
        self.inst = app.instrument_id
        self.report: dict = {"label": "TESTNET drill on the platform OMS, SOLUSDT linear; aggregates only (no order ids, no raw responses)", "steps": {}}
        self.drill_orders: dict[str, list[str]] = {v: [] for v in adapters}

    def now(self) -> int:
        return time.time_ns()

    def refresh(self, venue: str):
        self.seq += 1
        ev = public_book(venue, self.inst, self.seq)
        self.session.books[(venue, self.inst)].apply(ev)
        return self.session.books[(venue, self.inst)]

    def events(self, *names):
        return [e for e in self.session.ledger.events if e["event_type"] in names]

    def snapshot(self, venue: str) -> dict:
        (native,) = self.adapters[venue].transport({"action": "ACCOUNT_SNAPSHOT", "instrument_id": self.inst, "since_ns": self.now() - 3_600_000_000_000})
        ids = set(self.drill_orders[venue])
        mine = [o for o in native["orders"] if o["order_id"] in ids]
        return {"open_drill_orders": sum(1 for o in mine if o["state"] in ("NEW", "PARTIAL")), "position": native["positions"].get(self.inst, "0")}

    def quote(self, venue: str, price: Decimal) -> object:
        self.n += 1
        plan = self.session.oms.execute_quote(quote_id=f"drill-{venue.lower()}-{self.n}-{int(time.time())}", strategy_id="drill", instrument_id=self.inst, venue=venue, side=Side.BUY, price=price,
                                              quantity=SIZE[venue], books=self.session.books, observed_ts_ns=self.now())
        self.drill_orders[venue] += [c.order_id for c in plan.children]
        return plan

    def live(self, plan):
        return [c for c in plan.children if c.state in (OrderState.ACKNOWLEDGED, OrderState.PARTIAL, OrderState.AMEND_PENDING, OrderState.SENT)]

    def settle(self, seconds=1.0):
        end = time.time() + seconds
        while time.time() < end:
            self.session.oms.check_order_deadlines(self.now())
            time.sleep(0.2)

    def cancel_all(self, plan):
        for c in self.live(plan):
            self.session.oms.urgent_cancel(c.order_id, self.now(), "drill_cleanup")
        self.settle(1.0)

    def step_a(self, venue):
        book = self.refresh(venue)
        price = Decimal(str(book.best_bid)) - 20 * TICK
        plan = self.quote(venue, price)
        head = plan.children[0]
        out = {"placed_state": head.state.value, **({"placed_reject_reason": str(head.reject_reason)[:160]} if head.reject_reason else {})}
        before = len(self.events("AMEND_CONFIRMED"))
        out["amend_request"] = self.session.oms.request_amend(head.order_id, price + TICK, self.now(), "drill")
        self.drill_orders[venue] += [c.order_id for c in plan.children if c.replaces_order_id]
        self.settle(1.5)
        live = self.live(plan)
        snap = self.snapshot(venue)
        out.update({"amend_confirmed": len(self.events("AMEND_CONFIRMED")) - before, "oms_live_orders": len(live), "oms_live_price_is_new": bool(live) and live[-1].limit_price == price + TICK,
                    "venue_open_drill_orders": snap["open_drill_orders"]})
        out["result"] = "PASS" if (out["amend_confirmed"] == 1 and out["oms_live_orders"] == 1 and out["oms_live_price_is_new"] and snap["open_drill_orders"] == 1) else "FAIL"
        self.cancel_all(plan)
        out["venue_open_after_cancel"] = self.snapshot(venue)["open_drill_orders"]
        return out

    def step_b(self, venue):
        book = self.refresh(venue)
        plan = self.quote(venue, Decimal(str(book.best_bid)) - 20 * TICK)
        head = plan.children[0]
        if head.state != OrderState.ACKNOWLEDGED:
            return {"result": "ERROR", "placed_state": head.state.value, "placed_reject_reason": str(head.reject_reason)[:160]}
        before = {k: len(self.events(k)) for k in ("AMEND_REJECTED", "AMEND_CONFIRMED", "AMEND_TIMEOUT")}
        crossing = Decimal(str(self.refresh(venue).best_ask)) + 2 * TICK
        out = {"amend_request": self.session.oms.request_amend(head.order_id, crossing, self.now(), "drill_crossing")}
        self.drill_orders[venue] += [c.order_id for c in plan.children if c.replaces_order_id]
        self.settle(2.0)
        out.update({k: len(self.events(k)) - before[k] for k in before})
        snap = self.snapshot(venue)
        venue_status = self.session.router.query_order(venue, head.order_id, self.now())
        out.update({"oms_live_orders": len(self.live(plan)), "venue_open_drill_orders": snap["open_drill_orders"], "reject_reasons": sorted({str(e.get("reason", ""))[:120] for e in self.events("AMEND_REJECTED")[before["AMEND_REJECTED"]:]}),
                    "venue_state_of_the_order": None if venue_status is None or venue_status.state is None else venue_status.state.value})
        consistent = out["oms_live_orders"] == snap["open_drill_orders"] and out["AMEND_CONFIRMED"] == 0
        out["result"] = "PASS" if consistent and out["AMEND_REJECTED"] == 1 else "PASS_ORDER_ENDED_BY_VENUE" if consistent and snap["open_drill_orders"] == 0 else "FAIL"
        self.cancel_all(plan)
        return out

    def step_b2(self, venue):
        """An amend the venue must REFUSE outright and leave the order as it was: a price far below the venue's band (half the bid)."""
        book = self.refresh(venue)
        price = Decimal(str(book.best_bid)) - 20 * TICK
        plan = self.quote(venue, price)
        head = plan.children[0]
        if head.state != OrderState.ACKNOWLEDGED:
            return {"result": "ERROR", "placed_state": head.state.value, "placed_reject_reason": str(head.reject_reason)[:160]}
        before = {k: len(self.events(k)) for k in ("AMEND_REJECTED", "AMEND_CONFIRMED", "AMEND_TIMEOUT")}
        far = (Decimal(str(book.best_bid)) / 2).quantize(TICK)
        out = {"amend_request": self.session.oms.request_amend(head.order_id, far, self.now(), "drill_out_of_band")}
        self.drill_orders[venue] += [c.order_id for c in plan.children if c.replaces_order_id]
        self.settle(2.0)
        out.update({k: len(self.events(k)) - before[k] for k in before})
        snap = self.snapshot(venue)
        live = self.live(plan)
        out.update({"oms_live_orders": len(live), "oms_live_price_unchanged": bool(live) and live[-1].limit_price == price, "venue_open_drill_orders": snap["open_drill_orders"],
                    "reject_reasons": sorted({str(e.get("reason", ""))[:160] for e in self.events("AMEND_REJECTED")[before["AMEND_REJECTED"]:]})})
        out["result"] = "PASS" if (out["AMEND_REJECTED"] == 1 and out["oms_live_orders"] == 1 and out["oms_live_price_unchanged"] and snap["open_drill_orders"] == 1) else "FAIL"
        self.cancel_all(plan)
        return out

    def step_c(self, venue, wait_s):
        book = self.refresh(venue)
        bid, ask = Decimal(str(book.best_bid)), Decimal(str(book.best_ask))
        if ask - bid < 2 * TICK:
            return {"result": "UNVERIFIED", "why": "spread is one tick: no price inside the spread"}
        plan = self.quote(venue, ask - TICK)
        head = plan.children[0]
        deadline = time.time() + wait_s
        while time.time() < deadline and head.state != OrderState.FILLED:
            status = self.session.router.query_order(venue, head.order_id, self.now())
            if status is not None and status.state is not None:
                self.session.oms._apply_venue_status(plan, head, status, self.now())
            time.sleep(2.0)
        if head.state != OrderState.FILLED:
            self.cancel_all(plan)
            return {"result": "UNVERIFIED", "why": f"no fill within {wait_s} s at one tick under the ask", "filled": str(head.filled_quantity)}
        before = len(self.events("AMEND_REJECTED"))
        out = {"amend_request": self.session.oms.request_amend(head.order_id, head.limit_price - TICK, self.now(), "drill_after_fill")}
        self.settle(1.0)
        reasons = sorted({str(e.get("reason", ""))[:120] for e in self.events("AMEND_REJECTED")[before:]})
        out.update({"amend_rejected": len(self.events("AMEND_REJECTED")) - before, "reasons": reasons, "filled": str(head.filled_quantity)})
        out["result"] = "PASS" if (out["amend_request"].startswith("REFUSED") or any("not open" in r for r in reasons)) else "FAIL"
        return out

    def raw(self, venue, side, qty, *, reduce_only, tag):
        self.n += 1
        oid = f"drill-{tag}-{self.n}-{int(time.time())}"
        natives = self.adapters[venue].transport({"action": "SUBMIT", "order_id": oid, "instrument_id": self.inst, "side": side, "order_type": "MARKET", "time_in_force": "IOC",
                                                  "quantity": str(qty), "price": "0", "reduce_only": reduce_only, "post_only": False})
        self.drill_orders[venue].append(oid)
        return [n.get("event_type") for n in natives], [str(n.get("reason", ""))[:120] for n in natives if n.get("event_type") == "REJECTED"]

    def step_d(self, venue="BINANCE"):
        pos = Decimal(self.snapshot(venue)["position"])
        out = {"position_before": str(pos)}
        if pos < SIZE[venue]:
            out["open"] = self.raw(venue, "BUY", SIZE[venue], reduce_only=False, tag="open")[0]
        pos = Decimal(self.snapshot(venue)["position"])
        out["position_open"] = str(pos)
        events, reasons = self.raw(venue, "SELL", Decimal("0.01"), reduce_only=True, tag="ro-min")
        out.update({"reduce_only_0_01_events": events, "reduce_only_0_01_reject_reasons": reasons})
        out["reduce_only_below_min_notional"] = "ACCEPTED (exempt)" if "FILLED" in events else "REFUSED" if "REJECTED" in events else "UNCLEAR"
        out["result"] = "PASS" if out["reduce_only_below_min_notional"] != "UNCLEAR" else "UNVERIFIED"
        return out

    def flatten(self):
        out = {}
        for venue in self.adapters:
            pos = Decimal(self.snapshot(venue)["position"])
            if pos:
                self.raw(venue, "SELL" if pos > 0 else "BUY", abs(pos), reduce_only=True, tag="flat")
            snap = self.snapshot(venue)
            out[venue] = {"position_after": snap["position"], "open_drill_orders": snap["open_drill_orders"]}
        return out


def main() -> int:
    out_dir = Path(sys.argv[1])
    wait_s = float(sys.argv[sys.argv.index("--fill-wait-s") + 1]) if "--fill-wait-s" in sys.argv else 60.0
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        app, session, adapters = build(Path(tmp))
        drill = Drill(app, session, adapters)
        steps = drill.report["steps"]
        try:
            for venue in ("BYBIT", "BINANCE"):
                for name, fn in (("A_amend_moves_the_same_order", drill.step_a), ("B_amend_to_a_crossing_price", drill.step_b), ("B2_amend_refused_out_of_band", drill.step_b2)):
                    try:
                        steps[f"{venue}:{name}"] = fn(venue)
                    except Exception as exc:          # a step that cannot run is reported, never hidden
                        steps[f"{venue}:{name}"] = {"result": "ERROR", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            for name, fn in (("C_amend_after_a_fill", lambda: drill.step_c("BINANCE", wait_s)), ("D_reduce_only_at_the_minimum", drill.step_d)):
                try:
                    steps[f"BINANCE:{name}"] = fn()
                except Exception as exc:
                    steps[f"BINANCE:{name}"] = {"result": "ERROR", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        finally:
            drill.report["cleanup"] = drill.flatten()
            drill.report["counts"] = {"ledger_event_types": {k: sum(1 for e in session.ledger.events if e["event_type"] == k) for k in
                                                             ("AMEND_REQUESTED", "AMEND_CONFIRMED", "AMEND_REJECTED", "AMEND_TIMEOUT", "VENUE_ORDER_QUERY", "FILL")},
                                      "transport_errors": {v: len(a.transport_errors) for v, a in adapters.items()}}
            (out_dir / "testnet_amend_drill.json").write_text(json.dumps(drill.report, indent=1, default=str))
    print(json.dumps({k: v.get("result") for k, v in drill.report["steps"].items()}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
