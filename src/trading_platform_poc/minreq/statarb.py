"""Cross-venue mean-reversion strategy on the same economic instrument.

The signal is the standardised basis between two venues. When the basis is
rich the strategy sells the rich venue and buys the cheap one; the position is
closed when the basis reverts toward its rolling mean. The strategy only ever
expresses intent: sizing, venue choice, risk and execution belong downstream.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from trading_platform_poc.minreq.features import FeatureSnapshot
from trading_platform_poc.minreq.router import OpportunityIntent

STRATEGY_VERSION = "1.1.0"


class Stance(str, Enum):
    FLAT = "flat"
    LONG_BASIS = "long_basis"
    SHORT_BASIS = "short_basis"


@dataclass(frozen=True, slots=True)
class StatArbConfig:
    economic_group: str
    entry_z: Decimal = Decimal("2.0")
    exit_z: Decimal = Decimal("0.5")
    stop_z: Decimal = Decimal("5.0")
    target_quantity: Decimal = Decimal("0.02")
    cooldown_ns: int = 250_000_000
    min_samples: int = 20
    max_open_stances: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_version": STRATEGY_VERSION,
            "economic_group": self.economic_group,
            "entry_z": str(self.entry_z),
            "exit_z": str(self.exit_z),
            "stop_z": str(self.stop_z),
            "target_quantity": str(self.target_quantity),
            "cooldown_ns": self.cooldown_ns,
            "min_samples": self.min_samples,
            "max_open_stances": self.max_open_stances,
        }

    def config_hash(self) -> str:
        document = "|".join(f"{key}={value}" for key, value in sorted(
            self.to_dict().items()
        ))
        return hashlib.sha256(document.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    timestamp_ns: int
    stance: Stance
    action: str
    zscore: Decimal | None
    reason: str
    intent: OpportunityIntent | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_ns": self.timestamp_ns,
            "stance": self.stance.value,
            "action": self.action,
            "zscore": None if self.zscore is None else str(self.zscore),
            "reason": self.reason,
            "opportunity_id": (
                None if self.intent is None else self.intent.opportunity_id
            ),
        }


class CrossVenueStatArbStrategy:
    """Emit entry and exit intents from the standardised cross-venue basis."""

    def __init__(
        self,
        config: StatArbConfig,
        venue_instruments: Mapping[str, str],
        *,
        reference_venue: str | None = None,
    ) -> None:
        if len(venue_instruments) < 2:
            raise ValueError("the strategy needs two venues")
        self.config = config
        self.venue_instruments = dict(sorted(venue_instruments.items()))
        venues = tuple(self.venue_instruments)
        self.reference_venue = reference_venue or venues[0]
        self.quote_venue = next(
            venue for venue in venues if venue != self.reference_venue
        )
        self.stance = Stance.FLAT
        self.opened_at_ns = 0
        self.last_action_ns = 0
        self.decisions: list[StrategyDecision] = []
        self._sequence = 0
        self.stance_corrections: list[dict[str, Any]] = []

    def observe_positions(
        self, positions: Mapping[str, Decimal], *, busy: bool
    ) -> None:
        """Make the stance follow the positions actually held.

        An intent is not a position: a rejected or partially unwound entry
        leaves the book flat while the strategy believes it is in a trade, and
        a flatten after a kill leaves it flat too. While no plan is working,
        the stance is re-derived from the per-venue positions.
        """

        if busy:
            return
        reference = positions.get(self.reference_venue, Decimal(0))
        quote = positions.get(self.quote_venue, Decimal(0))
        if reference == 0 and quote == 0:
            implied = Stance.FLAT
        elif reference > 0 and quote < 0:
            implied = Stance.SHORT_BASIS
        elif reference < 0 and quote > 0:
            implied = Stance.LONG_BASIS
        else:
            # One-sided exposure is a residual owned by risk, not a stance.
            return
        if implied is not self.stance:
            self.stance_corrections.append(
                {
                    "from": self.stance.value,
                    "to": implied.value,
                    "reference_position": str(reference),
                    "quote_position": str(quote),
                }
            )
            self.stance = implied

    def _opportunity_id(self, as_of_ns: int, action: str) -> str:
        self._sequence += 1
        key = (
            f"{self.config.economic_group}|{action}|{as_of_ns}|{self._sequence}"
        )
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]

    def evaluate(self, snapshot: FeatureSnapshot) -> StrategyDecision:
        """Decide once per feature snapshot. Never looks past ``as_of_ns``."""

        zscore = snapshot.value("basis_zscore")
        as_of = snapshot.as_of_ns

        def record(
            action: str, reason: str, intent: OpportunityIntent | None
        ) -> StrategyDecision:
            decision = StrategyDecision(
                timestamp_ns=as_of,
                stance=self.stance,
                action=action,
                zscore=zscore,
                reason=reason,
                intent=intent,
            )
            self.decisions.append(decision)
            return decision

        if not snapshot.healthy:
            return record("hold", ";".join(snapshot.health_reasons), None)
        if snapshot.sample_count < self.config.min_samples:
            return record("hold", "warmup", None)
        if zscore is None:
            return record("hold", "no_signal", None)
        if as_of - self.last_action_ns < self.config.cooldown_ns:
            return record("hold", "cooldown", None)

        if self.stance is Stance.FLAT:
            if zscore >= self.config.entry_z:
                intent = self._build_intent(
                    as_of,
                    snapshot,
                    buy_venue=self.reference_venue,
                    sell_venue=self.quote_venue,
                    reason="entry_basis_rich",
                    signal=zscore,
                )
                self.stance = Stance.SHORT_BASIS
                self.opened_at_ns = as_of
                self.last_action_ns = as_of
                return record("enter_short_basis", "basis_rich", intent)
            if zscore <= -self.config.entry_z:
                intent = self._build_intent(
                    as_of,
                    snapshot,
                    buy_venue=self.quote_venue,
                    sell_venue=self.reference_venue,
                    reason="entry_basis_cheap",
                    signal=zscore,
                )
                self.stance = Stance.LONG_BASIS
                self.opened_at_ns = as_of
                self.last_action_ns = as_of
                return record("enter_long_basis", "basis_cheap", intent)
            return record("hold", "inside_band", None)

        stop_hit = abs(zscore) >= self.config.stop_z
        reverted = abs(zscore) <= self.config.exit_z
        if not (stop_hit or reverted):
            return record("hold", "holding_position", None)

        if self.stance is Stance.SHORT_BASIS:
            intent = self._build_intent(
                as_of,
                snapshot,
                buy_venue=self.quote_venue,
                sell_venue=self.reference_venue,
                reason="stop" if stop_hit else "exit_reversion",
                signal=zscore,
                risk_reducing=True,
            )
            action = "stop_short_basis" if stop_hit else "exit_short_basis"
        else:
            intent = self._build_intent(
                as_of,
                snapshot,
                buy_venue=self.reference_venue,
                sell_venue=self.quote_venue,
                reason="stop" if stop_hit else "exit_reversion",
                signal=zscore,
                risk_reducing=True,
            )
            action = "stop_long_basis" if stop_hit else "exit_long_basis"
        self.stance = Stance.FLAT
        self.opened_at_ns = 0
        self.last_action_ns = as_of
        return record(action, "stop" if stop_hit else "reversion", intent)

    def _build_intent(
        self,
        as_of_ns: int,
        snapshot: FeatureSnapshot,
        *,
        buy_venue: str,
        sell_venue: str,
        reason: str,
        signal: Decimal,
        risk_reducing: bool = False,
    ) -> OpportunityIntent:
        return OpportunityIntent(
            opportunity_id=self._opportunity_id(as_of_ns, reason),
            economic_group=self.config.economic_group,
            feature_id=snapshot.feature_id,
            as_of_ns=as_of_ns,
            target_quantity=self.config.target_quantity,
            buy_venue=buy_venue,
            sell_venue=sell_venue,
            buy_instrument_id=self.venue_instruments[buy_venue],
            sell_instrument_id=self.venue_instruments[sell_venue],
            signal=signal,
            reason=reason,
            risk_reducing=risk_reducing,
        )

    def summary(self) -> dict[str, Any]:
        actions: dict[str, int] = {}
        for decision in self.decisions:
            actions[decision.action] = actions.get(decision.action, 0) + 1
        return {
            "strategy_version": STRATEGY_VERSION,
            "config_hash": self.config.config_hash(),
            "stance": self.stance.value,
            "stance_corrections": len(self.stance_corrections),
            "decisions": len(self.decisions),
            "actions": dict(sorted(actions.items())),
        }
