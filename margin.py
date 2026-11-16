"""Venue margin: risk tiers, collateral haircuts, cross/isolated modes, liquidation (ECO04).

This extends ``RiskEngine.evaluate_margin`` for venues with a ``risk.margin``
configuration. Venues without one keep the fixed-rate path.

* **Risk tiers per position.** Each instrument's position notional selects
  its tier. Maintenance = notional x mmr - deduction (Binance ``cum``, Bybit
  MM deduction); initial = notional x imr - initial deduction. A position
  above the last tier is refused at admission and flagged
  ``POSITION_ABOVE_RISK_LIMIT``. Tiers come from config: the public contract
  master has flat rates only, so tiers are ``ASSUMED`` unless marked
  VERIFIED.
* **Collateral per venue, from the economic ledger.** The margin balance is
  computed from the venue's asset balances at marks:
  - eligible collateral counts at price x (1 - haircut); an asset that isn't
    listed is ineligible and counts at 0;
  - the perpetual contract asset counts at mark x multiplier (position
    value, never haircut);
  - every negative balance (loans, interest, and the USDT that the
    fully-funded perp representation spends) counts at full value.

  With no collateral list, only the settlement/quote asset is eligible, at
  0 haircut. Collateral is never shared between venues.
* **Cross vs isolated.** Cross: every position on the venue shares the
  venue's collateral. Isolated: a position's margin is its configured
  allocation (``isolated_margin``, labelled ASSUMED) plus its unrealized PnL.
  It breaches on its own, and the cross wallet cannot rescue it.
* **Exposure for initial margin** is the worst case: the position plus
  reserved package legs plus open hedge/unwind orders, each in the
  direction that increases it.
* **Venue-reported margin.** The lower of local and venue-reported is used.
  A venue figure lower than local by more than the tolerance raises
  ``VENUE_MARGIN_BELOW_LOCAL``.
* **Liquidation (simulated, simplified).**
  - Trigger: a venue breach (cross) or an isolated-position breach, marked
    at the book mid (an approximation of the venue mark).
  - Execution: the position is closed by walking the opposite side of the
    book, through the simulated venue's liquidity consumption. A
    liquidation fee is charged on the notional; unfilled quantity stays as
    ``LIQUIDATION_RESIDUAL`` and is retried at most once per book update.
  - Bankruptcy: equity below zero afterwards is flagged
    ``BANKRUPTCY_DEFICIT``.
  - Simplifications: a full close rather than the venues' stepwise partial
    liquidation, and a book walk rather than takeover at the bankruptcy
    price. Insurance fund and ADL are UNSUPPORTED.
  - Spot-margin liquidation is UNSUPPORTED (only the borrow-recall buy-in
    exists, see borrow.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable

from .economics import EconomicEventType
from .models import ChildOrder, OrderState, Side

if TYPE_CHECKING:
    from .ledger import Ledger

ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class RiskTier:
    max_notional: Decimal
    maintenance_rate: Decimal
    initial_rate: Decimal
    maintenance_deduction: Decimal = ZERO
    initial_deduction: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class TierSchedule:
    tiers: tuple[RiskTier, ...]
    source: str = "ASSUMED"

    def __post_init__(self) -> None:
        bounds = [t.max_notional for t in self.tiers]
        if not bounds or bounds != sorted(bounds) or len(set(bounds)) != len(bounds):
            raise ValueError("risk tiers need strictly increasing max_notional")

    def tier(self, notional: Decimal) -> tuple[int, RiskTier] | None:
        """The tier for an absolute notional; None above the last tier."""
        for index, tier in enumerate(self.tiers):
            if notional <= tier.max_notional:
                return index, tier
        return None

    def maintenance(self, notional: Decimal) -> Decimal | None:
        found = self.tier(notional)
        return None if found is None else max(notional * found[1].maintenance_rate - found[1].maintenance_deduction, ZERO)

    def initial(self, notional: Decimal) -> Decimal | None:
        found = self.tier(notional)
        return None if found is None else max(notional * found[1].initial_rate - found[1].initial_deduction, ZERO)


def liq_price_at(
    schedule: "TierSchedule",
    is_long: bool,
    qty: Decimal,
    entry_price: Decimal,
    available_margin: Decimal,
) -> Decimal | None:
    """Estimated liquidation price for a perpetual position increment.

    Uses the tier at the entry notional (approximation; tier may shift at liquidation).
    Returns None when the notional is above the last risk tier.

    Long  formula: liq = (margin - qty*entry + deduction) / (qty * (mmr - 1))
    Short formula: liq = (margin + qty*entry + deduction) / (qty * (mmr + 1))

    Both derived from: equity_at_liq = maintenance_margin_at_liq, where
      equity = available_margin + unrealised_pnl  and  MM = mmr * qty * liq - deduction.
    """
    notional = qty * entry_price
    found = schedule.tier(notional)
    if found is None:
        return None
    _, tier = found
    mmr = tier.maintenance_rate
    deduction = tier.maintenance_deduction
    if is_long:
        denom = qty * (mmr - ONE)      # negative (mmr < 1)
        if denom == ZERO:
            return None
        return (available_margin - qty * entry_price + deduction) / denom
    else:
        denom = qty * (mmr + ONE)
        if denom == ZERO:
            return None
        return (available_margin + qty * entry_price + deduction) / denom


def liq_distance_bps(liq_price: Decimal, entry_price: Decimal, is_long: bool) -> Decimal:
    """Signed-positive distance from entry to liquidation price in basis points."""
    if is_long:
        return (entry_price - liq_price) / entry_price * Decimal("10000")
    return (liq_price - entry_price) / entry_price * Decimal("10000")


@dataclass(frozen=True, slots=True)
class LiquidationPolicy:
    enabled: bool = False
    fee_rate: Decimal = ZERO          # on liquidated notional, charged in quote
    source: str = "ASSUMED"


@dataclass(frozen=True, slots=True)
class VenueMarginConfig:
    venue: str
    mode: str = "CROSS"                                           # CROSS | ISOLATED
    tiers: dict[str, TierSchedule] = field(default_factory=dict)  # instrument -> schedule
    collateral: dict[str, Decimal] | None = None                  # asset -> haircut; None = quote only
    isolated_margin: dict[str, Decimal] = field(default_factory=dict)
    liquidation: LiquidationPolicy = LiquidationPolicy()
    reported_tolerance: Decimal = Decimal("0.01")

    def haircut(self, asset: str, quote: str) -> Decimal:
        """1 means ineligible."""
        if self.collateral is None:
            return ZERO if asset == quote else ONE
        return self.collateral.get(asset, ONE)


def _missing_price_action(value: Any) -> str:
    value = str(value).lower()
    if value not in ("breach", "pause"):
        raise ValueError("margin.missing_price_action must be 'breach' or 'pause'")
    return value


def _mark_stamp(value: Any) -> str:
    if value not in ("restamp", "own_timestamp"):
        raise ValueError(f"margin.mark_stamp must be restamp or own_timestamp, got {value!r}")
    return str(value)


@dataclass(frozen=True, slots=True)
class MarginConfig:
    venues: dict[str, VenueMarginConfig] = field(default_factory=dict)
    evaluate_interval_ns: int = 1_000_000_000
    # After a liquidation closes one leg of a cross-venue hedge, flatten the
    # matching quantity of the opposite position on the other venue.
    flatten_after_liquidation: bool = True
    # Owner 11 Nov: a mark (exchange mark preferred, else book mid) counts for this long after it was last good (ASSUMED 5 s); after that the price is missing.
    mark_max_age_ns: int = 5_000_000_000
    # What a missing price does: "breach" (the old rule: fail closed, margin BREACH, kill switch) or "pause" (new risk is paused until a fresh price returns; only a real shortfall with a fresh price is a BREACH).
    missing_price_action: str = "breach"
    # Owner 16 Nov (the 5 s mark rule): "restamp" (every earlier profile, labelled LEGACY) stamps a mark with the time the evaluation saw it, so a mark lives up to about 2 x mark_max_age (9-10 s measured);
    # "own_timestamp" (v1_10 onward) keeps the mark's own time: a mark older than mark_max_age by its OWN timestamp is missing (age exactly mark_max_age still counts), and nothing is re-stamped.
    mark_stamp: str = "restamp"

    @classmethod
    def from_config(cls, section: dict[str, Any] | None) -> "MarginConfig":
        section = section or {}
        venues = {}
        for venue, row in (section.get("venues") or {}).items():
            mode = str(row.get("mode", "CROSS")).upper()
            if mode not in ("CROSS", "ISOLATED"):
                raise ValueError(f"margin mode for {venue} must be CROSS or ISOLATED")
            tiers = {}
            for instrument, items in (row.get("tiers") or {}).items():
                rows = items["tiers"] if isinstance(items, dict) else items
                tiers[instrument] = TierSchedule(tuple(
                    RiskTier(
                        max_notional=Decimal(str(t["max_notional"])),
                        maintenance_rate=Decimal(str(t["maintenance_rate"])),
                        initial_rate=Decimal(str(t["initial_rate"])),
                        maintenance_deduction=Decimal(str(t.get("maintenance_deduction", "0"))),
                        initial_deduction=Decimal(str(t.get("initial_deduction", "0"))),
                    ) for t in rows
                ), source=str(items.get("source", "ASSUMED")).upper() if isinstance(items, dict) else "ASSUMED")
            if len(tiers) > 1:
                # Reserved package exposure is tracked per venue, not per instrument.
                raise ValueError(f"margin tiers for {venue}: one tiered instrument per venue is supported")
            collateral = row.get("collateral")
            isolated = {str(i): Decimal(str(q)) for i, q in (row.get("isolated_margin") or {}).items()}
            if mode == "ISOLATED":
                missing = sorted(set(tiers) - set(isolated))
                if missing:
                    raise ValueError(f"isolated margin on {venue} needs isolated_margin for {missing}")
            liquidation = row.get("liquidation") or {}
            venues[str(venue)] = VenueMarginConfig(
                venue=str(venue), mode=mode, tiers=tiers,
                collateral=None if collateral is None else {
                    str(c["asset"]): Decimal(str(c.get("haircut", "0"))) for c in collateral
                    if c.get("eligible", True)
                },
                isolated_margin=isolated,
                liquidation=LiquidationPolicy(
                    enabled=bool(liquidation.get("enabled", False)),
                    fee_rate=Decimal(str(liquidation.get("fee_rate", "0"))),
                    source=str(liquidation.get("source", "ASSUMED")).upper(),
                ),
                reported_tolerance=Decimal(str(row.get("reported_tolerance", "0.01"))),
            )
            for asset, haircut in (venues[str(venue)].collateral or {}).items():
                if not ZERO <= haircut <= ONE:
                    raise ValueError(f"collateral haircut for {venue} {asset} must be within [0, 1]")
        return cls(venues=venues,
                   evaluate_interval_ns=int(section.get("evaluate_interval_ms", 1_000)) * 1_000_000,
                   flatten_after_liquidation=bool(section.get("flatten_after_liquidation", True)),
                   mark_max_age_ns=int(float(section.get("mark_max_age_s", 5.0)) * 1_000_000_000),
                   missing_price_action=_missing_price_action(section.get("missing_price_action", "breach")),
                   mark_stamp=_mark_stamp(section.get("mark_stamp", "restamp")))

    @property
    def configured(self) -> bool:
        return bool(self.venues)


def collateral_value(
    balances: dict[tuple[str, str, str], Decimal], venue: str, config: VenueMarginConfig, *,
    quote: str, prices: dict[str, Decimal], contract_assets: dict[str, Decimal],
) -> tuple[Decimal, list[dict[str, str]], list[str]]:
    """(margin balance, breakdown, alerts) from a venue's economic balances.

    ``prices`` values non-contract assets in the quote; ``contract_assets`` maps
    a perp contract asset to its mark x multiplier.
    """
    total = ZERO
    rows: list[dict[str, str]] = []
    alerts: list[str] = []
    per_asset: dict[str, Decimal] = {}
    for (v, _account, asset), quantity in balances.items():
        if v == venue and quantity:
            per_asset[asset] = per_asset.get(asset, ZERO) + quantity
    for asset, quantity in sorted(per_asset.items()):
        if not quantity:
            continue
        if asset in contract_assets:
            value, haircut, kind = quantity * contract_assets[asset], ZERO, "POSITION"
        else:
            price = ONE if asset == quote else prices.get(asset)
            if price is None:
                if quantity < 0:
                    alerts.append(f"COLLATERAL_PRICE_MISSING:{asset}")   # an unpriced liability: fail closed
                    value, haircut, kind = ZERO, ZERO, "UNPRICED_LIABILITY"
                else:
                    alerts.append(f"COLLATERAL_PRICE_MISSING:{asset}")
                    value, haircut, kind = ZERO, ONE, "UNPRICED"
            elif quantity < 0:
                value, haircut, kind = quantity * price, ZERO, "LIABILITY"   # never haircut
            else:
                haircut = config.haircut(asset, quote)
                value, kind = quantity * price * (ONE - haircut), "COLLATERAL" if haircut < 1 else "INELIGIBLE"
        total += value
        rows.append({"asset": asset, "quantity": str(quantity), "kind": kind,
                     "haircut": str(haircut), "value": str(value)})
    return total, rows, alerts


@dataclass(slots=True)
class LiquidationEngine:
    """Simulated venue liquidation of breached perp positions (see module docstring)."""

    ledger: "Ledger"
    config: MarginConfig
    adapter_for: Callable[[str], Any]
    on_liquidation: Callable[[str, int], None] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    # Positions under liquidation: the venue keeps closing them on each book
    # update until flat, even if margin recovers in between.
    active: set[tuple[str, str]] = field(default_factory=set)
    _attempted: dict[tuple[str, str], tuple[int, int]] = field(default_factory=dict)
    _sequence: int = 0

    def restore(self, ledger: "Ledger") -> None:
        """After a restart: liquidation history from the (journaled) LIQUIDATION fills,
        and a position with a liquidation behind it and quantity left is still
        under liquidation."""
        for event in ledger.economics.events:
            if event.event_type != EconomicEventType.LIQUIDATION:
                continue
            key = (event.venue, event.instrument_id)
            self.events.append({"venue": event.venue, "instrument_id": event.instrument_id,
                                "ts_ns": event.effective, "code": "LIQUIDATION_RESTORED",
                                "order_id": event.order_id, "detail": "replayed from the financing journal"})
            if ledger.positions.get(key, ZERO):
                self.active.add(key)
            else:
                self.active.discard(key)

    def liquidate(self, venue: str, instrument_id: str, book: Any, now_ns: int, reason: str) -> dict[str, Any] | None:
        policy = self.config.venues[venue].liquidation
        if not policy.enabled or book is None or not getattr(book, "is_tradable", False):
            return None
        position = self.ledger.positions.get((venue, instrument_id), ZERO)
        if not position:
            self.active.discard((venue, instrument_id))
            return None
        marker = (getattr(book, "sequence", 0), len(book.bids) + len(book.asks))
        if self._attempted.get((venue, instrument_id)) == marker:
            return None                                     # at most once per book update
        self._attempted[(venue, instrument_id)] = marker
        side = Side.SELL if position > 0 else Side.BUY
        adapter = self.adapter_for(venue)
        if not hasattr(adapter, "_walk"):
            self.events.append({"venue": venue, "instrument_id": instrument_id, "ts_ns": now_ns,
                                "code": "LIQUIDATION_NOT_SIMULATED", "detail": "venue adapter has no book walk"})
            return None
        limit = Decimal("0") if side == Side.SELL else Decimal("Infinity")
        filled, price, takes = adapter._walk(book, side, abs(position), limit)   # (qty, average price, levels)
        row: dict[str, Any] = {"venue": venue, "instrument_id": instrument_id, "ts_ns": now_ns, "reason": reason,
                               "position_before": str(position), "side": side.value,
                               "filled": str(filled), "residual": str(abs(position) - filled)}
        if filled > 0:
            adapter._consume(book, side, takes)
            self._sequence += 1
            notional = filled * price
            fee = notional * policy.fee_rate
            order = ChildOrder(
                order_id=f"liquidation|{venue}|{instrument_id}|{now_ns}|{self._sequence}",
                parent_id="venue-liquidation", opportunity_id="", venue=venue, instrument_id=instrument_id,
                side=side, quantity=filled, limit_price=price, state=OrderState.FILLED,
                filled_quantity=filled, average_price=price, fee=fee, order_role="LIQUIDATION",
                fee_asset="", fee_rate_bps=policy.fee_rate * Decimal("10000"),
                liquidity="LIQUIDATION", fee_source=f"LIQUIDATION_FEE_{policy.source}",
                state_updates=[{"event_ts_ns": now_ns, "cumulative_filled_quantity": str(filled)}],
            )
            self.ledger.apply_fill(order, now_ns, economic_type=EconomicEventType.LIQUIDATION)
            row.update(price=str(price), notional=str(notional), fee=str(fee),
                       levels=len(takes), order_id=order.order_id)
        row["code"] = "LIQUIDATION_RESIDUAL" if filled < abs(position) else "LIQUIDATED"
        row["position_after"] = str(self.ledger.positions.get((venue, instrument_id), ZERO))
        if row["code"] == "LIQUIDATION_RESIDUAL":
            self.active.add((venue, instrument_id))
        else:
            self.active.discard((venue, instrument_id))
        self.events.append(row)
        if self.on_liquidation is not None:
            self.on_liquidation(f"venue liquidation {venue} {instrument_id}: {row['code']}", now_ns)
        return row


class AccountTierReadError(RuntimeError):
    """Owner 16 Nov (limit A6): a venue's own risk tiers could not be read at start-up (no reader, a failed read, an unexpected shape): a real-order session then refuses to start."""


