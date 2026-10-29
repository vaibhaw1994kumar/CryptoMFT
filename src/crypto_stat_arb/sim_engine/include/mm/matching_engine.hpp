#pragma once

#include <array>
#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

#include "mm/types.hpp"   // mm::Side (kBid/kAsk)

// ─────────────────────────────────────────────────────────────────────────────
// mm::MatchingEngine
//
// Simulates Bybit's price-time-priority matching engine for dry-run back-
// testing.  The core data structure is a Red-Black tree (std::map) keyed on
// price, storing a PriceLevel that holds:
//
//   • total_size  – aggregate open interest at this level from the live book
//   • our_orders  – FIFO deque of our own simulated orders at this level
//
// Queue position: when we place a PostOnly order at price P, we snapshot
// the live book's total volume at P into `queue_ahead`.  Each incoming
// market trade at P decrements `queue_ahead_remaining` in FIFO order.
// When `queue_ahead_remaining` reaches 0 the order becomes fillable.
//
// Queue accounting (crypto_stat_arb copy; differs from the FAST_MM original):
//   • Level sizes come only from the book feed.  A trade never edits them,
//     so a trade and the book delta that reflects it are not double counted.
//   • Trades are the only source that *burns* queue ahead.  A book update can
//     only cap `queue_ahead_remaining` at the visible level size; the amount
//     capped is held as `clamp_credit` until the next update at that level,
//     so a book-delta-then-trade pair burns the same queue as trade-then-delta.
//   • Several of our orders at one level share the trade volume: only our own
//     fills absorb volume, so a later order's queue advances by the same print.
//   • A print strictly through our price means the level was swept: the order
//     fills in full at its own price.
//   • A level update to size 0 keeps our resting orders in the queue.
//
// Fee engine:
//   PostOnly fill → maker_rebate_rate (negative = we earn)
//   IOC/market    → taker_fee_rate    (positive = we pay)
//
// Thread safety: NOT thread-safe; use external locking if needed.
// ─────────────────────────────────────────────────────────────────────────────

namespace mm {

// ── Basic types ───────────────────────────────────────────────────────────────

enum class OrderType    { kPostOnly, kIOC, kMarket };
// Aggressor direction for on_trade(): BUY aggressor hits asks, SELL hits bids.
enum class AggressorSide { kBuy, kSell };

struct FillResult {
    uint64_t order_id{0};
    double   filled_qty{0.0};
    double   fill_price{0.0};
    double   fee{0.0};          // negative = rebate earned, positive = fee paid
    double   net_pnl_delta{0.0};// realised PnL contribution including fee
    bool     is_maker{false};
};

struct OrderEntry {
    uint64_t  order_id{0};
    Side      side{Side::kBid};
    double    price{0.0};
    double    size{0.0};
    double    remaining_size{0.0};

    // Queue position tracking
    double    queue_ahead{0.0};           // total book volume ahead at placement
    double    queue_ahead_remaining{0.0}; // decremented by market trades
    double    clamp_credit{0.0};          // queue removed by the last book cap
    bool      queue_satisfied{false};     // true once queue_ahead_remaining <= 0

    OrderType order_type{OrderType::kPostOnly};
    int64_t   placed_at_ns{0};
    bool      active{true};
};

// ── Fee engine ────────────────────────────────────────────────────────────────
//
// Bybit linear USDT perpetuals default fee schedule (VIP 0):
//   Maker: -0.010 bps  →  rate = -0.00001  (rebate)
//   Taker: +0.060 bps  →  rate = +0.00006  (cost)
//
// Adjusted for notional: fee = qty * price * rate

struct FeeConfig {
    double maker_rate{-0.00001};   // −0.010 bps — PostOnly fills earn rebate
    double taker_rate{+0.00006};   //  +0.060 bps — IOC/market fills pay fee
};

class FeeEngine {
public:
    explicit FeeEngine(FeeConfig cfg = {}) : cfg_(cfg) {}

    // Returns signed fee: negative = rebate (you keep it), positive = cost
    [[nodiscard]] double compute(double qty, double price, bool is_maker) const noexcept {
        return qty * price * (is_maker ? cfg_.maker_rate : cfg_.taker_rate);
    }

    [[nodiscard]] const FeeConfig& config() const noexcept { return cfg_; }

    // Cumulative stats
    double total_maker_rebate{0.0};
    double total_taker_fee{0.0};

