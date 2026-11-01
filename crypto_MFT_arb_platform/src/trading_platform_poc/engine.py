from __future__ import annotations

import hashlib
import json
from typing import Any

from trading_platform_poc.config import AppConfig
from trading_platform_poc.data_quality import (
    AnomalyCode,
    AnomalySeverity,
    DataQualityValidator,
)
from trading_platform_poc.models import (
    BookDelta,
    BookSnapshot,
    EventType,
    MarketEvent,
    OrderState,
    OrderUpdate,
    TradeTick,
)
from trading_platform_poc.oms import OrderManagementSystem
from trading_platform_poc.order_book import BinanceOrderBook
from trading_platform_poc.order_book import InvalidBookError, SequenceGapError
from trading_platform_poc.paper_exchange import PaperExchange, PaperResult
from trading_platform_poc.portfolio import Portfolio
from trading_platform_poc.risk import RiskContext, RiskEngine
from trading_platform_poc.strategy import SpreadCaptureStrategy


class TradingEngine:
    def __init__(
        self,
        config: AppConfig,
        *,
        book: BinanceOrderBook | None = None,
        strategy: Any | None = None,
        risk: RiskEngine | None = None,
        oms: OrderManagementSystem | None = None,
        exchange: PaperExchange | None = None,
        portfolio: Portfolio | None = None,
        data_quality: DataQualityValidator | None = None,
    ) -> None:
        self.config = config
        self.book = (
            BinanceOrderBook(config.symbol) if book is None else book
        )
        self.strategy = (
            SpreadCaptureStrategy(config.strategy)
            if strategy is None
            else strategy
        )
        self.risk = RiskEngine(config.risk) if risk is None else risk
        self.oms = OrderManagementSystem() if oms is None else oms
        self.exchange = (
            PaperExchange(config.paper) if exchange is None else exchange
        )
        self.portfolio = Portfolio() if portfolio is None else portfolio
        self.data_quality = (
            DataQualityValidator(config.data_quality)
            if data_quality is None
            else data_quality
        )
        self.market_data_healthy = False
        self._minimum_recovery_snapshot_sequence: int | None = None
        self.opportunities: list[dict[str, Any]] = []
        self.event_count = 0
        self._strategy_parameter_hash = hashlib.sha256(
            json.dumps(
                config.strategy.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def process(self, event: MarketEvent) -> bool:
        self.event_count += 1
        quality_state = self.data_quality.capture_state()
        if not self.data_quality.validate_event(event):
            self.mark_market_data_unhealthy(event.receive_timestamp_ns)
            return False
        if event.event_type is EventType.BOOK_SNAPSHOT:
            if not isinstance(event.payload, BookSnapshot):
                raise TypeError("book snapshot event has an invalid payload")
            minimum_sequence = self._minimum_recovery_snapshot_sequence
            if (
                minimum_sequence is not None
                and event.payload.last_update_id < minimum_sequence
            ):
                self.data_quality.restore_state(quality_state)
                self.data_quality.report(
                    AnomalyCode.INVALID_SEQUENCE,
                    AnomalySeverity.ERROR,
                    "recovery snapshot is older than the invalidated stream",
                    event=event,
                    blocking=True,
                    details={
                        "snapshot_last_update_id": str(
                            event.payload.last_update_id
                        ),
                        "minimum_recovery_sequence": str(minimum_sequence),
                    },
                )
                self.mark_market_data_unhealthy(
                    event.receive_timestamp_ns,
                    minimum_snapshot_sequence=minimum_sequence,
                )
                return False
            try:
                self.book.load_snapshot(
                    event.payload,
                    event.receive_timestamp_ns,
                )
            except InvalidBookError as error:
                self.data_quality.restore_state(quality_state)
                self.data_quality.report(
                    AnomalyCode.INVALID_BOOK,
                    AnomalySeverity.ERROR,
                    str(error),
                    event=event,
                    blocking=True,
                )
                self.mark_market_data_unhealthy(
                    event.receive_timestamp_ns,
                    minimum_snapshot_sequence=minimum_sequence,
                )
                return False
            self.market_data_healthy = self.data_quality.validate_book(
                self.book,
                event,
            )
            if not self.market_data_healthy:
                self.data_quality.restore_state(quality_state)
                self.mark_market_data_unhealthy(
                    event.receive_timestamp_ns,
                    minimum_snapshot_sequence=minimum_sequence,
                )
                return False
            self._minimum_recovery_snapshot_sequence = None
            return self.market_data_healthy
        if event.event_type is EventType.BOOK_DELTA:
            if not isinstance(event.payload, BookDelta):
                raise TypeError("book delta event has an invalid payload")
            if not self.market_data_healthy or not self.book.initialized:
                self.data_quality.report(
                    AnomalyCode.STRATEGY_BLOCKED,
                    AnomalySeverity.WARNING,
                    "book delta was ignored until a valid snapshot is accepted",
                    event=event,
                    blocking=True,
                )
                self.data_quality.restore_state(quality_state)
                return False
            try:
                applied = self.book.apply_delta(
                    event.payload,
                    event.receive_timestamp_ns,
                )
            except SequenceGapError as error:
                self.data_quality.restore_state(quality_state)
                self.mark_market_data_unhealthy(
                    event.receive_timestamp_ns,
                    minimum_snapshot_sequence=event.payload.final_update_id,
                )
                self.data_quality.report(
                    AnomalyCode.SEQUENCE_GAP,
                    AnomalySeverity.ERROR,
                    str(error),
                    event=event,
                    blocking=True,
                )
                return False
            except InvalidBookError as error:
                self.data_quality.restore_state(quality_state)
                self.mark_market_data_unhealthy(
                    event.receive_timestamp_ns,
                    minimum_snapshot_sequence=event.payload.final_update_id,
                )
                self.data_quality.report(
                    AnomalyCode.INVALID_BOOK,
                    AnomalySeverity.ERROR,
                    str(error),
                    event=event,
                    blocking=True,
                )
                return False
            if not applied:
                self.data_quality.report(
                    AnomalyCode.OUT_OF_ORDER_BOOK_UPDATE,
                    AnomalySeverity.WARNING,
                    "duplicate or stale order-book update was dropped",
                    event=event,
                    blocking=True,
                    details={
                        "book_last_update_id": str(
                            self.book.last_update_id
                        ),
                        "delta_first_update_id": str(
                            event.payload.first_update_id
                        ),
                        "delta_final_update_id": str(
                            event.payload.final_update_id
                        ),
                    },
                )
                self.data_quality.restore_state(quality_state)
                return False
            if applied:
                self.market_data_healthy = (
                    self.data_quality.validate_book(self.book, event)
                )
                if not self.market_data_healthy:
                    self.data_quality.restore_state(quality_state)
                    self.mark_market_data_unhealthy(
                        event.receive_timestamp_ns,
                        minimum_snapshot_sequence=(
                            event.payload.final_update_id
                        ),
                    )
                    return False
                feed_stale = (
                    event.receive_timestamp_ns - event.exchange_timestamp_ns
                    > self.config.binance.stale_after_ms * 1_000_000
                )
                self._evaluate_strategy(
                    event.receive_timestamp_ns,
                    feed_stale,
                )
            return self.market_data_healthy
        if event.event_type is EventType.TRADE:
            if not isinstance(event.payload, TradeTick):
                raise TypeError("trade event has an invalid payload")
            if not self.market_data_healthy:
                self.data_quality.report(
                    AnomalyCode.STRATEGY_BLOCKED,
                    AnomalySeverity.WARNING,
                    "trade was not sent to execution because book data is unhealthy",
                    event=event,
                    blocking=True,
                )
                self.data_quality.restore_state(quality_state)
                return False
            result = self.exchange.process_trade(
                event.payload,
                event.receive_timestamp_ns,
            )
            self._apply_paper_result(result)
            return True
        return True

    def mark_market_data_unhealthy(
        self,
        timestamp_ns: int,
        *,
        minimum_snapshot_sequence: int | None = None,
    ) -> None:
        current_sequence = self.book.last_update_id
        recovery_sequences = [
            sequence
            for sequence in (
                self._minimum_recovery_snapshot_sequence,
                current_sequence,
                minimum_snapshot_sequence,
            )
            if sequence is not None
        ]
        self._minimum_recovery_snapshot_sequence = (
            max(recovery_sequences) if recovery_sequences else None
        )
        self.market_data_healthy = False
        self.book.restore_state(None)
        self.data_quality.reset_book_state(self.book.symbol)
        self._apply_paper_result(self.exchange.cancel_all(timestamp_ns))

    def activate_kill_switch(self, timestamp_ns: int) -> None:
        self.risk.set_kill_switch(True)
        self._apply_paper_result(self.exchange.cancel_all(timestamp_ns))

    def close(self, timestamp_ns: int) -> None:
        self._apply_paper_result(self.exchange.cancel_all(timestamp_ns))

    def _evaluate_strategy(
        self,
        timestamp_ns: int,
        feed_stale: bool,
    ) -> None:
        if self.risk.kill_switch_active:
            return
        if self.strategy.should_cancel(self.book):
            self._apply_paper_result(
                self.exchange.cancel_all(timestamp_ns)
            )
            return
        if feed_stale:
            self._apply_paper_result(
                self.exchange.cancel_all(timestamp_ns)
            )
        intents = self.strategy.evaluate(
            self.book,
            timestamp_ns,
            self.oms.open_orders(),
        )
        for intent in intents:
            context = RiskContext(
                position=self.portfolio.position,
                open_order_count=len(self.oms.open_orders()),
                daily_net_pnl=self.portfolio.net_realized_pnl,
                feed_stale=feed_stale,
                mid_price=self.book.mid_price,
            )
            decision = self.risk.evaluate(intent, context, timestamp_ns)
            order = self.oms.create(intent)
            self.opportunities.append(
                {
                    "intent_id": str(intent.intent_id),
                    "strategy_id": intent.strategy_id,
                    "symbol": intent.symbol,
                    "side": intent.side.value,
                    "quantity": str(intent.quantity),
                    "price": str(intent.price),
                    "timestamp_ns": timestamp_ns,
                    "signal_timestamp_ns": intent.created_timestamp_ns,
                    "decision_timestamp_ns": decision.decision_timestamp_ns,
                    "best_bid": str(self.book.best_bid),
                    "best_ask": str(self.book.best_ask),
                    "mid_price": str(self.book.mid_price),
                    "spread": str(self.book.spread),
                    "spread_bps": str(self.book.spread_bps()),
                    "expected_gross_edge": str(
                        (self.book.spread or 0) * intent.quantity
                    ),
                    "strategy_version": getattr(
                        self.strategy,
                        "strategy_version",
                        "unversioned",
                    ),
                    "strategy_parameter_hash": self._strategy_parameter_hash,
                    "approved": decision.approved,
                    "reason": decision.reason,
                }
            )
            if not decision.approved:
                self.oms.transition(
                    order.order_id,
                    OrderState.RISK_REJECTED,
                    timestamp_ns,
                    reason=decision.reason,
                )
                continue

            order = self.oms.transition(
                order.order_id,
                OrderState.RISK_APPROVED,
                timestamp_ns,
                reason="approved",
            )
            order = self.oms.transition(
                order.order_id,
                OrderState.SUBMITTED,
                timestamp_ns,
            )
            self._apply_paper_result(
                self.exchange.submit(order, self.book, timestamp_ns)
            )

    def _apply_paper_result(self, result: PaperResult) -> None:
        for update in result.updates:
            self._apply_update(update)
        for fill in result.fills:
            self.portfolio.apply_fill(fill)

    def _apply_update(self, update: OrderUpdate) -> None:
        self.oms.transition(
            update.order_id,
            update.state,
            update.update_timestamp_ns,
            update.cumulative_filled_quantity,
            update.average_fill_price,
            update.reason,
        )
