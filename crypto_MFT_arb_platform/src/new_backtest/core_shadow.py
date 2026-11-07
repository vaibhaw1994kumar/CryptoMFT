"""Accounting-only shadow of the core platform (Phase B, matrix proposal 4A; delete when Phase B ends).

The unchanged backtester hands every fill it books, and nothing else, to the core's ledger and fee schedule. The shadow keeps the core's view of positions, trade cash and fees
next to the backtester's own numbers and reports every difference with a class. It only reads: it holds copies of values (Decimal), never gives a core object back, and
the switch ArbBacktestConfig.core_shadow is off by default, so a run without it is untouched.

Compared: (1) position per venue after every fill; (2) cash per venue (both ledgers book fees into cash, so a fee difference shows up here too; the trade-cash check below removes it); (3) fees, two columns: the backtester's own fee and the core FeeSchedule rate at the same notional; (4) equity at the end: the backtester's
common-timestamp valuation (R11) against each venue valued at its own last mid, which is what the core does today (A2).
Not compared (not built, say so in every report): funding (no funding file in March; January settles in the backtester only), margin and liquidation, the core's risk engine and
OMS (order level behaviour stays covered by the parity tests).

Differences are sorted BUG_BACKTEST / BUG_CORE / BY_DESIGN by the rules written next to each check; anything the shadow cannot decide is UNCLASSIFIED and listed."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

TOL_BASE = Decimal("1e-9")          # float against Decimal (matrix C8)
TOL_USDT = Decimal("1e-6")


def _dec(x: float) -> Decimal:
    return Decimal(repr(float(x)))


class CoreShadow:
    def __init__(self, pair: str, venues: tuple[str, ...], fee_rows: list[dict] | None = None, *, funding_rate_dir: str | None = None,
                 start_date: str | None = None, end_date: str | None = None) -> None:
        from crypto_stat_arb.fees import FeeSchedule, Liquidity
        from crypto_stat_arb.ledger import Ledger
        from crypto_stat_arb.models import ChildOrder, Side

        self._ChildOrder, self._Side, self._Liquidity = ChildOrder, Side, Liquidity
        base = pair[:-4] if pair.endswith("USDT") else pair
        self.instrument = f"{base}-USDT-PERP"
        if fee_rows is None:
            import crypto_stat_arb
            fee_rows = json.loads((Path(crypto_stat_arb.__file__).parent / "config" / "perp_research.json").read_text())["fees"]
        self.schedule = FeeSchedule.from_config([r for r in fee_rows if r.get("instrument_id") == self.instrument])
        self.ledger = Ledger()
        self.venues = tuple(venues)
        self.n = 0
        self.bt_fee = Decimal(0)
        self.core_fee = Decimal(0)
        self.max_fill_fee_diff = Decimal(0)
        self.unpriced: list[str] = []
        self.mismatches: list[dict[str, Any]] = []
        self.fee_rows: dict[str, Any] = {}
        self.last_mid: dict[str, float] = {}
        self.funding = None                                   # B2.2: the core FundingEngine, fed the same rate files and the same fills as the backtester's funding_settler
        self._last_ns = 0
        if funding_rate_dir:
            self._init_funding(funding_rate_dir, start_date, end_date)

    def _init_funding(self, directory: str, start_date: str | None, end_date: str | None) -> None:
        import datetime as dt
        from crypto_stat_arb.funding import FundingEngine, FundingRateBook, FundingSpec
        self.ledger.asset_resolver = lambda venue, instrument_id: (instrument_id, "USDT")        # the session's rule for a perpetual: the position asset is the instrument id
        specs = [FundingSpec(self._venue(v), self.instrument, self.instrument, "USDT") for v in self.venues]
        lo = dt.datetime.fromisoformat(start_date).replace(tzinfo=dt.timezone.utc).timestamp() if start_date else None
        hi = dt.datetime.fromisoformat(end_date).replace(tzinfo=dt.timezone.utc).timestamp() + 86_400 if end_date else None
        book, mine = FundingRateBook(), {v.lower() for v in self.venues}
        for path in sorted(Path(directory).glob("*.jsonl")):
            for raw in path.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                row = json.loads(raw)
                if str(row.get("venue", "")).lower() not in mine:
                    continue
                ts = (int(row["funding_ts_ns"]) / 1e9) if row.get("funding_ts_ns") is not None else (int(row["funding_ts_ms"]) / 1e3 if row.get("funding_ts_ms") is not None else None)
                if ts is None or (lo is not None and ts < lo) or (hi is not None and ts >= hi):
                    continue
                book.add_row(dict(row, venue=str(row["venue"]).upper(), instrument_id=self.instrument), "shadow")
        self.funding = FundingEngine(self.ledger, specs, book, mark_fallback="book_mid")

    def on_mid(self, venue: str, now: float, mid: float) -> None:
        """B2.2: every book event's mid, so the core engine can mark a settlement at the mid prevailing at its time."""
        if self.funding is not None and mid > 0:
            self._last_ns = int(now * 1_000_000_000)
            self.funding.observe(self._venue(venue), self.instrument, self._last_ns, _dec(mid))

    def _venue(self, v: str) -> str:
        return v.upper()

    def on_fill(self, fill, inventory) -> None:
        """Called by the backtester after it booked `fill` (inventory already updated). Reads only."""
        self.n += 1
        liq = self._Liquidity.TAKER if fill.is_taker else self._Liquidity.MAKER
        venue = self._venue(fill.venue)
        rate = self.schedule.rate_bps(venue, self.instrument, liq)
        qty, px = _dec(fill.qty), _dec(fill.price)
        notional = qty * px
        if rate is None:
            self.unpriced.append(f"{venue}/{self.instrument}")
            core_fee = Decimal(0)
        else:
            core_fee = notional * rate / Decimal(10_000)
            self.fee_rows.setdefault(f"{venue}/{liq.value}", str(rate))
        bt_fee = _dec(fill.fee)
        self.bt_fee += bt_fee
        self.core_fee += core_fee
        self.max_fill_fee_diff = max(self.max_fill_fee_diff, abs(bt_fee - core_fee))
        order = self._ChildOrder(order_id=f"shadow-{self.n}", parent_id=f"shadow-{fill.signal_id}", opportunity_id=f"shadow-{fill.signal_id}", venue=venue,
                                 instrument_id=self.instrument, side=self._Side.BUY if fill.side == "buy" else self._Side.SELL, quantity=qty, limit_price=px,
                                 filled_quantity=qty, average_price=px, fee=core_fee, liquidity=liq.value, fee_rate_bps=rate, fee_source="ASSUMED")
        self.ledger.apply_fill(order, int(fill.ts * 1_000_000_000))
        self._compare_after_fill(fill, inventory)

    def _compare_after_fill(self, fill, inventory) -> None:
        key = (self._venue(fill.venue), self.instrument)
        core_pos = self.ledger.positions.get(key, Decimal(0))
        bt_pos = _dec(inventory.base.get(fill.venue, 0.0))
        if abs(core_pos - bt_pos) > TOL_BASE:
            self.mismatches.append({"fill": self.n, "venue": fill.venue, "what": "position", "bt": str(bt_pos), "core": str(core_pos), "diff": str(core_pos - bt_pos)})

    def _compare_funding(self, bt_paid: dict[str, float], bt_settlements: list[dict]) -> dict[str, Any]:
        """Backtester funding_settler against the core FundingEngine: totals per venue and settlement by settlement (venue, time). Sign: the backtester's payment is positive when PAID,
        the core's amount is the holder's cash flow (negative when paid)."""
        core = {}
        for e in self.funding.settled_events():
            d = dict(e.details)
            core[(e.venue.lower(), round(e.ts_ns / 1e6))] = {"position": Decimal(d["position"]), "rate": Decimal(d["rate"]), "mark": Decimal(d["mark_price"]), "paid": -e.postings[0].quantity,
                                                           "mark_source": e.valuation_source}
        core_by_venue: dict[str, Decimal] = {}
        for (v, _), c in core.items():
            core_by_venue[v] = core_by_venue.get(v, Decimal(0)) + c["paid"]
        rows, unmatched_bt, max_diff, mark_diff = [], [], Decimal(0), 0
        seen = set()
        for r in bt_settlements:
            key = (str(r["venue"]).lower(), round(float(r["ts"]) * 1000))
            c = core.get(key)
            if c is None:
                if float(r["payment"]) != 0.0:
                    unmatched_bt.append({"venue": r["venue"], "ts": r["ts"], "payment": r["payment"]})
                continue
            seen.add(key)
            diff = c["paid"] - _dec(r["payment"])
            max_diff = max(max_diff, abs(diff))
            if abs(c["mark"] - _dec(r["mark"])) > Decimal("1e-9"):
                mark_diff += 1
            rows.append({"venue": r["venue"], "ts": r["ts"], "position": {"bt": str(_dec(r["position_base"])), "core": str(c["position"])}, "rate": {"bt": str(_dec(r["rate"])), "core": str(c["rate"])},
                         "mark": {"bt": str(_dec(r["mark"])), "core": str(c["mark"])}, "paid": {"bt": str(_dec(r["payment"])), "core": str(c["paid"]), "diff": str(diff)}})
        core_only = [{"venue": k[0], "ts_ms": k[1], "paid": str(c["paid"])} for k, c in core.items() if k not in seen]
        by_venue = {v: {"bt_paid": str(_dec(bt_paid.get(v, 0.0))), "core_paid": str(core_by_venue.get(v.lower(), Decimal(0))), "diff": str(core_by_venue.get(v.lower(), Decimal(0)) - _dec(bt_paid.get(v, 0.0)))} for v in self.venues}
        return {"settlements_compared": len(rows), "max_abs_payment_diff": str(max_diff), "mark_differs_in": mark_diff, "unmatched_backtester": unmatched_bt, "core_only": core_only, "by_venue": by_venue,
                "flags": [{"code": f["code"], "blocking": f["blocking"]} for f in self.funding.flags][:20], "rows": rows[:50]}

    def finish(self, inventory, mids: dict[str, float], bt_ending_equity: float | None, funding_paid: dict[str, float] | None = None, valuation: dict | None = None,
               funding_settlements: list[dict] | None = None) -> dict[str, Any]:
        """The report. `valuation`: the result of the core's own `crypto_stat_arb.valuation.common_timestamp_valuation` (B2.1): when it is available the core ledger is also marked at those prices, which resolves A2. `mids`: each venue's last book mid; `bt_ending_equity`: the backtester's common-timestamp ending equity; `funding_paid`: funding the backtester settled into
        each venue's cash (payment positive = paid). The core funding engine is not in the shadow, so that cash is taken out of the backtester's side before the comparison and reported."""
        funding_paid = {v: float(x) for v, x in (funding_paid or {}).items()}
        funding_report = None
        if self.funding is not None:
            self.funding.settle_until(self._last_ns, final=True)
            funding_report = self._compare_funding(funding_paid, funding_settlements or [])
            funding_paid_bt = funding_paid
            funding_paid = {}                                  # both ledgers now book funding into cash: nothing is taken out of the backtester's side
        else:
            funding_paid_bt = funding_paid
        positions, cash = {}, {}
        for v in self.venues:
            key = (self._venue(v), self.instrument)
            core_pos = self.ledger.positions.get(key, Decimal(0))
            bt_pos = _dec(inventory.base.get(v, 0.0))
            positions[v] = {"bt": str(bt_pos), "core": str(core_pos), "diff": str(core_pos - bt_pos), "ok": abs(core_pos - bt_pos) <= TOL_BASE}
            core_cash = self.ledger.cash_deltas.get(self._venue(v), Decimal(0))
            bt_cash = _dec(inventory.cash.get(v, 0.0) + funding_paid.get(v, 0.0))       # funding was booked as cash -= payment: add it back
            cash[v] = {"bt_cash": str(bt_cash), "core_cash": str(core_cash), "diff": str(core_cash - bt_cash)}
        bt_cash_total = sum((_dec(inventory.cash.get(v, 0.0) + funding_paid.get(v, 0.0)) for v in self.venues), Decimal(0))
        core_cash_total = sum((self.ledger.cash_deltas.get(self._venue(v), Decimal(0)) for v in self.venues), Decimal(0))
        fee_diff = self.core_fee - self.bt_fee
        # both cash figures are net of their own fees, so the gap is (trade cash difference) minus (fee difference); the trade-cash difference is what is checked
        cash_gap = (core_cash_total - bt_cash_total) + fee_diff
        diffs: list[dict[str, Any]] = []
        for p in positions.values():
            if not p["ok"]:
                diffs.append({"id": "POSITION", "class": "BUG_BACKTEST_OR_CORE", "detail": "position differs after the same fills; compare the per-fill mismatches"})
                break
        if abs(cash_gap) > TOL_USDT:
            diffs.append({"id": "TRADE_CASH", "class": "UNCLASSIFIED", "detail": f"trade cash differs by {cash_gap} after removing the fee difference"})
        if self.unpriced:
            diffs.append({"id": "FEE_RULE_MISSING", "class": "BUG_CORE", "detail": f"no core fee rule for {sorted(set(self.unpriced))}"})
        elif abs(fee_diff) > TOL_USDT:
            diffs.append({"id": "C1_FEE_RATE", "class": "BY_DESIGN", "detail": f"core FeeSchedule minus backtester fees = {fee_diff} (core rates {self.fee_rows}); the two stacks carry different assumed rates"})
        equity = None
        if bt_ending_equity is not None:
            core_eq = sum((self.ledger.cash_deltas.get(self._venue(v), Decimal(0)) + self.ledger.positions.get((self._venue(v), self.instrument), Decimal(0)) * _dec(mids.get(v, 0.0))
                           for v in self.venues), Decimal(0))             # the core ledger's cash is already net of its fees
            gap = core_eq - (_dec(bt_ending_equity) + sum((_dec(x) for x in funding_paid.values()), Decimal(0)))     # funding taken out of the backtester's equity too
            valuation_diff = gap + fee_diff                        # the part of the gap that is not the fee difference (core equity is net of the core fee, the backtester's of its own)
            equity = {"bt_common_timestamp": str(_dec(bt_ending_equity)), "core_per_venue_last_mid": str(core_eq), "diff": str(gap), "diff_not_explained_by_fees": str(valuation_diff),
                      "note": "A2: the backtester marks every venue at the last time all venues had data (R11); the core marks each venue at its own last mid."}
            if valuation is not None and valuation.get("available"):
                marks = {v: _dec(valuation["marks"][v]) for v in self.venues}
                core_eq_c = sum((self.ledger.cash_deltas.get(self._venue(v), Decimal(0)) + self.ledger.positions.get((self._venue(v), self.instrument), Decimal(0)) * marks[v]
                                 for v in self.venues), Decimal(0))
                gap_c = core_eq_c - (_dec(bt_ending_equity) + sum((_dec(x) for x in funding_paid.values()), Decimal(0))) + fee_diff
                equity["core_at_common_timestamp_marks"] = str(core_eq_c)
                equity["diff_not_explained_by_fees_at_common_timestamp_marks"] = str(gap_c)
                equity["common_ts"] = valuation.get("common_ts")
                if abs(gap_c) <= TOL_USDT:
                    equity["a2"] = "RESOLVED: the core's own common_timestamp_valuation, applied to the core ledger, gives the backtester's equity (the function lives in the core since Phase A; only the per-venue last-mid shortcut differed)"
                    valuation_diff = Decimal(0)
                else:
                    valuation_diff = gap_c
            if abs(valuation_diff) > TOL_USDT:
                diffs.append({"id": "A2_VALUATION", "class": "BY_DESIGN", "detail": f"equity differs by {valuation_diff} beyond the fee difference: common-timestamp against per-venue closing marks"})
        return {
            "label": "core accounting shadow, read only; funding, margin, risk and OMS not compared", "fills": self.n, "instrument": self.instrument,
            "tolerance": {"base": str(TOL_BASE), "usdt": str(TOL_USDT)}, "positions": positions, "trade_cash": cash,
            "fees": {"bt_total": str(self.bt_fee), "core_total": str(self.core_fee), "diff": str(fee_diff), "max_per_fill_abs_diff": str(self.max_fill_fee_diff), "core_rates_bps": self.fee_rows},
            "trade_cash_gap_after_removing_fee_difference": str(cash_gap), "funding_booked_by_backtester_not_compared": ({} if funding_report is not None else {v: str(_dec(x)) for v, x in funding_paid.items()}), "funding": funding_report, "equity": equity, "differences": diffs, "mismatches_per_fill": self.mismatches[:20], "ok": not diffs,
        }
