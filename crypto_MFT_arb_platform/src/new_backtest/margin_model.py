"""Opt-in margin, liquidation and cross-venue equity-transfer model for CrossVenueArbBacktester.

Off (ArbBacktestConfig.margin = None, the default) the replay is exactly as before: infinite margin, no transfers. On, each venue
has its own collateral account:

    equity(v) = initial_capital(v) + cash(v) + base(v) * mark(v)        mark = the venue's book mid (no mark-price stream in the data)

and the venue's position notional |base| * mark is placed in a risk tier (ASSUMED numbers, declared per run) that gives the initial
and maintenance margin. Three effects, each reported:

  * gate      a new signal is refused when, after its clip, either venue's equity would be below that venue's initial margin
              (reject counter signals_blocked_by_margin);
  * liquidate when equity(v) < maintenance(v) the venue closes the position at market (walking its own book), charges a
              liquidation fee, and, with flatten_other_venue, the backtester flattens the matching quantity on the other venue so
              the hedge does not become a naked position. Liquidation cost is reported on its own line, never mixed into gross P&L;
  * transfer  with TransferModelConfig, a venue whose FREE equity (equity - initial margin in use) drops below low_watermark
              requests a top-up from the other venue over a route with a delay, a fee and a minimum size. The sending venue's cash
              leaves at initiation, the receiving venue's cash arrives at confirmation: that latency is what the model is for.

Mirrors crypto_stat_arb.margin / transfers (tiers, cross-margin mode, flatten-after-liquidation, routes with delay and
watermark rebalancing) in the backtester's float world, without importing the live package.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MarginTier:
    max_notional: float
    initial_rate: float
    maintenance_rate: float
    maintenance_deduction: float = 0.0


@dataclass(frozen=True)
class VenueMargin:
    initial_capital: float                      # quote currency at the start of the run
    tiers: tuple[MarginTier, ...]
    liquidation_fee_bps: float = 0.0            # on liquidated notional
    source: str = "ASSUMED"
    # WP5: "cross" (default: the whole venue account backs the position) or "isolated" (only the initial margin the position
    # carries backs it: liquidation when the unrealised loss exceeds initial - maintenance, whatever else sits in the account).
    mode: str = "cross"
    # WP5: initial margin reserved for RESTING orders (notional x initial rate of the tier the resulting position would be in).
    reserve_open_orders: bool = True

    def __post_init__(self) -> None:
        if self.mode not in ("cross", "isolated"):
            raise ValueError("VenueMargin.mode must be 'cross' or 'isolated'")

    def tier(self, notional: float) -> MarginTier | None:
        for t in self.tiers:
            if notional <= t.max_notional:
                return t
        return None

    def initial(self, notional: float) -> float | None:
        t = self.tier(notional)
        return None if t is None else notional * t.initial_rate

    def maintenance(self, notional: float) -> float | None:
        t = self.tier(notional)
        return None if t is None else max(0.0, notional * t.maintenance_rate - t.maintenance_deduction)


@dataclass(frozen=True)
class TransferRoute:
    source: str
    destination: str
    delay_s: float
    fee: float = 0.0                            # quote currency, charged to the source at initiation
    min_quantity: float = 0.0


@dataclass(frozen=True)
class TransferModelConfig:
    routes: tuple[TransferRoute, ...]
    low_watermark: float                        # free equity below which a venue asks for a top-up
    target: float                               # free equity the top-up aims for
    cooldown_s: float = 60.0


@dataclass(frozen=True)
class MarginModelConfig:
    venues: dict[str, VenueMargin]
    check_every_s: float = 1.0
    gate_new_signals: bool = True
    liquidate: bool = True
    flatten_other_venue: bool = True
    transfers: TransferModelConfig | None = None
    # WP6: per-venue margin-utilisation limit (initial margin after the clip / equity); None = off.
    max_utilisation: float | None = None
    # A9 (Phase B): what a position above the venue's LAST margin tier means. "ignore" (v1, v1.1): no maintenance figure exists, so it is never liquidated. "breach" (v1_2, the
    # core rule, POSITION_ABOVE_RISK_LIMIT, fail closed): it counts as a margin breach and is liquidated like one.
    above_last_tier: str = "ignore"

    def __post_init__(self) -> None:
        if self.above_last_tier not in ("ignore", "breach"):
            raise ValueError("MarginModelConfig.above_last_tier must be 'ignore' or 'breach'")


@dataclass
class PendingTransfer:
    route: TransferRoute
    amount: float
    initiated_ts: float
    confirm_ts: float
    reason: str


class MarginModel:
    def __init__(self, cfg: MarginModelConfig, venues: tuple[str, ...]):
        for v in venues:
            if v not in cfg.venues:
                raise ValueError(f"margin: no VenueMargin for venue {v!r}")
            vm = cfg.venues[v]
            if not (vm.initial_capital > 0.0):
                raise ValueError(f"margin: {v} initial_capital must be > 0")
            bounds = [t.max_notional for t in vm.tiers]
            if not bounds or bounds != sorted(bounds) or len(set(bounds)) != len(bounds):
                raise ValueError(f"margin: {v} needs strictly increasing tier bounds")
            for t in vm.tiers:
                if not (0.0 < t.maintenance_rate <= t.initial_rate <= 1.0):
                    raise ValueError(f"margin: {v} tier rates must satisfy 0 < maintenance <= initial <= 1")
        if not (cfg.check_every_s > 0.0):
            raise ValueError("margin: check_every_s must be > 0")
        tr = cfg.transfers
        if tr is not None:
            if not tr.routes:
                raise ValueError("transfers: at least one route")
            for r in tr.routes:
                if r.source == r.destination or r.source not in venues or r.destination not in venues:
                    raise ValueError(f"transfers: bad route {r.source}->{r.destination}")
                if not (r.delay_s > 0.0):
                    raise ValueError("transfers: delay_s must be > 0 (a transfer is never instantaneous)")
            if not (0.0 <= tr.low_watermark < tr.target):
                raise ValueError("transfers: need 0 <= low_watermark < target")
        self.cfg, self.venues = cfg, tuple(venues)
        self.last_check = -float("inf")
        self.transfer_cash: dict[str, float] = {v: 0.0 for v in venues}     # net effect of transfers on each venue's cash
        self.pending: list[PendingTransfer] = []
        self.last_initiated: dict[tuple[str, str], float] = {}
        self.events: list[dict] = []
        self.stats = {"checks": 0, "min_margin_ratio": float("inf"), "peak_initial_margin_usage": 0.0, "min_equity": float("inf"),
                      "liquidations": 0, "liquidated_qty": 0.0, "liquidation_fees": 0.0, "flattened_after_liquidation": 0,
                      "signals_blocked_by_margin": 0, "signals_blocked_by_reserved_margin": 0, "transfers_initiated": 0,
                      "transfers_completed": 0, "transfer_fees": 0.0, "transferred_amount": 0.0, "seconds_below_watermark": 0.0,
                      "tier_exceeded": 0, "adl_exposure_events": 0, "isolated_liquidations": 0}
        self.entry_cost: dict[str, float] = {v: 0.0 for v in venues}        # WP5 isolated mode: cost basis of the open position
        # r12: closest approach to liquidation per venue over the run: |mark - liquidation price| / mark in bps, with the time, mark,
        # position and equity at that moment. None until the venue has held a position at a check.
        self.liq_distance: dict[str, dict | None] = {v: None for v in venues}
        self._below_since: dict[str, float | None] = {v: None for v in venues}
        # B2.3: what the last periodic check saw and decided, per venue, kept as plain numbers so the core accounting shadow can compare against it. Written, never read, by the model.
        self.last_rows: dict[str, dict] = {}
        self.last_rows_ts: float | None = None

    # ---- accounting -------------------------------------------------------------------------------------------
    def mark(self, venue: str, books) -> float:
        b = books.get(venue)
        return b.mid if (b is not None and b.ready and b.mid > 0) else 0.0

    def equity(self, venue: str, inventory, books) -> float:
        return (self.cfg.venues[venue].initial_capital + inventory.cash.get(venue, 0.0) + self.transfer_cash[venue]
                + inventory.base.get(venue, 0.0) * self.mark(venue, books))

    def notional(self, venue: str, inventory, books) -> float:
        return abs(inventory.base.get(venue, 0.0)) * self.mark(venue, books)

    def free_equity(self, venue: str, inventory, books, reserved_notional: float = 0.0) -> float:
        im = self.cfg.venues[venue].initial(self.notional(venue, inventory, books) + reserved_notional)
        return self.equity(venue, inventory, books) - (im if im is not None else float("inf"))

    # ---- WP5 additions ----------------------------------------------------------------------------------------
    def unrealised(self, venue: str, inventory, books) -> float:
        """Unrealised P&L of the open position at the venue mark (position x mark - cost basis of the ledger's position)."""
        base = inventory.base.get(venue, 0.0)
        if base == 0.0:
            return 0.0
        return base * self.mark(venue, books) - self.entry_cost.get(venue, 0.0)      # entry_cost = cash spent (long) / received (short, negative)

    def liquidation_price(self, venue: str, inventory, books) -> float | None:
        """Cross: the mark at which equity(v) == maintenance(v) (None when the position cannot be liquidated by price alone, e.g.
        fully funded, or flat). Isolated: the mark at which the unrealised loss equals initial - maintenance of the position."""
        base = inventory.base.get(venue, 0.0)
        mark = self.mark(venue, books)
        if base == 0.0 or mark <= 0.0:
            return None
        vm = self.cfg.venues[venue]
        t = vm.tier(abs(base) * mark)
        if t is None:
            return None
        r, d = t.maintenance_rate, t.maintenance_deduction
        if vm.mode == "isolated":
            im = vm.initial(abs(base) * mark)
            buffer = (im or 0.0) - max(0.0, abs(base) * mark * r - d)
            p = mark - buffer / base                       # long: price falls by buffer/|base|; short: rises
            return p if p > 0 else None
        k = vm.initial_capital + inventory.cash.get(venue, 0.0) + self.transfer_cash[venue] + d
        den = base - r * abs(base)
        if den == 0.0:
            return None
        p = -k / den
        return p if p > 0 else None

    def _breaches(self, venue: str, inventory, books) -> tuple[bool, float, float]:
        """(liquidate?, equity-like figure, maintenance) under the venue's margin mode."""
        vm = self.cfg.venues[venue]
        notional = self.notional(venue, inventory, books)
        mm = vm.maintenance(notional)
        if vm.mode == "isolated":
            im = vm.initial(notional)
            if mm is None or im is None or notional <= 0:
                return False, 0.0, 0.0
            backing = im + self.unrealised(venue, inventory, books)   # only the position's own margin plus its P&L
            return backing < mm, backing, mm
        eq = self.equity(venue, inventory, books)
        return (notional > 0 and mm is not None and mm > 0 and eq < mm), eq, (mm or 0.0)

    def note_fill(self, venue: str, signed_qty: float, price: float, inventory) -> None:
        """Track the cost basis for the isolated mode (called by the backtester on every fill)."""
        if venue not in self.cfg.venues:
            return
        base_after = inventory.base.get(venue, 0.0)
        base_before = base_after - signed_qty
        if base_before == 0.0 or (base_before > 0) != (base_after > 0):
            self.entry_cost[venue] = -inventory.cash.get(venue, 0.0) if base_after != 0.0 else 0.0

    # ---- the gate -----------------------------------------------------------------------------------------------
    def allows(self, signal, inventory, books, reserved: dict[str, float] | None = None) -> bool:
        """False when either leg's venue could not post initial margin for its position after this clip. `reserved` (WP5):
        notional of resting orders per venue whose initial margin is already spoken for (VenueMargin.reserve_open_orders)."""
        if not self.cfg.gate_new_signals:
            return True
        for venue, sign in ((signal.buy_venue, 1.0), (signal.sell_venue, -1.0)):
            if venue not in self.cfg.venues:
                continue                                                   # WP7b: a spot leg has no margin account here
            mark = self.mark(venue, books)
            if mark <= 0:
                continue
            res = (reserved or {}).get(venue, 0.0) if self.cfg.venues[venue].reserve_open_orders else 0.0
            if res > 0.0:
                im_res = self.cfg.venues[venue].initial(self.notional(venue, inventory, books) + res + signal.qty * mark)
                if im_res is None or self.equity(venue, inventory, books) < im_res:
                    self.stats["signals_blocked_by_reserved_margin"] += 1
                    self.stats["signals_blocked_by_margin"] += 1
                    return False
            new_notional = abs(inventory.base.get(venue, 0.0) + sign * signal.qty) * mark
            if self.cfg.max_utilisation is not None:
                im_after = self.cfg.venues[venue].initial(new_notional + res)
                eq = self.equity(venue, inventory, books)
                if im_after is None or eq <= 0.0 or im_after / eq > self.cfg.max_utilisation:
                    self.stats["signals_blocked_by_utilisation"] = self.stats.get("signals_blocked_by_utilisation", 0) + 1
                    self.stats["signals_blocked_by_margin"] += 1
                    return False
            im = self.cfg.venues[venue].initial(new_notional)
            if im is None:                                             # above the last tier: the venue would not accept it
                self.stats["tier_exceeded"] += 1
                self.stats["signals_blocked_by_margin"] += 1
                return False
            if self.equity(venue, inventory, books) < im:
                self.stats["signals_blocked_by_margin"] += 1
                return False
        return True

    # ---- periodic step ------------------------------------------------------------------------------------------
    def step(self, now: float, inventory, books) -> list[tuple[str, str, float, float]]:
        """Every check_every_s: settle transfers, check maintenance, request rebalances. Returns liquidation instructions
        [(venue, side, qty, fee_bps)] for the backtester to execute against the venue's book; the matching flatten on the other
        venue is returned too when configured."""
        if now - self.last_check < self.cfg.check_every_s:
            return []
        self.last_check = now
        self.stats["checks"] += 1
        self._settle_transfers(now)
        out: list[tuple[str, str, float, float]] = []
        rows: dict[str, dict] = {}
        for v in self.venues:
            eq = self.equity(v, inventory, books)
            notional = self.notional(v, inventory, books)
            self.stats["min_equity"] = min(self.stats["min_equity"], eq)
            vm = self.cfg.venues[v]
            im = vm.initial(notional)
            if im is not None and eq > 0:
                self.stats["peak_initial_margin_usage"] = max(self.stats["peak_initial_margin_usage"], im / eq)
            mm = vm.maintenance(notional)
            if notional > 0:
                lp = self.liquidation_price(v, inventory, books)
                mk = self.mark(v, books)
                if lp is not None and mk > 0:
                    dist = abs(mk - lp) / mk * 1e4
                    cur = self.liq_distance[v]
                    if cur is None or dist < cur["min_bps"]:
                        self.liq_distance[v] = {"min_bps": dist, "ts": now, "mark": mk, "liquidation_price": lp,
                                                "position_base": inventory.base.get(v, 0.0), "equity": eq}
            breach, backing, mm_eff = self._breaches(v, inventory, books)
            over_tier = notional > 0 and mm is None and self.cfg.above_last_tier == "breach"     # A9: above the last tier is a breach under the core rule
            if over_tier:
                breach, backing, mm_eff = True, eq, 0.0
                self.stats["above_last_tier_breaches"] = self.stats.get("above_last_tier_breaches", 0) + 1
            rows[v] = {"equity": eq, "transfer_cash": self.transfer_cash[v], "notional": notional, "mark": self.mark(v, books), "base": inventory.base.get(v, 0.0),
                       "maintenance": mm, "initial": im, "breach": bool(breach and notional > 0 and (over_tier or (mm is not None and mm > 0))), "over_tier": over_tier}
            if notional > 0 and ((mm is not None and mm > 0) or over_tier):
                if not over_tier:
                    self.stats["min_margin_ratio"] = min(self.stats["min_margin_ratio"], backing / mm_eff if mm_eff > 0 else float("inf"))
                if breach and self.cfg.liquidate:
                    qty = abs(inventory.base.get(v, 0.0))
                    side = "sell" if inventory.base.get(v, 0.0) > 0 else "buy"
                    # ADL exposure: the venue's own book cannot absorb the liquidation within the captured depth
                    b = books.get(v)
                    depth = sum(sz for _, sz in b.levels("bid" if side == "sell" else "ask", 50)) if b is not None and b.ready else 0.0
                    adl = depth < qty
                    if adl:
                        self.stats["adl_exposure_events"] += 1
                    if vm.mode == "isolated":
                        self.stats["isolated_liquidations"] += 1
                    self.events.append({"ts": now, "type": "liquidation", "venue": v, "mode": vm.mode, "equity": backing, "maintenance": mm_eff,
                                        "qty": qty, "side": side, "adl_exposure": adl, "book_depth": depth,
                                        "liquidation_price": self.liquidation_price(v, inventory, books)})
                    out.append((v, side, qty, vm.liquidation_fee_bps))
                    if self.cfg.flatten_other_venue:
                        for o in self.venues:
                            if o != v and inventory.base.get(o, 0.0) != 0.0:
                                oq = min(qty, abs(inventory.base.get(o, 0.0)))
                                out.append((o, "sell" if inventory.base.get(o, 0.0) > 0 else "buy", oq, -1.0))   # -1: a flatten, no liquidation fee
        self.last_rows, self.last_rows_ts = rows, now
        if self.cfg.transfers is not None:
            self._rebalance(now, inventory, books)
        return out

    def record_liquidation(self, venue: str, qty: float, fee: float, flatten: bool) -> None:
        if flatten:
            self.stats["flattened_after_liquidation"] += 1
        else:
            self.stats["liquidations"] += 1
            self.stats["liquidated_qty"] += qty
            self.stats["liquidation_fees"] += fee

    # ---- transfers ------------------------------------------------------------------------------------------------
    def _settle_transfers(self, now: float) -> None:
        keep = []
        for p in self.pending:
            if p.confirm_ts <= now:
                self.transfer_cash[p.route.destination] += p.amount
                self.stats["transfers_completed"] += 1
                self.events.append({"ts": now, "type": "transfer_confirmed", "from": p.route.source, "to": p.route.destination, "amount": p.amount})
            else:
                keep.append(p)
        self.pending = keep

    def _rebalance(self, now: float, inventory, books) -> None:
        tr = self.cfg.transfers
        for v in self.venues:
            actual = self.free_equity(v, inventory, books)
            # The time below the watermark is measured on the money that is really there (that is the exposure the latency
            # creates); the request decision counts funds already in transit so one shortfall is not requested twice.
            if actual >= tr.low_watermark:
                if self._below_since[v] is not None:
                    self.stats["seconds_below_watermark"] += now - self._below_since[v]
                    self._below_since[v] = None
            elif self._below_since[v] is None:
                self._below_since[v] = now
            free = actual + sum(p.amount for p in self.pending if p.route.destination == v)
            if free >= tr.low_watermark:
                continue
            options = []
            for r in tr.routes:
                if r.destination != v or any(p.route == r for p in self.pending):
                    continue
                last = self.last_initiated.get((r.source, r.destination))
                if last is not None and now - last < tr.cooldown_s:
                    continue
                spare = self.free_equity(r.source, inventory, books) - r.fee - tr.low_watermark
                amount = min(tr.target - free, spare)
                if amount > 0 and amount >= r.min_quantity:
                    options.append((spare, r, amount))
            if options:
                _, r, amount = max(options, key=lambda x: x[0])
                self.transfer_cash[r.source] -= amount + r.fee
                self.stats["transfer_fees"] += r.fee
                self.stats["transferred_amount"] += amount
                self.stats["transfers_initiated"] += 1
                self.last_initiated[(r.source, r.destination)] = now
                self.pending.append(PendingTransfer(r, amount, now, now + r.delay_s, f"{v} free {free:.2f} < watermark {tr.low_watermark}"))
                self.events.append({"ts": now, "type": "transfer_initiated", "from": r.source, "to": v, "amount": amount, "fee": r.fee, "arrives": now + r.delay_s})

    def finish(self, now: float) -> None:
        for v, since in self._below_since.items():
            if since is not None:
                self.stats["seconds_below_watermark"] += now - since
                self._below_since[v] = None

    def report(self, inventory, books) -> dict:
        eq = {v: self.equity(v, inventory, books) for v in self.venues}
        s = dict(self.stats)
        if s["min_margin_ratio"] == float("inf"):
            s["min_margin_ratio"] = None
        if s["min_equity"] == float("inf"):
            s["min_equity"] = None
        return {"enabled": True, "sources": {v: self.cfg.venues[v].source for v in self.venues},
                "modes": {v: self.cfg.venues[v].mode for v in self.venues},
                "min_liquidation_distance": dict(self.liq_distance),
                "blocked_signals_by_reason": {"initial_margin_or_equity": self.stats["signals_blocked_by_margin"]
                                              - self.stats["signals_blocked_by_reserved_margin"] - self.stats.get("signals_blocked_by_utilisation", 0),
                                              "reserved_for_resting_orders": self.stats["signals_blocked_by_reserved_margin"],
                                              "utilisation_limit": self.stats.get("signals_blocked_by_utilisation", 0),
                                              "tier_exceeded": self.stats["tier_exceeded"]},
                "liquidation_price_by_venue": {v: self.liquidation_price(v, inventory, books) for v in self.venues},
                "unrealised_by_venue": {v: self.unrealised(v, inventory, books) for v in self.venues},
                "initial_capital": {v: self.cfg.venues[v].initial_capital for v in self.venues},
                "ending_equity_by_venue": eq, "transfer_cash_by_venue": dict(self.transfer_cash), "pending_transfers": len(self.pending),
                "events": self.events[-200:], **s}
