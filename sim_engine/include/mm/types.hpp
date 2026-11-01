#pragma once

#include <cstdint>
#include <optional>
#include <string>

namespace mm {

enum class Side {
  kBid,
  kAsk,
};

inline std::string SideToString(const Side side) {
  return side == Side::kBid ? "bid" : "ask";
}

inline std::string SideToKrakenSpotType(const Side side) {
  return side == Side::kBid ? "buy" : "sell";
}

inline std::string SideToKrakenFuturesType(const Side side) {
  return side == Side::kBid ? "buy" : "sell";
}

struct OrderQuote {
  Side side{Side::kBid};
  double price{0.0};
  double size{0.0};
  bool post_only{true};
  std::string client_order_id{};
};

struct QuoteIntent {
  std::optional<OrderQuote> bid{};
  std::optional<OrderQuote> ask{};
  bool cancel_all{false};
};

struct MarketSnapshot {
  int64_t ts_ms{0};
  double mid{0.0};
  double trade_sign{0.0};
  double cancel_burst{0.0};
  double depth_collapse{0.0};
};

struct GatewayResponse {
  bool ok{false};
  bool sent{false};
  long status_code{0};
  double latency_ms{0.0};
  std::string body{};
  std::string error{};
  std::string order_id{};  // filled on successful SubmitQuote
};

}  // namespace mm
