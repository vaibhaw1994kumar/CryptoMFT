from __future__ import annotations

from ..intent import IntentError, Role
from .base import Tactic


class MarketBanded(Tactic):
    """IOC with the unwind price band (mid +/- band). The only tactic that may carry no worst_price (legacy unbounded unwind);
    when a band is configured the child's limit must sit inside it."""
    name = "MARKET_BANDED"
    roles = (Role.UNWIND, Role.FLATTEN)
    passive = False

    def check_cap(self, intent, leg) -> None:
        if intent.worst_price is None:
            band = float(self.cfg.unwind.get("band_bps") or 0.0)
            if band > 0.0 and self.cfg.price_cap_required:
                raise IntentError(f"unwind {intent.intent_id}: a band of {band} bps is configured but the intent carries no worst_price")
            return
        super().check_cap(intent, leg)
