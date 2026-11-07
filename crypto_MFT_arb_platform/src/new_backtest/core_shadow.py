"""Accounting-only shadow of the core platform (Phase B, matrix proposal 4A; delete when Phase B ends).

The unchanged backtester hands every fill it books, and nothing else, to the core's ledger and fee schedule. The shadow keeps the core's view of positions, trade cash and fees
next to the backtester's own numbers and reports every difference with a class. It only reads: it holds copies of values (Decimal), never gives a core object back, and
the switch ArbBacktestConfig.core_shadow is off by default, so a run without it is untouched.

Compared: (1) position per venue after every fill; (2) cash per venue (both ledgers book fees into cash, so a fee difference shows up here too; the trade-cash check below removes it); (3) fees, two columns: the backtester's own fee and the core FeeSchedule rate at the same notional; (4) equity at the end: the backtester's
common-timestamp valuation (R11) against each venue valued at its own last mid, which is what the core does today (A2).
(5) funding (B2.2): the core FundingEngine beside funding_settler; (6) margin (B2.3): at every periodic check of the backtester's margin model, the core RiskEngine.evaluate_margin (tiers, ECONOMIC_LEDGER_COLLATERAL balance,
cross mode, A9 fail-closed above the last tier) on the shadow ledger, against the model's own equity, maintenance, initial margin and breach decision.
Not compared (not built, say so in every report): the margin gate (initial margin of a new clip, reserved margin for resting orders), isolated mode, transfers, liquidation execution (the shadow never liquidates), the core's
risk engine limits and OMS (order level behaviour stays covered by the parity tests).

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
                 start_date: str | None = None, end_date: str | None = None, margin=None, margin_flat_every: int = 600, funding_venues: tuple[str, ...] | None = None) -> None:
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
        self.fee_by_venue: dict[str, list[Decimal]] = {v: [Decimal(0), Decimal(0)] for v in self.venues}       # [backtester fee, core fee], per venue (B2.3)
        self.funding_venues = tuple(funding_venues) if funding_venues is not None else tuple(venues)       # perp venues only: a spot wallet never pays funding
        self.funding_due: list[tuple[float, str, int]] = []   # B2.5: (settlement time in s, venue, ns) of every rate row in the window, sorted: when the core engine is asked to settle
        self.margin_driven = False                            # B2.6: True when CoreMarginModel decides from this engine: the read-only comparison against the old model is then moot
        self.funding_driven = False                           # B2.5: True when CoreFundingSettler drives the run's cash from this engine: nothing else may settle it
        self.funding = None                                   # B2.2: the core FundingEngine, fed the same rate files and the same fills as the backtester's funding_settler
        self._last_ns = 0
        if funding_rate_dir:
            self._init_funding(funding_rate_dir, start_date, end_date)
        self.margin = None                                    # B2.3: the core RiskEngine margin path, fed the same fills, compared at the backtester's own check instants
        self.margin_cfg = margin
        if margin is not None:
            self._init_margin(margin, margin_flat_every)

    def _init_funding(self, directory: str, start_date: str | None, end_date: str | None) -> None:
        import datetime as dt
        from crypto_stat_arb.funding import FundingEngine, FundingRateBook, FundingSpec
        self.ledger.asset_resolver = lambda venue, instrument_id: (instrument_id, "USDT")        # the session's rule for a perpetual: the position asset is the instrument id
        specs = [FundingSpec(self._venue(v), self.instrument, self.instrument, "USDT", interval_ns=8 * 3_600 * 10**9, anchor_ns=0) for v in self.funding_venues]      # the 8-hour schedule (B2.7a): a missing record is then flagged, and a jittered record (Binance +14 ms) snaps to its boundary
        lo = dt.datetime.fromisoformat(start_date).replace(tzinfo=dt.timezone.utc).timestamp() if start_date else None
        hi = dt.datetime.fromisoformat(end_date).replace(tzinfo=dt.timezone.utc).timestamp() + 86_400 if end_date else None
        book, mine = FundingRateBook(), {v.lower() for v in self.funding_venues}
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
                exact_ns = int(row["funding_ts_ns"]) if row.get("funding_ts_ns") is not None else int(row["funding_ts_ms"]) * 1_000_000        # the engine's own key: a float round trip is off by up to 256 ns
        self.funding = FundingEngine(self.ledger, specs, book, mark_fallback="book_mid")
        # When the engine is asked to settle: every record key AFTER snapping (exact ns) and every 8-hour boundary of the window, so a boundary with no record reaches settle_until during the run and is flagged while cash can still change.
        due = {(v.lower(), ns) for (v, _i, ns) in self.funding.rates.realized}
        if lo is not None and hi is not None:
            step = 8 * 3_600 * 10**9
            for v in self.funding_venues:
                t = -(-int(lo * 1e9) // step) * step
                while t < int(hi * 1e9):
                    due.add((v.lower(), t))
                    t += step
        self.funding_due = sorted((ns / 1e9, v, ns) for v, ns in due)

    def _init_margin(self, cfg, flat_every: int) -> None:
        from crypto_stat_arb.economics import opening_balance_events
        from crypto_stat_arb.instruments import InstrumentMaster
        from crypto_stat_arb.margin import MarginConfig
        from crypto_stat_arb.models import Instrument
        from crypto_stat_arb.risk import AccountRiskSnapshot, RiskEngine, RiskLimits

        self.ledger.asset_resolver = lambda venue, instrument_id: (instrument_id, "USDT")
        self.margin_venues = tuple(v for v in self.venues if v in cfg.venues)
        isolated = [v for v in self.margin_venues if cfg.venues[v].mode != "cross"]
        base = self.instrument.split("-")[0]
        sections = {}
        for v in self.margin_venues:
            vm = cfg.venues[v]
            sections[self._venue(v)] = {"mode": "CROSS", "liquidation": {"enabled": False},
                                        "tiers": {self.instrument: {"source": str(vm.source), "tiers": [
                                            {"max_notional": repr(t.max_notional), "maintenance_rate": repr(t.maintenance_rate), "initial_rate": repr(t.initial_rate),
                                             "maintenance_deduction": repr(t.maintenance_deduction)} for t in vm.tiers]}}}
        for event in opening_balance_events([(self._venue(v), "USDT", _dec(cfg.venues[v].initial_capital)) for v in self.margin_venues]):
            self.ledger.economics.apply(event)
        master = InstrumentMaster([Instrument(instrument_id=self.instrument, venue=self._venue(v), symbol=self.instrument, base=base, quote="USDT", tick_size=Decimal("0.001"),
                                              quantity_step=Decimal("0.001"), min_quantity=Decimal("0.001"), min_notional=Decimal("1"), taker_fee_bps=Decimal("5"), product_type="PERP",
                                              settlement_asset="USDT", funding_interval_ns=8 * 3_600 * 10**9) for v in self.margin_venues])
        risk = RiskEngine(RiskLimits(max_order_notional=Decimal("1e12"), max_gross_notional=Decimal("1e12"), max_residual_base=Decimal("1e12"), max_book_age_ns=10**18),
                          {self._venue(v): AccountRiskSnapshot(venue=self._venue(v), margin_enabled=True) for v in self.margin_venues})
        risk.margin_config = MarginConfig.from_config({"venues": sections})
        risk.instruments = master
        risk.ledger = self.ledger
        self.margin = risk
        self._m = {"flat_every": max(1, int(flat_every)), "checks": 0, "evaluated": 0, "skipped_flat": 0, "skipped_no_mark": 0, "venue_rows": 0, "breach_agree": 0, "breach_disagree": 0,
                   "core_warning": 0, "core_breach": 0, "bt_breach": 0, "max_balance_gap": Decimal(0), "max_maintenance_diff": Decimal(0), "max_initial_diff": Decimal(0), "tier_mismatch": 0,
                   "by_class": {}, "mismatches": [], "not_compared": [f"isolated mode on {isolated}"] if isolated else [], "transfers": cfg.transfers is not None,
                   "above_last_tier_rule": cfg.above_last_tier, "max_notional": Decimal(0)}

    def on_margin_check(self, now: float, rows: dict[str, dict]) -> None:
        """B2.3: one periodic check of the backtester's margin model, called right after the model decided and before any liquidation fill: `rows[venue]` holds the numbers it used and its breach decision. Reads only."""
        if self.margin_driven:
            return
        m = self._m
        m["checks"] += 1
        marks = {v: float(r["mark"]) for v, r in rows.items() if v in self.margin_venues}
        if not marks or any(x <= 0.0 for x in marks.values()):
            m["skipped_no_mark"] += 1
            return
        held = any(r["base"] != 0.0 for v, r in rows.items() if v in self.margin_venues)
        if not held and m["checks"] % m["flat_every"] != 1:
            m["skipped_flat"] += 1
            return
        now_ns = int(now * 1_000_000_000)
        if self.funding is not None and not self.funding_driven:
            self.funding.settle_until(now_ns - 1, final=True)        # the backtester pays funding into cash when it happens; the core engine settles only when asked
        report = self.margin.evaluate_margin(ledger=self.ledger, now_ns=now_ns, mark_prices={(self._venue(v), self.instrument): _dec(x) for v, x in marks.items()},
                                             open_orders={}, enforce_controls=False, dry_run=True)
        core_rows = {r.venue: r for r in report.venues}
        m["evaluated"] += 1
        for v in self.margin_venues:
            self._compare_margin_row(now, v, rows[v], core_rows.get(self._venue(v)))

    def core_margin_rows(self, now: float, marks: dict[str, float]) -> dict[str, Any]:
        """B2.6: the core's margin state per venue (dry run, no open orders) at these marks."""
        now_ns = int(now * 1_000_000_000)
        if self.funding is not None and not self.funding_driven:
            self.funding.settle_until(now_ns - 1, final=True)
        report = self.margin.evaluate_margin(ledger=self.ledger, now_ns=now_ns, mark_prices={(self._venue(v), self.instrument): _dec(x) for v, x in marks.items()}, open_orders={},
                                             enforce_controls=False, dry_run=True)
        return {r.venue.lower(): r for r in report.venues}

    def core_margin_admits(self, venue: str, qty: float, marks: dict[str, float], reserved_qty: float) -> tuple[bool, str]:
        """B2.6: the core's admission check for one more leg of `qty` on `venue` (worst-case exposure: position plus resting orders, then this leg): its initial-margin increment against the free margin
        the core computes. `reserved_qty` is the resting orders' quantity (counted on both sides, as the backtester's gate reserves them)."""
        V = self._venue(venue)
        self.margin.margin_marks = {(self._venue(v), self.instrument): _dec(x) for v, x in marks.items()}
        self.margin.margin_open_orders = {(V, self.instrument): (_dec(reserved_qty), _dec(reserved_qty))} if reserved_qty > 0 else {}
        try:
            increment, free = self.margin._tiered_initial_increment(V, self.instrument, _dec(qty), _dec(marks[venue]), self._last_ns)
        except ValueError as exc:                                       # above the last risk tier: the venue would not accept it
            return False, str(exc)
        finally:
            self.margin.margin_open_orders = {}
        return (increment <= free), f"increment {increment} free {free}"

    def _compare_margin_row(self, now: float, v: str, bt: dict, core) -> None:
        from crypto_stat_arb.risk import MarginState
        m = self._m
        m["venue_rows"] += 1
        inf = Decimal("Infinity")
        fee_bt, fee_core = self.fee_by_venue[v]
        fee_diff = fee_core - fee_bt
        bt_eq = _dec(bt["equity"] - bt["transfer_cash"])
        notional = _dec(bt["notional"])
        m["max_notional"] = max(m["max_notional"], notional)
        if core is None:
            self._margin_mismatch(now, v, "CORE_ROW_MISSING", "BUG_CORE", {})
            return
        gap = core.effective_margin_balance - bt_eq + fee_diff            # core cash is net of the core fee, the backtester's of its own: what is left is not a fee effect
        mm_core = core.maintenance_margin_required
        im_core = core.initial_margin_required
        mm_bt = inf if bt["maintenance"] is None else _dec(bt["maintenance"])
        im_bt = inf if bt["initial"] is None else _dec(bt["initial"])
        m["max_balance_gap"] = max(m["max_balance_gap"], abs(gap))
        d_mm = Decimal(0) if mm_core == mm_bt else abs(mm_core - mm_bt)
        d_im = Decimal(0) if im_core == im_bt else abs(im_core - im_bt)
        if d_mm != inf:
            m["max_maintenance_diff"] = max(m["max_maintenance_diff"], d_mm)
        if d_im != inf:
            m["max_initial_diff"] = max(m["max_initial_diff"], d_im)
        core_breach = core.state == MarginState.BREACH
        m["core_breach"] += core_breach
        m["bt_breach"] += bool(bt["breach"])
        m["core_warning"] += core.state == MarginState.WARNING
        tiers = self.margin_cfg.venues[v].tiers
        bt_tier = next((i for i, t in enumerate(tiers) if bt["notional"] <= t.max_notional), None) if bt["notional"] > 0 else None
        core_tier = next((p["tier"] for p in core.details.get("positions", []) if Decimal(p["quantity"])), None)
        if bt_tier != core_tier and bt["notional"] > 0:
            m["tier_mismatch"] += 1
            self._margin_mismatch(now, v, "TIER", "UNCLASSIFIED", {"bt": bt_tier, "core": core_tier, "notional": str(notional)})
        if abs(gap) > TOL_USDT:
            self._margin_mismatch(now, v, "BALANCE", "UNCLASSIFIED", {"bt_equity": str(bt_eq), "core_balance": str(core.effective_margin_balance), "gap_after_fee_correction": str(gap)})
        if d_mm > TOL_USDT or d_im > TOL_USDT:
            self._margin_mismatch(now, v, "REQUIREMENT", "UNCLASSIFIED", {"bt_maintenance": str(mm_bt), "core_maintenance": str(mm_core), "bt_initial": str(im_bt), "core_initial": str(im_core)})
        if core_breach == bool(bt["breach"]):
            m["breach_agree"] += 1
            return
        m["breach_disagree"] += 1
        corrected = core.effective_margin_balance + fee_diff              # the core balance with the fee difference removed, judged by the backtester's own rule
        corrected_breach = bt["notional"] > 0 and bt["maintenance"] is not None and bt["maintenance"] > 0 and corrected < mm_bt
        if bt["over_tier"] is False and bt["maintenance"] is None and bt["notional"] > 0 and core_breach and self.margin_cfg.above_last_tier == "ignore":
            cls, why = "BY_DESIGN", "A9: above the last tier the backtester ignores (v1, v1.1) while the core fails closed"
        elif core_breach != corrected_breach and corrected_breach == bool(bt["breach"]):
            cls, why = "BY_DESIGN", "C1: the decision flips only because the two stacks carry different fees"
        else:
            cls, why = "UNCLASSIFIED", "breach decision differs"
        self._margin_mismatch(now, v, "BREACH", cls, {"bt_breach": bool(bt["breach"]), "core_state": core.state.value, "alerts": list(core.alert_codes), "why": why})

    def _margin_mismatch(self, now: float, v: str, what: str, cls: str, detail: dict) -> None:
        m = self._m
        key = f"{what}/{cls}"
        m["by_class"][key] = m["by_class"].get(key, 0) + 1
        if len(m["mismatches"]) < 20:
            m["mismatches"].append({"ts": now, "venue": v, "what": what, "class": cls, **detail})

    def on_mid(self, venue: str, now: float, mid: float) -> None:
        """B2.2: every book event's mid, so the core engine can mark a settlement at the mid prevailing at its time."""
        if self.funding is not None and mid > 0:
            self._last_ns = int(now * 1_000_000_000)
            self.funding.observe(self._venue(venue), self.instrument, self._last_ns, _dec(mid))

    def _venue(self, v: str) -> str:
        return v.upper()

    def on_fill(self, fill, inventory, liquidation_fee_bps: float | None = None) -> None:
        """Called by the backtester after it booked `fill` (inventory already updated). Reads only. `liquidation_fee_bps` (B2.7): the fill is a margin liquidation: the declared fee rides on the taker fee and the core books it as a LIQUIDATION."""
        self.n += 1
        liq = self._Liquidity.TAKER if fill.is_taker else self._Liquidity.MAKER
        venue = self._venue(fill.venue)
        rate = self.schedule.rate_bps(venue, self.instrument, liq)
        is_liq = liquidation_fee_bps is not None
        if is_liq and rate is not None:
            rate = rate + _dec(liquidation_fee_bps)
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
        if fill.venue in self.fee_by_venue:
            self.fee_by_venue[fill.venue][0] += bt_fee
            self.fee_by_venue[fill.venue][1] += core_fee
        self.max_fill_fee_diff = max(self.max_fill_fee_diff, abs(bt_fee - core_fee))
        order = self._ChildOrder(order_id=f"shadow-{self.n}", parent_id=f"shadow-{fill.signal_id}", opportunity_id=f"shadow-{fill.signal_id}", venue=venue,
                                 instrument_id=self.instrument, side=self._Side.BUY if fill.side == "buy" else self._Side.SELL, quantity=qty, limit_price=px,
                                 filled_quantity=qty, average_price=px, fee=core_fee, liquidity="LIQUIDATION" if is_liq else liq.value, fee_rate_bps=rate,
                                 fee_source="LIQUIDATION_FEE_ASSUMED" if is_liq else "ASSUMED", **({"order_role": "LIQUIDATION"} if is_liq else {}))
        if is_liq:
            from crypto_stat_arb.economics import EconomicEventType
            self.ledger.apply_fill(order, int(fill.ts * 1_000_000_000), economic_type=EconomicEventType.LIQUIDATION)
        else:
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

    def funding_settler(self) -> "CoreFundingSettler":
        return CoreFundingSettler(self)

    def finish(self, inventory, mids: dict[str, float], bt_ending_equity: float | None, funding_paid: dict[str, float] | None = None, valuation: dict | None = None,
               funding_settlements: list[dict] | None = None) -> dict[str, Any]:
        """The report. `valuation`: the result of the core's own `crypto_stat_arb.valuation.common_timestamp_valuation` (B2.1): when it is available the core ledger is also marked at those prices, which resolves A2. `mids`: each venue's last book mid; `bt_ending_equity`: the backtester's common-timestamp ending equity; `funding_paid`: funding the backtester settled into
        each venue's cash (payment positive = paid). The core funding engine is not in the shadow, so that cash is taken out of the backtester's side before the comparison and reported."""
        funding_paid = {v: float(x) for v, x in (funding_paid or {}).items()}
        funding_report = None
        if self.funding is not None:
            if not self.funding_driven:
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
        margin_report = None
        if self.margin is not None:
            mg = self._m
            margin_report = {"checks_seen": mg["checks"], "evaluated": mg["evaluated"], "skipped_flat": mg["skipped_flat"], "skipped_no_mark": mg["skipped_no_mark"], "venue_rows_compared": mg["venue_rows"],
                             "breach": {"agree": mg["breach_agree"], "disagree": mg["breach_disagree"], "backtester_breach_rows": mg["bt_breach"], "core_breach_rows": mg["core_breach"], "core_warning_rows": mg["core_warning"]},
                             "max_balance_gap_after_fee_correction": str(mg["max_balance_gap"]), "max_maintenance_diff": str(mg["max_maintenance_diff"]), "max_initial_diff": str(mg["max_initial_diff"]),
                             "tier_mismatches": mg["tier_mismatch"], "max_notional_seen": str(mg["max_notional"]), "above_last_tier_rule": mg["above_last_tier_rule"], "differences_by_class": mg["by_class"],
                             "mismatches": mg["mismatches"], "not_compared": mg["not_compared"] + ["the margin gate (initial margin of a new clip, reserved margin of resting orders)", "liquidation execution", "liquidation price and distance"]
                             + (["transfers (equity compared net of transfer cash)"] if mg["transfers"] else []),
                             "sampling": f"checks with an open position are all evaluated; flat checks are evaluated once in {self._m['flat_every']}"}
            if mg["by_class"]:
                unclassified = {k: n for k, n in mg["by_class"].items() if k.endswith("/UNCLASSIFIED")}
                by_design = {k: n for k, n in mg["by_class"].items() if k.endswith("/BY_DESIGN")}
                other = {k: n for k, n in mg["by_class"].items() if k.endswith("/BUG_CORE")}
                if unclassified or other:
                    diffs.append({"id": "MARGIN", "class": "UNCLASSIFIED" if unclassified else "BUG_CORE", "detail": f"margin differs: {dict(unclassified, **other)}"})
                if by_design:
                    diffs.append({"id": "MARGIN_BY_DESIGN", "class": "BY_DESIGN", "detail": f"margin differs by design: {by_design}"})
        return {
            "label": "core accounting shadow, read only; margin gate, isolated mode, transfers, liquidation execution, risk limits and OMS not compared", "fills": self.n, "instrument": self.instrument,
            "tolerance": {"base": str(TOL_BASE), "usdt": str(TOL_USDT)}, "positions": positions, "trade_cash": cash,
            "fees": {"bt_total": str(self.bt_fee), "core_total": str(self.core_fee), "diff": str(fee_diff), "max_per_fill_abs_diff": str(self.max_fill_fee_diff), "core_rates_bps": self.fee_rows},
            "trade_cash_gap_after_removing_fee_difference": str(cash_gap), "funding_booked_by_backtester_not_compared": ({} if funding_report is not None else {v: str(_dec(x)) for v, x in funding_paid.items()}), "funding": funding_report, "margin": margin_report, "equity": equity, "differences": diffs, "mismatches_per_fill": self.mismatches[:20], "ok": not diffs,
        }


