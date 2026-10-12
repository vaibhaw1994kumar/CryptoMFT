# OMS/RMS pre-trade validation

## Admission invariant

The OMS evaluates the complete intended package before reserving risk or dispatching either child. The result is an immutable `PreTradeValidationReport`; every rule is retained as a pass/fail row with scope, venue, instrument, observed value, configured limit and detail. Any failed row rejects the parent with `dispatched_children=0`. The reservation engine repeats hard capacity checks when it mutates state.

When a cross-venue eligibility fingerprint is supplied, the OMS first verifies that the exact source market state still matches the strategy decision. The pre-trade matrix then validates the current books and controls. This preserves reproducible opportunity routing while ensuring a direct OMS caller cannot bypass freshness checks.

## Risk matrix and test evidence

| Requirement | Matrix code | Source | Rejection test |
| --- | --- | --- | --- |
| Instrument exists | `INSTRUMENT_EXISTS` | Instrument master | `test_instrument_must_exist` |
| Instrument trading status | `INSTRUMENT_STATUS` | Instrument master | `test_instrument_status_must_be_trading` |
| Price band from current mid | `PRICE_BAND` | Book + `max_price_deviation_bps` | `test_price_must_be_inside_configured_band` |
| Tick size | `TICK_SIZE` | Instrument master | `test_price_must_match_tick_size` |
| Lot/quantity step | `LOT_SIZE` | Instrument master | `test_quantity_must_match_lot_size` |
| Minimum quantity | `MINIMUM_QUANTITY` | Instrument master | `test_quantity_must_meet_minimum` |
| Minimum notional | `MINIMUM_NOTIONAL` | Instrument master | `test_notional_must_meet_instrument_minimum` |
| Maximum child order | `MAXIMUM_ORDER_NOTIONAL` | Platform RMS limits | `test_order_must_not_exceed_platform_maximum` |
| Venue order maximum | `VENUE_ORDER_NOTIONAL` | Paper account snapshot | `test_order_must_not_exceed_venue_maximum` |
| Strategy package maximum | `STRATEGY_PACKAGE_NOTIONAL` | Strategy RMS limits | `test_package_must_not_exceed_strategy_limit` |
| Platform gross capacity | `PLATFORM_GROSS_NOTIONAL` | Active reservations + RMS limit | covered by package reservation tests |
| Live book available/valid | `LIVE_BOOK_AVAILABLE`, `LIVE_BOOK_VALID` | Reconstructed order book | `test_live_book_must_be_valid` |
| Live feed age | `LIVE_DATA_AGE` | Local receive timestamp | `test_live_market_data_must_be_fresh` |
| Venue/account enabled | `VENUE_STATUS`, `ACCOUNT_STATUS` | Router + account snapshot | `test_venue_and_account_must_be_enabled` |
| Strategy enabled | `STRATEGY_STATUS` | Account/strategy control | `test_strategy_must_be_enabled_for_account` |
| Trade-break clear | `TRADE_BREAK_STATUS` | Venue-scoped RMS control | trade-break suspension tests |
| Paper cash | `AVAILABLE_CASH` | Account balance and reservations | `test_paper_cash_must_cover_buy_leg` |
| Paper margin | `AVAILABLE_MARGIN` | Margin rate, balance and reservations | `test_paper_margin_must_cover_required_margin` |
| Atomic rejection | report decision + `PRE_TRADE_REJECTED` | OMS ledger | `test_oms_records_matrix_and_dispatches_nothing_on_failure` |

The machine-readable schema is `docs/schemas/pretrade_validation.schema.json`; a compact approved example is `data/samples/orders/pretrade_validation.json`. All limit tests are in `tests/test_pretrade.py`.
