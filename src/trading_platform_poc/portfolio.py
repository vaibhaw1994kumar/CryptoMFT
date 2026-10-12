from __future__ import annotations

from decimal import Decimal

from trading_platform_poc.models import Fill, Side


class Portfolio:
    def __init__(self) -> None:
        self.position = Decimal(0)
        self.average_entry_price = Decimal(0)
        self.realized_pnl = Decimal(0)
        self.fees = Decimal(0)
        self.fills: list[Fill] = []
        self.maximum_absolute_position = Decimal(0)
        self.maximum_notional_exposure = Decimal(0)

    @property
    def net_realized_pnl(self) -> Decimal:
        return self.realized_pnl - self.fees

    def apply_fill(self, fill: Fill) -> None:
        signed_quantity = (
            fill.quantity if fill.side is Side.BUY else -fill.quantity
        )
        old_position = self.position
        new_position = old_position + signed_quantity

        if old_position == 0 or (old_position > 0) == (signed_quantity > 0):
            total_cost = (
                abs(old_position) * self.average_entry_price
                + abs(signed_quantity) * fill.price
            )
            self.position = new_position
            self.average_entry_price = total_cost / abs(new_position)
        else:
            closing_quantity = min(abs(old_position), abs(signed_quantity))
            if old_position > 0:
                self.realized_pnl += (
                    fill.price - self.average_entry_price
                ) * closing_quantity
            else:
                self.realized_pnl += (
                    self.average_entry_price - fill.price
                ) * closing_quantity
            self.position = new_position
            if new_position == 0:
                self.average_entry_price = Decimal(0)
            elif (new_position > 0) != (old_position > 0):
                self.average_entry_price = fill.price

        self.fees += fill.fee
        self.fills.append(fill)
        self.maximum_absolute_position = max(
            self.maximum_absolute_position,
            abs(self.position),
        )
        self.maximum_notional_exposure = max(
            self.maximum_notional_exposure,
            abs(self.position) * fill.price,
        )

    def unrealized_pnl(self, mark_price: Decimal | None) -> Decimal:
        if mark_price is None or self.position == 0:
            return Decimal(0)
        if self.position > 0:
            return (mark_price - self.average_entry_price) * self.position
        return (
            self.average_entry_price - mark_price
        ) * abs(self.position)

    def snapshot(self, mark_price: Decimal | None) -> dict[str, str]:
        unrealized = self.unrealized_pnl(mark_price)
        return {
            "position": str(self.position),
            "average_entry_price": str(self.average_entry_price),
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(unrealized),
            "fees": str(self.fees),
            "net_pnl": str(self.net_realized_pnl + unrealized),
            "maximum_absolute_position": str(
                self.maximum_absolute_position
            ),
            "maximum_notional_exposure": str(
                self.maximum_notional_exposure
            ),
        }