    void record(double fee, bool is_maker) noexcept {
        if (is_maker) total_maker_rebate -= fee;  // rebate stored as positive
        else          total_taker_fee    += fee;
    }

private:
    FeeConfig cfg_;
};

// ── Price level (one node in the RB tree) ────────────────────────────────────

struct PriceLevel {
    double price{0.0};
    double market_total_size{0.0};  // live book volume at this level (from WS)
    std::deque<uint64_t> our_order_ids;  // FIFO — front = oldest (filled first)
};

// ── Queue position estimator ─────────────────────────────────────────────────
//
// Standalone helper to estimate queue_ahead when placing a new order.
// Uses the live book snapshot: everything at the same price level that was
// placed *before* our order is ahead of us.
//
// For a PostOnly order placed at price P:
//   queue_ahead = book_volume_at_P   (conservative: assume all existing
//                                     volume is ahead of us)
//
// As market trades arrive at P with volume V:
//   queue_ahead_remaining -= V
//   if queue_ahead_remaining <= 0 && trade.price <= our_bid:  fill eligible

class QueuePositionEstimator {
public:
    // Call this when placing an order; returns estimated queue_ahead size
    [[nodiscard]] static double estimate(
        double order_price,
        Side   side,
        const std::map<double, double, std::greater<double>>& bid_book,
        const std::map<double, double, std::less<double>>&    ask_book
    ) noexcept {
        if (side == Side::kBid) {
            auto it = bid_book.find(order_price);
            return it != bid_book.end() ? it->second : 0.0;
        } else {
            auto it = ask_book.find(order_price);
            return it != ask_book.end() ? it->second : 0.0;
        }
    }

    // Incremental update: returns how much of `trade_vol` was consumed
    // against `queue_ahead_remaining` (stops at 0)
    [[nodiscard]] static double consume(double& queue_ahead_remaining,
                                        double trade_vol) noexcept {
        if (queue_ahead_remaining <= 0.0) return 0.0;
        double consumed = std::min(queue_ahead_remaining, trade_vol);
        queue_ahead_remaining -= consumed;
        return consumed;
    }
};

// ── Matching engine ───────────────────────────────────────────────────────────

class MatchingEngine {
public:
    // ── Book types ─────────────────────────────────────────────────────────
    // Bids: descending (best bid = begin())
    using BidBook = std::map<double, PriceLevel, std::greater<double>>;
    // Asks: ascending (best ask = begin())
    using AskBook = std::map<double, PriceLevel, std::less<double>>;

    explicit MatchingEngine(FeeConfig fee_cfg = {}) : fee_engine_(fee_cfg) {}

    // Fraction of the visible level assumed ahead of a newly placed order
    // (1.0 = back of the queue, the conservative default; 0.0 = front).
    void set_queue_position_frac(double frac) {
        if (!(frac >= 0.0 && frac <= 1.0))
            throw std::invalid_argument("queue_position_frac must be within [0, 1]");
        queue_position_frac_ = frac;
    }
    [[nodiscard]] double queue_position_frac() const noexcept { return queue_position_frac_; }

    // ── Live book update (from WS L2 snapshots/deltas) ─────────────────────

    void update_bid_level(double price, double size) {
        _update_level(bid_book_, price, size);
    }

    void update_ask_level(double price, double size) {
        _update_level(ask_book_, price, size);
    }

    // Bulk snapshot reset (call on reconnect or full L2 snapshot)
    void reset_book(
        const std::vector<std::array<double,2>>& bids,  // [{price,size}, ...]
        const std::vector<std::array<double,2>>& asks
    ) {
        // Preserve our own order IDs — only wipe market depth
        for (auto& [p, lvl] : bid_book_) lvl.market_total_size = 0.0;
        for (auto& [p, lvl] : ask_book_) lvl.market_total_size = 0.0;

        for (auto& [price, size] : bids) update_bid_level(price, size);
        for (auto& [price, size] : asks) update_ask_level(price, size);
    }

    // ── Order placement ────────────────────────────────────────────────────

