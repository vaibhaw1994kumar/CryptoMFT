"""Cross-venue basis mean-reversion (statistical arbitrage) strategy.

Ported from ``unified_trading_platform/.../minreq/statarb.py`` onto this
platform's books, eligibility policy and ``Opportunity`` contract.

Signal: the basis ``(mid_quote - mid_reference) / mid_reference`` in bps,
standardised against a rolling window of *past* samples only (the current
observation never enters its own mean/stdev, so there is no look-ahead).

* basis rich (z >= entry_z): sell the quote venue, buy the reference venue
  (``SHORT_BASIS``);
* basis cheap (z <= -entry_z): buy the quote venue, sell the reference venue
  (``LONG_BASIS``);
* exit when the basis reverts (|z| <= exit_z on the correct side), on a stop
  (|z| >= stop_z) or after ``max_holding_ns``.

The strategy only emits intents. The stance is updated optimistically when an
intent is emitted and reverted when the OMS reports that the package did not
fill (residuals are unwound by the OMS), so the stance follows real positions.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import StrEnum
import math
from typing import Any

from .alignment import CrossVenueEligibilityPolicy
from .fees import Liquidity
from .instruments import InstrumentMaster
from .models import Opportunity, ParentPlan, PlanState, Side, stable_id
from .orderbook import OrderBook


STRATEGY_VERSION = "1.0.0"


class Stance(StrEnum):
    FLAT = "FLAT"
    SHORT_BASIS = "SHORT_BASIS"
    LONG_BASIS = "LONG_BASIS"


@dataclass(frozen=True, slots=True)
class MeanReversionConfig:
    strategy_id: str = "basis_mean_reversion_v1"
    quantity: Decimal = Decimal("0.002")
    window: int = 300
    min_samples: int = 60
    sample_interval_ns: int = 1_000_000_000
    entry_z: float = 2.5
    exit_z: float = 0.5
    stop_z: float = 6.0
    min_deviation_bps: float = 1.0
    cooldown_ns: int = 10_000_000_000
    max_holding_ns: int | None = 1_800_000_000_000

    def __post_init__(self) -> None:
        if self.min_samples < 2 or self.window < self.min_samples:
            raise ValueError("window must be >= min_samples >= 2")
        if not 0 <= self.exit_z < self.entry_z < self.stop_z:
            raise ValueError("thresholds must satisfy 0 <= exit_z < entry_z < stop_z")
        if self.quantity <= 0 or self.sample_interval_ns <= 0:
            raise ValueError("quantity and sample interval must be positive")

    @classmethod
    def from_config(cls, row: dict[str, Any]) -> "MeanReversionConfig":
        holding = row.get("max_holding_ms", 1_800_000)
        return cls(
            strategy_id=str(row.get("strategy_id", "basis_mean_reversion_v1")),
            quantity=Decimal(str(row.get("quantity", "0.002"))),
            window=int(row.get("window", 300)),
            min_samples=int(row.get("min_samples", 60)),
            sample_interval_ns=int(row.get("sample_interval_ms", 1_000)) * 1_000_000,
            entry_z=float(row.get("entry_z", 2.5)),
            exit_z=float(row.get("exit_z", 0.5)),
            stop_z=float(row.get("stop_z", 6.0)),
            min_deviation_bps=float(row.get("min_deviation_bps", 1.0)),
            cooldown_ns=int(row.get("cooldown_ms", 10_000)) * 1_000_000,
            max_holding_ns=None if holding is None else int(holding) * 1_000_000,
        )


class CrossVenueMeanReversionStrategy:
    def __init__(
        self,
        config: MeanReversionConfig,
        instrument_id: str,
        venues: tuple[str, str],
        instruments: InstrumentMaster,
        eligibility_policy: CrossVenueEligibilityPolicy,
    ) -> None:
        self.config = config
        self.strategy_id = config.strategy_id
        self.instrument_id = instrument_id
        self.reference_venue, self.quote_venue = venues
        self.instruments = instruments
        self.eligibility_policy = eligibility_policy
        self.stance = Stance.FLAT
        self.allow_entries = True
        self.opened_at_ns = 0
        self.last_action_ns: int | None = None
        self.pending: dict[str, tuple[str, Stance]] = {}
        self._samples: deque[float] = deque(maxlen=config.window)
        self._next_sample_ns = 0
        self._sequence = 0
        self.samples_taken = 0
        self.last_basis_bps: float | None = None
        self.last_zscore: float | None = None
        self.max_abs_zscore = 0.0
        self.actions: dict[str, int] = {}
        self.plan_outcomes: dict[str, int] = {}
        self.stance_reverts = 0
        self.skipped: dict[str, int] = {}

    # -- signal --------------------------------------------------------------

    @staticmethod
    def _mid(book: OrderBook) -> Decimal:
        return (book.best_bid + book.best_ask) / Decimal("2")

    def _statistics(self) -> tuple[float, float] | None:
        count = len(self._samples)
        if count < self.config.min_samples:
            return None
        mean = math.fsum(self._samples) / count
        variance = math.fsum((value - mean) ** 2 for value in self._samples) / (count - 1)
        return mean, math.sqrt(variance)

    def _skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def evaluate(
        self, books: dict[tuple[str, str], OrderBook], now_ns: int
    ) -> Opportunity | None:
        decision = self.eligibility_policy.evaluate(books, now_ns)
        if not decision.eligible:
            return None
        reference = books[(self.reference_venue, self.instrument_id)]
        quote = books[(self.quote_venue, self.instrument_id)]
        reference_mid = self._mid(reference)
        basis_bps = float((self._mid(quote) - reference_mid) / reference_mid * Decimal("10000"))
        stats = self._statistics()
        zscore = None
        mean = None
        if stats is not None and stats[1] > 1e-9:
            mean = stats[0]
            zscore = (basis_bps - mean) / stats[1]
        if now_ns >= self._next_sample_ns:
            # Appended after the z-score is computed: no look-ahead.
            self._samples.append(basis_bps)
            self._next_sample_ns = now_ns + self.config.sample_interval_ns
            self.samples_taken += 1
        self.last_basis_bps = basis_bps
        self.last_zscore = zscore
        if zscore is None or mean is None:
            self._skip("warmup")
            return None
        if not math.isfinite(zscore):
            self._skip("non_finite_zscore")
            return None
        self.max_abs_zscore = max(self.max_abs_zscore, abs(zscore))
        if self.pending:
            self._skip("plan_working")
            return None
        if (
            self.last_action_ns is not None
            and now_ns - self.last_action_ns < self.config.cooldown_ns
        ):
            self._skip("cooldown")
            return None
        deviation = basis_bps - mean
        if self.stance == Stance.FLAT:
            if not self.allow_entries:
                return None
            if zscore >= self.config.entry_z and deviation >= self.config.min_deviation_bps:
                return self._intent(
                    books, now_ns, decision.fingerprint, "enter_short_basis",
                    buy_venue=self.reference_venue, sell_venue=self.quote_venue,
                    target=Stance.SHORT_BASIS, deviation=deviation,
                )
            if zscore <= -self.config.entry_z and -deviation >= self.config.min_deviation_bps:
                return self._intent(
                    books, now_ns, decision.fingerprint, "enter_long_basis",
                    buy_venue=self.quote_venue, sell_venue=self.reference_venue,
                    target=Stance.LONG_BASIS, deviation=deviation,
                )
            return None
        return self._maybe_exit(books, now_ns, decision.fingerprint, zscore, deviation)

    def _maybe_exit(
        self, books, now_ns: int, fingerprint: str, zscore: float, deviation: float,
        *, force: bool = False,
    ) -> Opportunity | None:
        holding = now_ns - self.opened_at_ns
        stop = abs(zscore) >= self.config.stop_z
        reverted = (
            zscore <= self.config.exit_z if self.stance == Stance.SHORT_BASIS
            else zscore >= -self.config.exit_z
        )
        expired = (
            self.config.max_holding_ns is not None
            and holding >= self.config.max_holding_ns
        )
        if not (force or stop or reverted or expired):
            return None
        reason = (
            "forced_exit" if force else "stop" if stop
            else "reversion" if reverted else "max_holding"
        )
        if self.stance == Stance.SHORT_BASIS:
            return self._intent(
                books, now_ns, fingerprint, f"exit_short_basis_{reason}",
                buy_venue=self.quote_venue, sell_venue=self.reference_venue,
                target=Stance.FLAT, deviation=deviation,
            )
        return self._intent(
            books, now_ns, fingerprint, f"exit_long_basis_{reason}",
            buy_venue=self.reference_venue, sell_venue=self.quote_venue,
            target=Stance.FLAT, deviation=deviation,
        )

    def force_exit(
        self, books: dict[tuple[str, str], OrderBook], now_ns: int
    ) -> Opportunity | None:
        """Close an open stance regardless of the signal (session shutdown)."""
        if self.stance == Stance.FLAT or self.pending:
            return None
        decision = self.eligibility_policy.evaluate(books, now_ns)
        if not decision.eligible:
            self._skip("forced_exit_books_ineligible")
            return None
        return self._maybe_exit(
            books, now_ns, decision.fingerprint, self.last_zscore or 0.0, 0.0, force=True
        )

    # -- intents and plan feedback --------------------------------------------

    def _intent(
        self, books, now_ns: int, fingerprint: str, action: str, *,
        buy_venue: str, sell_venue: str, target: Stance, deviation: float,
    ) -> Opportunity | None:
        buy_book = books[(buy_venue, self.instrument_id)]
        sell_book = books[(sell_venue, self.instrument_id)]
        buy_price = buy_book.best_ask
        sell_price = sell_book.best_bid
        quantity = min(
            self.config.quantity,
            buy_book.available(Side.BUY, buy_price),
            sell_book.available(Side.SELL, sell_price),
        )
        quantity = min(
            self.instruments.round_quantity(buy_venue, self.instrument_id, quantity, now_ns),
            self.instruments.round_quantity(sell_venue, self.instrument_id, quantity, now_ns),
        )
        if quantity <= 0:
            self._skip("no_touch_liquidity")
            return None
        gross_bps = (sell_price - buy_price) / buy_price * Decimal("10000")
        fees_bps = (
            self.instruments.fee_bps(buy_venue, self.instrument_id, Liquidity.TAKER, now_ns)
            + self.instruments.fee_bps(sell_venue, self.instrument_id, Liquidity.TAKER, now_ns)
        )
        self._sequence += 1
        opportunity_id = stable_id(
            "opportunity", self.strategy_id, action, now_ns, self._sequence
        )
        previous = self.stance
        self.pending[opportunity_id] = (action, previous)
        self.stance = target
        if action.startswith("enter"):
            self.opened_at_ns = now_ns
        self.last_action_ns = now_ns
        self.actions[action] = self.actions.get(action, 0) + 1
        return Opportunity(
            opportunity_id=opportunity_id,
            strategy_id=self.strategy_id,
            instrument_id=self.instrument_id,
            observed_ts_ns=now_ns,
            buy_venue=buy_venue,
            sell_venue=sell_venue,
            buy_price=buy_price,
            sell_price=sell_price,
            quantity=quantity,
            gross_edge_bps=gross_bps,
            # Expected capture if the basis fully reverts, net of one leg
            # pair of taker fees on entry and on exit.
            estimated_net_edge_bps=(
                Decimal(str(round(abs(deviation), 6))) - 2 * fees_bps
            ),
            source_event_ids=(buy_book.last_event_id or "", sell_book.last_event_id or ""),
            eligibility_fingerprint=fingerprint,
        )

    def on_plan_closed(self, plan: ParentPlan) -> None:
        pending = self.pending.pop(plan.opportunity.opportunity_id, None)
        if pending is None:
            return
        action, previous = pending
        outcome = plan.terminal_outcome or plan.state.value
        self.plan_outcomes[outcome] = self.plan_outcomes.get(outcome, 0) + 1
        if plan.state == PlanState.CLOSED and outcome == PlanState.FILLED.value:
            return
        # Package did not complete (rejected, or residual unwound): the
        # position did not change, so restore the stance it had before.
        self.stance = previous
        self.stance_reverts += 1

    def summary(self) -> dict[str, Any]:
        stats = self._statistics()
        return {
            "strategy_id": self.strategy_id,
            "type": "CROSS_VENUE_BASIS_MEAN_REVERSION",
            "version": STRATEGY_VERSION,
            "config": {
                key: (str(value) if isinstance(value, Decimal) else value)
                for key, value in asdict(self.config).items()
            },
            "stance": self.stance.value,
            "pending_plans": len(self.pending),
            "samples_taken": self.samples_taken,
            "window_samples": len(self._samples),
            "warmed_up": stats is not None,
            "basis_mean_bps": None if stats is None else stats[0],
            "basis_stdev_bps": None if stats is None else stats[1],
            "last_basis_bps": self.last_basis_bps,
            "last_zscore": self.last_zscore,
            "max_abs_zscore": self.max_abs_zscore,
            "actions": dict(sorted(self.actions.items())),
            "plan_outcomes": dict(sorted(self.plan_outcomes.items())),
            "stance_reverts": self.stance_reverts,
            "skipped": dict(sorted(self.skipped.items())),
        }
