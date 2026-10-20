"""F-06: funding and borrow survive an OMS restart: no duplicate and no missing posting."""

from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.config import load_config
from crypto_stat_arb.economics import EconomicEventType
from crypto_stat_arb.models import MarketEvent
from crypto_stat_arb.plan_store import ParentPlanStore
from crypto_stat_arb.session import TradingSession


ROOT = Path(__file__).parents[1]
HOUR = 3_600_000_000_000
START = 10 * HOUR + 1_000_000_000          # 10:00:01


def snapshot(venue, ts, sequence, bid, ask, instrument):
    native = {"lastUpdateId": sequence} if venue == "BINANCE" else {"u": sequence}
    return MarketEvent.from_dict({
        "event_type": "BOOK_SNAPSHOT", "venue": venue, "instrument_id": instrument,
        "exchange_ts_ns": ts - 100_000, "receive_ts_ns": ts, "sequence": sequence,
        "bids": [[bid, "1.000"]], "asks": [[ask, "1.000"]], "native": native,
    })


def events(instrument):
    """An arb at 10:00:01 (buy BINANCE, sell BYBIT), then quiet books every 10 minutes to 13:30."""
    rows = [snapshot("BINANCE", START, 100, "59999.90", "60000.00", instrument),
            snapshot("BYBIT", START + 1_000_000, 900, "60060.00", "60060.10", instrument)]
    for n in range(1, 22):
        ts = START + n * 600_000_000_000
        rows.append(snapshot("BINANCE", ts, 100 + n, "60000.00", "60000.10", instrument))
        rows.append(snapshot("BYBIT", ts + 1_000_000, 900 + n, "60000.00", "60000.10", instrument))
    return rows


def write_config(directory: Path, kind: str) -> Path:
    raw = json.loads((ROOT / "config/research.json").read_text())
    if kind == "funding":
        instrument = "BTC-USDT-PERP"
        raw["strategy"]["instrument_id"] = instrument
        raw["risk"]["inventory"] = {"model": "unconstrained"}
        for row in raw["instruments"]:
            row.update(instrument_id=instrument, product_type="PERP", funding_interval_hours=1)
        for row in raw.get("fees", []):
            row["instrument_id"] = instrument
        raw["funding"] = {"settle_lag_ms": 0, "rates": [
            {"venue": v, "instrument_id": instrument, "funding_ts_ns": h * HOUR,
             "rate": str(Decimal("0.0001") * h), "mark_price": "60000", "source": "SIMULATED"}
            for v in ("BINANCE", "BYBIT") for h in (11, 12, 13)
        ]}
    else:
        raw["risk"]["inventory"]["initial_base_by_venue"] = {"BINANCE": "0.05", "BYBIT": "0"}
        for row in raw["instruments"]:
            row["borrow_applicable"] = True
        raw["risk"]["borrow"] = {"settle_lag_ms": 0, "lines": [{
            "venue": "BYBIT", "asset": "BTC", "authorized": True, "limit": "0.05",
            "rate_per_interval": "0.0001", "interval_hours": 1, "source": "CONFIGURED",
        }]}
    path = directory / f"{kind}.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def outcome(session: TradingSession) -> dict:
    marks = {key: Decimal("60000.05") for key in session.books}
    state = session.oms.recalculate_account_state(mark_prices=marks)
    ledger = session.ledger.economics
    reversed_ids = {e.reverses for e in ledger.events if e.reverses}
    financing = sorted(
        (e.idempotency_key, str(sum(p.quantity for p in e.postings)))
        for e in ledger.events
        if e.event_type in (EconomicEventType.FUNDING, EconomicEventType.BORROW_ACCRUAL,
                            EconomicEventType.BORROW_OPEN)
        and e.event_id not in reversed_ids
    )
    return {
        "balances": {f"{v}:{a}:{s}": str(q) for (v, a, s), q in sorted(ledger.balances().items()) if q},
        "venues": {v: {k: r[k] for k in ("funding", "borrow_cost", "equity", "realized_pnl", "fees")}
                   for v, r in state["venues"].items()},
        "financing_events": financing,
        "funding": session.funding.report().get("realized_by_venue"),
        "borrow": {k: {f: row[f] for f in ("principal", "interest_due")}
                   for k, row in session.borrow.report().get("lines", {}).items()},
    }


def run(config: Path, store_path: Path, rows, restart_after: int | None = None, crash_window: bool = False):
    cfg, _ = load_config(config)
    store = ParentPlanStore(store_path)
    session = TradingSession(cfg, plan_store=store)
    for event in rows[: restart_after or len(rows)]:
        session.on_event(event)
    if restart_after is not None:
        if crash_window:
            # The process died after journaling events but before checkpointing.
            store.connection.execute("DELETE FROM financing_state")
            store.connection.commit()
        store.close()                                    # crash: no drain, no shutdown
        store = ParentPlanStore(store_path)
        session = TradingSession(cfg, plan_store=store)
        for event in rows[restart_after:]:
            session.on_event(event)
    session.drain()
    result = outcome(session)
    store.close()
    return result


class FinancingRestartTests(unittest.TestCase):
    def check(self, kind: str, instrument: str):
        rows = events(instrument)
        restart = next(i for i, e in enumerate(rows) if e.receive_ts_ns > 11 * HOUR + 30 * 60 * 10**9)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = write_config(base, kind)
            straight = run(config, base / "a.sqlite", rows)
            restarted = run(config, base / "b.sqlite", rows, restart_after=restart)
            crashed = run(config, base / "c.sqlite", rows, restart_after=restart, crash_window=True)
        return straight, restarted, crashed

    def test_funding_across_a_restart_matches_an_uninterrupted_run(self):
        straight, restarted, crashed = self.check("funding", "BTC-USDT-PERP")
        self.assertEqual(len(straight["financing_events"]), 6)       # 2 venues x 11:00, 12:00, 13:00
        self.assertNotEqual(straight["funding"], {})
        self.assertEqual(restarted, straight)
        self.assertEqual(crashed, straight)

    def test_borrow_across_a_restart_matches_an_uninterrupted_run(self):
        straight, restarted, crashed = self.check("borrow", "BTC-USDT-SPOT")
        kinds = [key.split("|")[0] for key, _ in straight["financing_events"]]
        self.assertEqual(kinds.count("borrow-interest"), 3)           # 11:00, 12:00, 13:00
        self.assertEqual(kinds.count("borrow-open"), 1)
        self.assertGreater(Decimal(straight["venues"]["BYBIT"]["borrow_cost"]), 0)
        self.assertEqual(restarted, straight)
        self.assertEqual(crashed, straight)


if __name__ == "__main__":
    unittest.main()