    // Returns order_id (monotonically increasing)
    uint64_t place_order(Side side, double price, double size,
                         OrderType type = OrderType::kPostOnly,
                         int64_t ts_ns = 0) {
        uint64_t oid = ++next_order_id_;

        double queue_ahead = 0.0;
        if (type == OrderType::kPostOnly) {
            // Snapshot live book volume at this price → that's ahead of us
            if (side == Side::kBid) {
                auto it = bid_book_.find(price);
                if (it != bid_book_.end())
                    queue_ahead = it->second.market_total_size * queue_position_frac_;
            } else {
                auto it = ask_book_.find(price);
                if (it != ask_book_.end())
                    queue_ahead = it->second.market_total_size * queue_position_frac_;
            }
        }
        // IOC/market → queue_ahead = 0 (fills immediately against opposite side)

        OrderEntry e{};
        e.order_id              = oid;
        e.side                  = side;
        e.price                 = price;
        e.size                  = size;
        e.remaining_size        = size;
        e.queue_ahead           = queue_ahead;
        e.queue_ahead_remaining = queue_ahead;
        e.queue_satisfied       = (queue_ahead <= 0.0);
        e.order_type            = type;
        e.placed_at_ns          = ts_ns;
        e.active                = true;

        orders_.emplace(oid, e);

        // Insert into the appropriate book level (FIFO — push_back)
        if (side == Side::kBid) {
            bid_book_[price].price = price;
            bid_book_[price].our_order_ids.push_back(oid);
        } else {
            ask_book_[price].price = price;
            ask_book_[price].our_order_ids.push_back(oid);
        }

        return oid;
    }

    bool cancel_order(uint64_t oid) {
        auto it = orders_.find(oid);
        if (it == orders_.end() || !it->second.active) return false;
        it->second.active = false;
        _remove_from_level(it->second);
        return true;
    }

    bool amend_price(uint64_t oid, double new_price) {
        auto it = orders_.find(oid);
        if (it == orders_.end() || !it->second.active) return false;

        OrderEntry& e = it->second;
        _remove_from_level(e);

        // Re-estimate queue at new price
        double new_queue = 0.0;
        if (e.side == Side::kBid) {
            auto bit = bid_book_.find(new_price);
            if (bit != bid_book_.end()) new_queue = bit->second.market_total_size * queue_position_frac_;
            bid_book_[new_price].price = new_price;
            bid_book_[new_price].our_order_ids.push_back(oid);
        } else {
            auto ait = ask_book_.find(new_price);
            if (ait != ask_book_.end()) new_queue = ait->second.market_total_size * queue_position_frac_;
            ask_book_[new_price].price = new_price;
            ask_book_[new_price].our_order_ids.push_back(oid);
        }

        e.price                 = new_price;
        e.queue_ahead           = new_queue;
        e.queue_ahead_remaining = new_queue;
        e.clamp_credit          = 0.0;
        e.queue_satisfied       = (new_queue <= 0.0);
        return true;
    }

    // ── Market trade arrives (from WS public trades feed) ─────────────────
    //
    // A market trade at `trade_price` for `trade_vol` means:
    //   • If trade is a BUY  (aggressor = buyer)  → hits the ASK side
    //   • If trade is a SELL (aggressor = seller) → hits the BID side
    //
    // We update queue_ahead_remaining for our orders on the hit side,
    // then check for fills.

    std::vector<FillResult> on_trade(double trade_price, double trade_vol,
                                     AggressorSide aggressor_side) {
        std::vector<FillResult> fills;

        if (aggressor_side == AggressorSide::kBuy) {
            // Buy aggressor hits ask side → our passive asks may fill
            _process_trades_on_side(ask_book_, trade_price, trade_vol,
                                    Side::kAsk, /*is_maker=*/true, fills);
        } else {
            // Sell aggressor hits bid side → our passive bids may fill
            _process_trades_on_side(bid_book_, trade_price, trade_vol,
                                    Side::kBid, /*is_maker=*/true, fills);
        }

        return fills;
    }

    // ── IOC / market order placement + immediate matching ─────────────────