class CoreFundingSettler:
    """B2.5: the run's funding, booked by the core FundingEngine instead of funding_settler. Same interface as FundingSettler (settle, next_ts, report, funding_paid, loaded_records), so every consumer in the backtester is unchanged.

    At each call, every rate row whose time has passed (same float comparison as the settler) is handed to the core engine, which settles it against the shadow ledger's position at that time (fills strictly before the
    settlement count) and marks it with the funding record's own mark price where the file has one, otherwise the book mid at the settlement, flagged FUNDING_MARK_FALLBACK and counted in the report. The core's cash flow is applied
    to the backtester's cash. A settlement the core could not book (no rate, no mark) is recorded with payment 0 and source "none", with the core's flag; it is never dropped without a trace."""
    def __init__(self, shadow: CoreShadow) -> None:
        if shadow.funding is None:
            raise ValueError("funding_source core needs funding_rate_dir (the core engine has no rate book otherwise)")
        self.sh = shadow
        shadow.funding_driven = True
        self.funding_paid = 0.0
        self.settlements: list[dict] = []
        self.loaded_records = len(shadow.funding_due)
        self._ptr = 0
        self._applied: set[str] = set()
        self._flags_seen = 0

    def next_ts(self) -> float | None:
        due = self.sh.funding_due
        return due[self._ptr][0] if self._ptr < len(due) else None

    def settle(self, now_s: float, inventory, books) -> None:
        due, sh = self.sh.funding_due, self.sh
        if self._ptr >= len(due) or due[self._ptr][0] > now_s:
            return
        hi = self._ptr
        while hi < len(due) and due[hi][0] <= now_s:
            hi += 1
        until_ns = max(int(round(now_s * 1e9)), max(ns for _, _, ns in due[self._ptr:hi]))
        self._ptr = hi
        sh.funding.settle_until(until_ns, final=True)
        for e in sh.funding.settled_events():
            if e.event_id in self._applied:
                continue
            self._applied.add(e.event_id)
            d = dict(e.details)
            amount = float(e.postings[0].quantity)                     # the holder's cash flow: negative when paid
            venue = e.venue.lower()
            inventory.apply(venue, 0.0, amount)
            self.funding_paid += -amount
            self.settlements.append({"venue": venue, "ts": e.ts_ns / 1e9, "position_base": float(d["position"]), "mark": float(d["mark_price"]),
                                     "mark_source": "record" if "funding record" in e.valuation_source else "book_mid", "rate": float(d["rate"]), "payment": -amount})
        for f in sh.funding.flags[self._flags_seen:]:
            if f["blocking"]:
                self.settlements.append({"venue": f["venue"].lower(), "ts": f["funding_ts_ns"] / 1e9, "position_base": float(f["position"]), "mark": 0.0, "mark_source": "none",
                                         "rate": None, "payment": 0.0, "flag": f["code"]})
        self._flags_seen = len(sh.funding.flags)

    def report(self) -> dict:
        by_venue: dict[str, float] = {}
        for r in self.settlements:
            by_venue[r["venue"]] = by_venue.get(r["venue"], 0.0) + r["payment"]
        core = self.sh.funding.report()
        codes: dict[str, int] = {}
        for f in self.sh.funding.flags:
            codes[f["code"]] = codes.get(f["code"], 0) + 1
        return {"funding_paid": self.funding_paid, "by_venue": by_venue, "loaded_records": self.loaded_records, "contract_multiplier": 1.0, "settlement_currency": "USDT",
                "mark_sources": {k: sum(1 for r in self.settlements if r["mark_source"] == k) for k in ("record", "book_mid", "none")},
                "source": "core crypto_stat_arb.funding.FundingEngine", "mark_rule": "the funding record's mark price where present, otherwise the book mid at the settlement (flagged FUNDING_MARK_FALLBACK, counted)",
                "FUNDING_MARK_FALLBACK": codes.get("FUNDING_MARK_FALLBACK", 0), "core_flags": codes,
                "core_status": {k: core.get(k) for k in ("status", "economic_status", "data_coverage", "mark_basis", "settlements_booked", "settlements_on_book_mid")},
                "settlements": list(self.settlements)}


