from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading_platform_poc.config import RiskConfig
from trading_platform_poc.models import OrderIntent, RiskDecision, Side


@dataclass(frozen=True, slots=True)
class RiskContext:
    position: Decimal
    open_order_count: int
    daily_net_pnl: Decimal
    feed_stale: bool
    mid_price: Decimal | None


class RiskEngine:
    def __init__(self, config: RiskConfig) -> None:
        self._config = config
        self._kill_switch_active = False

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch_active

    def set_kill_switch(self, active: bool) -> None:
        self._kill_switch_active = active

    def evaluate(
        self,
        intent: OrderIntent,
        context: RiskContext,
        timestamp_ns: int,
    ) -> RiskDecision:
        rejection = self._rejection_reason(intent, context)
        return RiskDecision(
            approved=rejection is None,
            reason=rejection or "approved",
            decision_timestamp_ns=timestamp_ns,
        )

    def _rejection_reason(
        self,
        intent: OrderIntent,
        context: RiskContext,
    ) -> str | None:
        if self._kill_switch_active:
            return "kill_switch_active"
        if context.feed_stale:
            return "stale_market_data"
        if context.mid_price is None or context.mid_price <= 0:
            return "missing_reference_price"
        if context.open_order_count >= self._config.max_open_orders:
            return "max_open_orders"
        if context.daily_net_pnl <= -self._config.max_daily_loss:
            return "max_daily_loss"

        reference_price = intent.price or context.mid_price
        notional = reference_price * intent.quantity
        if notional > self._config.max_order_notional:
            return "max_order_notional"

        signed_quantity = (
            intent.quantity if intent.side is Side.BUY else -intent.quantity
        )
        projected_position = context.position + signed_quantity
        if abs(projected_position) > self._config.max_net_position:
            return "max_net_position"

        deviation_bps = (
            abs(reference_price - context.mid_price)
            / context.mid_price
            * Decimal(10_000)
        )
        if deviation_bps > self._config.price_collar_bps:
            return "price_collar"
        return None