    std::vector<FillResult> place_aggressive(Side side, double size,
                                              double limit_price,
                                              int64_t ts_ns = 0) {
        std::vector<FillResult> fills;
        double remaining = size;

        if (side == Side::kBid) {
            // Buy IOC: walk up the ask book
            for (auto it = ask_book_.begin();
                 it != ask_book_.end() && remaining > 0.0; ) {
                if (it->first > limit_price) break;
                double fill_qty = std::min(remaining, it->second.market_total_size);
                if (fill_qty > 0.0) {
                    double fee = fee_engine_.compute(fill_qty, it->first, false);
                    fee_engine_.record(fee, false);
                    fills.push_back({++next_order_id_, fill_qty, it->first,
                                     fee, -fill_qty * it->first - fee, false});
                    remaining -= fill_qty;
                    it->second.market_total_size -= fill_qty;
                    if (it->second.market_total_size <= 0.0 &&
                        it->second.our_order_ids.empty())
                        it = ask_book_.erase(it);
                    else ++it;
                } else ++it;
            }
        } else {
            // Sell IOC: walk down the bid book
            for (auto it = bid_book_.begin();
                 it != bid_book_.end() && remaining > 0.0; ) {
                if (it->first < limit_price) break;
                double fill_qty = std::min(remaining, it->second.market_total_size);
                if (fill_qty > 0.0) {
                    double fee = fee_engine_.compute(fill_qty, it->first, false);
                    fee_engine_.record(fee, false);
                    fills.push_back({++next_order_id_, fill_qty, it->first,
                                     fee, fill_qty * it->first - fee, false});
                    remaining -= fill_qty;
                    it->second.market_total_size -= fill_qty;
                    if (it->second.market_total_size <= 0.0 &&
                        it->second.our_order_ids.empty())
                        it = bid_book_.erase(it);
                    else ++it;
                } else ++it;
            }
        }
        return fills;
    }

    // ── Accessors ──────────────────────────────────────────────────────────

    [[nodiscard]] std::optional<double> best_bid() const noexcept {
        if (bid_book_.empty()) return std::nullopt;
        return bid_book_.begin()->first;
    }

    [[nodiscard]] std::optional<double> best_ask() const noexcept {
        if (ask_book_.empty()) return std::nullopt;
        return ask_book_.begin()->first;
    }

    [[nodiscard]] std::optional<double> spread() const noexcept {
        auto b = best_bid(); auto a = best_ask();
        if (!b || !a) return std::nullopt;
        return *a - *b;
    }

    [[nodiscard]] const OrderEntry* get_order(uint64_t oid) const {
        auto it = orders_.find(oid);
        return it != orders_.end() ? &it->second : nullptr;
    }

    [[nodiscard]] const FeeEngine& fee_engine() const noexcept { return fee_engine_; }

    [[nodiscard]] double queue_ahead_remaining(uint64_t oid) const noexcept {
        auto it = orders_.find(oid);
        return it != orders_.end() ? it->second.queue_ahead_remaining : 0.0;
    }

    [[nodiscard]] bool queue_satisfied(uint64_t oid) const noexcept {
        auto it = orders_.find(oid);
        return it != orders_.end() ? it->second.queue_satisfied : false;
    }

    // Snapshot of top N levels for diagnostics
    struct LevelSnapshot { double price; double market_size; size_t our_orders; };

    [[nodiscard]] std::vector<LevelSnapshot> bid_levels(int n = 5) const {
        std::vector<LevelSnapshot> out;
        int i = 0;
        for (auto& [p, lvl] : bid_book_) {
            if (i++ >= n) break;
            out.push_back({p, lvl.market_total_size, lvl.our_order_ids.size()});
        }
        return out;
    }

    [[nodiscard]] std::vector<LevelSnapshot> ask_levels(int n = 5) const {
        std::vector<LevelSnapshot> out;
        int i = 0;
        for (auto& [p, lvl] : ask_book_) {
            if (i++ >= n) break;
            out.push_back({p, lvl.market_total_size, lvl.our_order_ids.size()});
        }
        return out;
    }

    void reset_all() {
        bid_book_.clear(); ask_book_.clear(); orders_.clear();
        fee_engine_.total_maker_rebate = 0.0;
        fee_engine_.total_taker_fee    = 0.0;
        next_order_id_ = 0;
    }

private:
    // ── Core data structures ───────────────────────────────────────────────
    BidBook  bid_book_;                          // RB tree, descending price
    AskBook  ask_book_;                          // RB tree, ascending price
    std::unordered_map<uint64_t, OrderEntry> orders_;  // O(1) by order_id
    FeeEngine  fee_engine_;
    uint64_t   next_order_id_{0};
    double     queue_position_frac_{1.0};

    // ── Internal helpers ───────────────────────────────────────────────────

    void _remove_from_level(const OrderEntry& e) {
        if (e.side == Side::kBid) {
            auto it = bid_book_.find(e.price);
            if (it == bid_book_.end()) return;
            auto& q = it->second.our_order_ids;
            for (auto qi = q.begin(); qi != q.end(); ++qi) {
                if (*qi == e.order_id) { q.erase(qi); break; }
            }
            if (q.empty() && it->second.market_total_size <= 0.0)
                bid_book_.erase(it);
        } else {
            auto it = ask_book_.find(e.price);
            if (it == ask_book_.end()) return;
            auto& q = it->second.our_order_ids;
            for (auto qi = q.begin(); qi != q.end(); ++qi) {
                if (*qi == e.order_id) { q.erase(qi); break; }
            }
            if (q.empty() && it->second.market_total_size <= 0.0)
                ask_book_.erase(it);
        }
    }