def _make_core_margin_model():
    from .margin_model import MarginModel

    class CoreMarginModel(MarginModel):
        """B2.6: the run's margin decisions come from the core. Every check evaluates the core RiskEngine margin path (tiers, collateral from the ledger, cross mode, A9 fail-closed above the last tier) on the
        ledger the fills are booked into; a breach is the core's BREACH state; the admission gate is the core's worst-case initial-margin increment against its free margin. What stays in the backtester's
        class is bookkeeping only (counters, events, the flatten after a liquidation, the report) and the execution of a liquidation against the simulator's book (the core's own liquidation engine walks
        a simulated venue adapter the backtester does not have). Refused here, because the core path does not model them: transfers, isolated mode, a utilisation limit. Not provided by the core and reported as None:
        the liquidation price and distance."""
        def __init__(self, cfg, venues, shadow):
            if cfg.transfers is not None:
                raise ValueError("margin_source core: transfers are not modelled on the core path")
            if cfg.max_utilisation is not None:
                raise ValueError("margin_source core: max_utilisation is not a core rule")
            if any(vm.mode != "cross" for vm in cfg.venues.values()):
                raise ValueError("margin_source core: isolated mode is not compared or modelled on the core path (cross only)")
            if shadow.margin is None:
                raise ValueError("margin_source core needs the core accounting object (margin configured)")
            super().__init__(cfg, venues)
            self.sh = shadow
            shadow.margin_driven = True
            self.core = {"checks_skipped_no_mark": 0, "gate_calls": 0, "gate_refusals": 0, "gate_refusals_above_last_tier": 0, "breach_rows": 0, "warning_rows": 0}

        def step(self, now, inventory, books):
            if now - self.last_check < self.cfg.check_every_s:
                return []
            self.last_check = now
            self.stats["checks"] += 1
            marks = {v: self.mark(v, books) for v in self.venues}
            self.last_rows, self.last_rows_ts = {}, now
            if any(x <= 0.0 for x in marks.values()):
                self.core["checks_skipped_no_mark"] += 1
                return []
            from crypto_stat_arb.risk import MarginState
            rows = self.sh.core_margin_rows(now, marks)
            out: list[tuple[str, str, float, float]] = []
            for v in self.venues:
                row = rows[v]
                eq = float(row.effective_margin_balance)
                notional = float(row.gross_notional)
                mm = float(row.maintenance_margin_required)
                self.stats["min_equity"] = min(self.stats["min_equity"], eq)
                if eq > 0 and row.initial_margin_required.is_finite():
                    self.stats["peak_initial_margin_usage"] = max(self.stats["peak_initial_margin_usage"], float(row.initial_margin_required) / eq)
                if notional > 0 and mm > 0 and mm != float("inf"):
                    self.stats["min_margin_ratio"] = min(self.stats["min_margin_ratio"], eq / mm)
                self.core["warning_rows"] += row.state == MarginState.WARNING
                breach = row.state == MarginState.BREACH and notional > 0
                if not breach:
                    continue
                self.core["breach_rows"] += 1
                if mm == float("inf"):
                    self.stats["above_last_tier_breaches"] = self.stats.get("above_last_tier_breaches", 0) + 1
                if not self.cfg.liquidate:
                    continue
                vm = self.cfg.venues[v]
                qty = abs(inventory.base.get(v, 0.0))
                side = "sell" if inventory.base.get(v, 0.0) > 0 else "buy"
                b = books.get(v)
                depth = sum(sz for _, sz in b.levels("bid" if side == "sell" else "ask", 50)) if b is not None and b.ready else 0.0
                adl = depth < qty
                if adl:
                    self.stats["adl_exposure_events"] += 1
                self.events.append({"ts": now, "type": "liquidation", "venue": v, "mode": vm.mode, "equity": eq, "maintenance": mm, "qty": qty, "side": side, "adl_exposure": adl,
                                    "book_depth": depth, "liquidation_price": None, "core_alerts": list(row.alert_codes)})
                out.append((v, side, qty, vm.liquidation_fee_bps))
                if self.cfg.flatten_other_venue:
                    for o in self.venues:
                        if o != v and inventory.base.get(o, 0.0) != 0.0:
                            out.append((o, "sell" if inventory.base.get(o, 0.0) > 0 else "buy", min(qty, abs(inventory.base.get(o, 0.0))), -1.0))
            return out

        def allows(self, signal, inventory, books, reserved=None):
            if not self.cfg.gate_new_signals:
                return True
            marks = {v: self.mark(v, books) for v in self.venues}
            for venue, sign in ((signal.buy_venue, 1.0), (signal.sell_venue, -1.0)):
                if venue not in self.cfg.venues:
                    continue
                if marks.get(venue, 0.0) <= 0 or any(x <= 0 for x in marks.values()):
                    continue
                res = (reserved or {}).get(venue, 0.0) if self.cfg.venues[venue].reserve_open_orders else 0.0
                self.core["gate_calls"] += 1
                ok, why = self.sh.core_margin_admits(venue, signal.qty, marks, res / marks[venue])
                if not ok:
                    self.core["gate_refusals"] += 1
                    self.stats["signals_blocked_by_margin"] += 1
                    if "above the last risk tier" in why:
                        self.core["gate_refusals_above_last_tier"] += 1
                        self.stats["tier_exceeded"] += 1
                    return False
            return True

        def report(self, inventory, books):
            r = super().report(inventory, books)
            r["margin_source"] = "core crypto_stat_arb RiskEngine (tiers, ledger collateral, cross mode, A9 fail-closed, worst-case admission)"
            r["core_path"] = dict(self.core, not_provided_by_core=["liquidation price", "liquidation distance"], not_modelled=["transfers", "isolated mode", "utilisation limit", "liquidation fee in the core ledger (the fee rides on the backtester's fill)"])
            r["min_liquidation_distance"] = {v: None for v in self.venues}
            r["liquidation_price_by_venue"] = {v: None for v in self.venues}
            return r

    return CoreMarginModel
