// matching_engine_bindings.cpp
//
// pybind11 bindings exposing MatchingEngine, FeeEngine, and
// QueuePositionEstimator to Python.  The Python side lives in
// Bybit_MM/bybit_order_manager.py which replaces its naive
// price-crossing fill logic with calls into this module.
//
// Build:
//   pip install pybind11
//   cmake -B build -DBUILD_PYTHON_BINDINGS=ON && cmake --build build

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/functional.h>
#include "mm/matching_engine.hpp"
#include "mm/types.hpp"

namespace py = pybind11;
using namespace mm;

PYBIND11_MODULE(mm_matching_engine, m) {
    m.doc() = "Bybit matching engine simulator — price-time priority, "
              "queue position tracking, and fee engine.";

    // ── Side enum ──────────────────────────────────────────────────────────
    py::enum_<Side>(m, "Side")
        .value("BID", Side::kBid)
        .value("ASK", Side::kAsk)
        .export_values();

    // ── OrderType enum ─────────────────────────────────────────────────────
    py::enum_<OrderType>(m, "OrderType")
        .value("POST_ONLY", OrderType::kPostOnly)
        .value("IOC",       OrderType::kIOC)
        .value("MARKET",    OrderType::kMarket)
        .export_values();

    // ── AggressorSide enum ─────────────────────────────────────────────────
    py::enum_<AggressorSide>(m, "AggressorSide")
        .value("BUY",  AggressorSide::kBuy)
        .value("SELL", AggressorSide::kSell)
        .export_values();

    // ── FillResult ─────────────────────────────────────────────────────────
    py::class_<FillResult>(m, "FillResult")
        .def_readonly("order_id",      &FillResult::order_id)
        .def_readonly("filled_qty",    &FillResult::filled_qty)
        .def_readonly("fill_price",    &FillResult::fill_price)
        .def_readonly("fee",           &FillResult::fee)
        .def_readonly("net_pnl_delta", &FillResult::net_pnl_delta)
        .def_readonly("is_maker",      &FillResult::is_maker)
        .def("__repr__", [](const FillResult& r) {
            return "<FillResult oid=" + std::to_string(r.order_id)
                 + " qty=" + std::to_string(r.filled_qty)
                 + " @"    + std::to_string(r.fill_price)
                 + " fee="  + std::to_string(r.fee)
                 + " maker=" + std::string(r.is_maker ? "Y" : "N") + ">";
        });

    // ── FeeConfig ──────────────────────────────────────────────────────────
    py::class_<FeeConfig>(m, "FeeConfig")
        .def(py::init<>())
        .def(py::init<double, double>(),
             py::arg("maker_rate") = -0.00001,
             py::arg("taker_rate") = +0.00006)
        .def_readwrite("maker_rate", &FeeConfig::maker_rate)
        .def_readwrite("taker_rate", &FeeConfig::taker_rate);

    // ── FeeEngine ──────────────────────────────────────────────────────────
    py::class_<FeeEngine>(m, "FeeEngine")
        .def(py::init<FeeConfig>(), py::arg("cfg") = FeeConfig{})
        .def("compute", &FeeEngine::compute,
             py::arg("qty"), py::arg("price"), py::arg("is_maker"),
             "Returns signed fee: negative = rebate earned, positive = fee paid")
        .def_readonly("total_maker_rebate", &FeeEngine::total_maker_rebate)
        .def_readonly("total_taker_fee",    &FeeEngine::total_taker_fee)
        .def("net_fee_pnl", [](const FeeEngine& fe) {
            return fe.total_maker_rebate - fe.total_taker_fee;
        }, "total_maker_rebate - total_taker_fee (positive = net earned)");

    // ── OrderEntry (read-only view) ────────────────────────────────────────
    py::class_<OrderEntry>(m, "OrderEntry")
        .def_readonly("order_id",               &OrderEntry::order_id)
        .def_readonly("price",                  &OrderEntry::price)
        .def_readonly("size",                   &OrderEntry::size)
        .def_readonly("remaining_size",         &OrderEntry::remaining_size)
        .def_readonly("queue_ahead",            &OrderEntry::queue_ahead)
        .def_readonly("queue_ahead_remaining",  &OrderEntry::queue_ahead_remaining)
        .def_readonly("queue_satisfied",        &OrderEntry::queue_satisfied)
        .def_readonly("active",                 &OrderEntry::active)
        .def("fill_ratio", [](const OrderEntry& e) {
            if (e.size <= 0.0) return 0.0;
            return (e.size - e.remaining_size) / e.size;
        });

    // ── LevelSnapshot ─────────────────────────────────────────────────────
    py::class_<MatchingEngine::LevelSnapshot>(m, "LevelSnapshot")
        .def_readonly("price",       &MatchingEngine::LevelSnapshot::price)
        .def_readonly("market_size", &MatchingEngine::LevelSnapshot::market_size)
        .def_readonly("our_orders",  &MatchingEngine::LevelSnapshot::our_orders);

    // ── MatchingEngine ─────────────────────────────────────────────────────
    py::class_<MatchingEngine>(m, "MatchingEngine")
        .def(py::init<FeeConfig>(), py::arg("fee_cfg") = FeeConfig{})

        // Book updates (feed from live WS)
        .def("update_bid_level", &MatchingEngine::update_bid_level,
             py::arg("price"), py::arg("size"),
             "Update or remove a bid level from the live book.")
        .def("update_ask_level", &MatchingEngine::update_ask_level,
             py::arg("price"), py::arg("size"),
             "Update or remove an ask level from the live book.")
        .def("reset_book", &MatchingEngine::reset_book,
             py::arg("bids"), py::arg("asks"),
             "Full book snapshot reset. bids/asks = list of [price, size] pairs.")

        // Order management
        .def("place_order", &MatchingEngine::place_order,
             py::arg("side"), py::arg("price"), py::arg("size"),
             py::arg("order_type") = OrderType::kPostOnly,
             py::arg("ts_ns") = 0,
             "Place a simulated order. Returns order_id (uint64). "
             "queue_ahead is auto-estimated from current book depth.")
        .def("cancel_order", &MatchingEngine::cancel_order,
             py::arg("order_id"),
             "Cancel an order by ID. Returns True if found and cancelled.")
        .def("amend_price", &MatchingEngine::amend_price,
             py::arg("order_id"), py::arg("new_price"),
             "Amend order price; re-estimates queue_ahead at new level.")

        // Market event processing
        .def("on_trade", &MatchingEngine::on_trade,
             py::arg("trade_price"), py::arg("trade_vol"),
             py::arg("aggressor_side"),  // AggressorSide.BUY or SELL
             "Process a market trade. Returns list of FillResult for our orders.")
        .def("place_aggressive", &MatchingEngine::place_aggressive,
             py::arg("side"), py::arg("size"), py::arg("limit_price"),
             py::arg("ts_ns") = 0,
             "IOC/market order: sweep opposite book, return fills immediately.")

        // Accessors
        .def("best_bid",  &MatchingEngine::best_bid)
        .def("best_ask",  &MatchingEngine::best_ask)
        .def("spread",    &MatchingEngine::spread)
        .def("get_order", &MatchingEngine::get_order,
             py::arg("order_id"), py::return_value_policy::reference_internal)
        .def("queue_ahead_remaining", &MatchingEngine::queue_ahead_remaining,
             py::arg("order_id"))
        .def("queue_satisfied", &MatchingEngine::queue_satisfied,
             py::arg("order_id"))
        .def("bid_levels", &MatchingEngine::bid_levels,
             py::arg("n") = 5,
             "Top N bid levels as LevelSnapshot list.")
        .def("ask_levels", &MatchingEngine::ask_levels,
             py::arg("n") = 5,
             "Top N ask levels as LevelSnapshot list.")
        .def("fee_engine", &MatchingEngine::fee_engine,
             py::return_value_policy::reference_internal)
        .def("reset_all", &MatchingEngine::reset_all,
             "Clear all book data, orders, and fee accumulators.");
}
