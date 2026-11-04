from __future__ import annotations

from ..intent import Role
from .base import Tactic


class IocCapped(Tactic):
    """IOC limit capped at worst_price. Phase 0: the cap is today's touch +/- slack, so the behaviour is unchanged; the
    mandatory-cap rule means a hedge without a worst_price is refused (mutation: removing the cap must fail a test)."""
    name = "IOC_CAPPED"
    roles = (Role.HEDGE, Role.PRIMARY)       # PRIMARY: taker_taker legs
    passive = False