    template<typename BookMap>
    void _update_level(BookMap& book, double price, double size) {
        auto it = book.find(price);
        if (it == book.end()) {
            if (size > 0.0) {
                book[price].price             = price;
                book[price].market_total_size = size;
            }
            return;
        }
        PriceLevel& lvl = it->second;
        lvl.market_total_size = size > 0.0 ? size : 0.0;
        for (uint64_t oid : lvl.our_order_ids) {
            auto oit = orders_.find(oid);
            if (oit == orders_.end() || !oit->second.active) continue;
            OrderEntry& e = oit->second;
            e.clamp_credit = 0.0;
            if (e.queue_ahead_remaining > lvl.market_total_size) {
                e.clamp_credit          = e.queue_ahead_remaining - lvl.market_total_size;
                e.queue_ahead_remaining = lvl.market_total_size;
            }
            if (e.queue_ahead_remaining <= 0.0) e.queue_satisfied = true;
        }
        if (lvl.market_total_size <= 0.0 && lvl.our_order_ids.empty())
            book.erase(it);
    }

    void _record_fill(OrderEntry& e, double fill_qty, double level_price,
                      Side our_side, bool is_maker,
                      std::vector<FillResult>& fills) {
        double fee = fee_engine_.compute(fill_qty, level_price, is_maker);
        fee_engine_.record(fee, is_maker);
        double pnl_delta = (our_side == Side::kBid)
            ? -fill_qty * level_price - fee   // bought: negative cash
            :  fill_qty * level_price - fee;  // sold:   positive cash
        fills.push_back({e.order_id, fill_qty, level_price, fee, pnl_delta,
                         is_maker});
        e.remaining_size -= fill_qty;
        if (e.remaining_size <= 0.0) e.active = false;
    }

    template<typename BookMap>
    void _process_trades_on_side(BookMap& book, double trade_price,
                                  double trade_vol, Side our_side,
                                  bool is_maker,
                                  std::vector<FillResult>& fills) {
        for (auto it = book.begin(); it != book.end(); ) {
            double level_price = it->first;
            PriceLevel& lvl    = it->second;

            // For bids: level_price >= trade_price means trade swept us
            // For asks: level_price <= trade_price means trade swept us
            bool swept = (our_side == Side::kBid)
                           ? (level_price >= trade_price)
                           : (level_price <= trade_price);
            if (!swept) break;
            bool through = level_price != trade_price;

            double our_filled = 0.0;  // volume absorbed by our own fills
            auto qit = lvl.our_order_ids.begin();
            while (qit != lvl.our_order_ids.end()) {
                auto oit = orders_.find(*qit);
                if (oit == orders_.end() || !oit->second.active) {
                    qit = lvl.our_order_ids.erase(qit);
                    continue;
                }
                OrderEntry& e = oit->second;
                double fill_qty = 0.0;
                if (through) {
                    // The print is beyond our price: the whole level traded.
                    e.queue_ahead_remaining = 0.0;
                    e.clamp_credit          = 0.0;
                    fill_qty                = e.remaining_size;
                } else {
                    double passing = trade_vol - our_filled;
                    if (passing <= 0.0) break;
                    double ahead = e.queue_ahead_remaining + e.clamp_credit;
                    double burn  = std::min(ahead, passing);
                    double credit_left = std::max(0.0, e.clamp_credit - passing);
                    e.queue_ahead_remaining = std::max(0.0, ahead - burn - credit_left);
                    e.clamp_credit          = credit_left;
                    fill_qty = std::min(e.remaining_size, passing - burn);
                }
                if (e.queue_ahead_remaining <= 0.0) e.queue_satisfied = true;
                if (fill_qty > 0.0) {
                    _record_fill(e, fill_qty, level_price, our_side, is_maker, fills);
                    our_filled += fill_qty;
                }
                if (!e.active) qit = lvl.our_order_ids.erase(qit);
                else ++qit;
            }

            if (lvl.market_total_size <= 0.0 && lvl.our_order_ids.empty())
                it = book.erase(it);
            else
                ++it;
            if (!through) break;  // the print's own level is the last one hit
        }
    }
};

}  // namespace mm