def read_account_tiers(readers: dict[str, Any], venues: tuple[str, ...], instrument_id: str) -> dict[str, TierSchedule]:
    """Read every venue's own risk tiers for the instrument (`readers[venue].account_risk_tiers(instrument_id)` -> rows of text rates) into TierSchedules sourced 'ACCOUNT_READ'. Collects every problem and raises
    once (fail closed: tiers that cannot be read are not tiers that were checked). Read-only."""
    out: dict[str, TierSchedule] = {}
    problems: list[str] = []
    for venue in venues:
        reader = readers.get(venue)
        if reader is None:
            problems.append(f"{venue}: no account tier reader (the account's own tiers are not read)")
            continue
        try:
            rows = reader.account_risk_tiers(instrument_id)
            out[venue] = TierSchedule(tuple(
                RiskTier(max_notional=Decimal(str(r["max_notional"])), maintenance_rate=Decimal(str(r["maintenance_rate"])), initial_rate=Decimal(str(r["initial_rate"])),
                         maintenance_deduction=Decimal(str(r.get("maintenance_deduction", "0")))) for r in rows), source=f"ACCOUNT_READ:{venue}")
        except Exception as exc:
            problems.append(f"{venue}: the account's risk tiers could not be read ({type(exc).__name__}: {exc})")
    if problems:
        raise AccountTierReadError("account risk tiers unavailable: " + "; ".join(problems))
    return out


def apply_account_tiers(config: MarginConfig, tiers: dict[str, TierSchedule], instrument_id: str) -> MarginConfig:
    """The margin configuration with the account-read tiers in place of the ASSUMED ones, for the venues it configures. Nothing else changes."""
    from dataclasses import replace as _replace

    venues = {}
    for venue, vc in config.venues.items():
        venues[venue] = _replace(vc, tiers={**vc.tiers, instrument_id: tiers[venue]}) if venue in tiers else vc
    return _replace(config, venues=venues)
