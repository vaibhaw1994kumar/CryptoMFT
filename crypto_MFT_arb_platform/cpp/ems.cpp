#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <limits>
#include <map>
#include <optional>
#include <queue>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

namespace py = pybind11;

namespace hft {

constexpr std::size_t kMaximumScheduleSlices = 1'000'000;

enum class Side { Buy, Sell };
enum class OrderType { Market, Limit, PostOnly };
enum class PlanState {
  Created,
  RiskReserved,
  OrdersCreated,
  LegsDispatched,
  Executing,
  PartiallyFilled,
  Filled,
  HedgeRequired,
  Hedging,
  RecoveryRequired,
  CancelRemaining,
  UnwindRequired,
  Unwinding,
  Reconciling,
  Closed,
  FailedSafe,
};

struct VenueQuote {
  std::string venue;
  std::string symbol;
  double bid{0.0};
  double ask{0.0};
  double bid_size{0.0};
  double ask_size{0.0};
  double fee_bps{0.0};
  double latency_us{0.0};
  bool healthy{true};
};

struct RouteSlice {
  std::string venue;
  double quantity{0.0};
  double reference_price{0.0};
  double expected_fee{0.0};
  double score{0.0};
};

struct ScheduledSlice {
  std::int64_t timestamp_ns{0};
  double quantity{0.0};
};

struct Leg {
  std::string symbol;
  Side side{Side::Buy};
  double quantity{0.0};
  double reference_price{0.0};
  std::vector<std::string> venue_preferences;
  bool hedge{false};
};

struct MultiLegPlan {
  std::string plan_id;
  std::vector<Leg> legs;
  PlanState state{PlanState::Created};
  bool risk_reserved{false};
  std::uint64_t risk_book_token{0};
};

struct Event {
  std::int64_t timestamp_ns{0};
  std::uint64_t sequence{0};
  std::string event_type;
  std::string payload;
};

void require_identifier(const std::string& value, const char* name) {
  if (value.empty()) {
    throw std::invalid_argument(std::string(name) + " must not be empty");
  }
}

void require_finite_positive(double value, const char* name) {
  if (!std::isfinite(value) || !(value > 0.0)) {
    throw std::invalid_argument(std::string(name) +
                                " must be finite and positive");
  }
}

void require_finite_non_negative(double value, const char* name) {
  if (!std::isfinite(value) || value < 0.0) {
    throw std::invalid_argument(std::string(name) +
                                " must be finite and non-negative");
  }
}

void validate_quote(const VenueQuote& quote) {
  require_identifier(quote.venue, "venue");
  require_identifier(quote.symbol, "symbol");
  require_finite_positive(quote.bid, "bid");
  require_finite_positive(quote.ask, "ask");
  require_finite_non_negative(quote.bid_size, "bid size");
  require_finite_non_negative(quote.ask_size, "ask size");
  require_finite_non_negative(quote.fee_bps, "fee bps");
  require_finite_non_negative(quote.latency_us, "latency");
  if (quote.bid > quote.ask) {
    throw std::invalid_argument("quote bid must not exceed ask");
  }
}

void validate_leg(const Leg& leg) {
  require_identifier(leg.symbol, "leg symbol");
  require_finite_positive(leg.quantity, "leg quantity");
  require_finite_positive(leg.reference_price, "leg reference price");
  std::set<std::string> venues;
  for (const auto& venue : leg.venue_preferences) {
    require_identifier(venue, "venue preference");
    if (!venues.insert(venue).second) {
      throw std::invalid_argument("venue preferences must be unique");
    }
  }
}

class SmartOrderRouter {
 public:
  explicit SmartOrderRouter(double latency_penalty_bps_per_ms = 0.0)
      : latency_penalty_bps_per_ms_(latency_penalty_bps_per_ms) {
    require_finite_non_negative(latency_penalty_bps_per_ms_,
                                "latency penalty");
  }

  std::vector<RouteSlice> route(
      Side side, double quantity,
      const std::vector<VenueQuote>& quotes) const {
    return route_with_preferences(side, quantity, quotes, {});
  }

  std::vector<RouteSlice> route_with_preferences(
      Side side, double quantity, const std::vector<VenueQuote>& quotes,
      const std::vector<std::string>& venue_preferences) const {
    require_finite_positive(quantity, "route quantity");

    std::unordered_map<std::string, std::size_t> preference_rank;
    for (std::size_t index = 0; index < venue_preferences.size(); ++index) {
      require_identifier(venue_preferences[index], "venue preference");
      if (!preference_rank.emplace(venue_preferences[index], index).second) {
        throw std::invalid_argument("venue preferences must be unique");
      }
    }

    struct Candidate {
      VenueQuote quote;
      double available;
      double price;
      double score;
      std::size_t preference_rank;
    };

    std::vector<Candidate> candidates;
    std::set<std::string> venues;
    std::optional<std::string> symbol;
    long double total_available = 0.0L;
    for (const auto& quote : quotes) {
      validate_quote(quote);
      if (!venues.insert(quote.venue).second) {
        throw std::invalid_argument("router quotes must have unique venues");
      }
      if (symbol && quote.symbol != *symbol) {
        throw std::invalid_argument(
            "router quotes must refer to one symbol");
      }
      symbol = quote.symbol;
      if (!quote.healthy) {
        continue;
      }

      const double available =
          side == Side::Buy ? quote.ask_size : quote.bid_size;
      if (available == 0.0) {
        continue;
      }
      const double price = side == Side::Buy ? quote.ask : quote.bid;
      const double penalty_bps =
          quote.fee_bps +
          quote.latency_us * latency_penalty_bps_per_ms_ / 1000.0;
      if (!std::isfinite(penalty_bps)) {
        throw std::overflow_error("routing penalty overflow");
      }
      const double penalty_fraction = penalty_bps / 10000.0;
      const double score =
          side == Side::Buy ? price * (1.0 + penalty_fraction)
                            : price * (1.0 - penalty_fraction);
      if (!std::isfinite(score) || !(score > 0.0)) {
        throw std::invalid_argument(
            "fees and latency leave no positive execution value");
      }
      const auto rank = preference_rank.find(quote.venue);
      candidates.push_back(
          {quote, available, price, score,
           rank == preference_rank.end()
               ? std::numeric_limits<std::size_t>::max()
               : rank->second});
      total_available += static_cast<long double>(available);
    }

    if (total_available < static_cast<long double>(quantity)) {
      throw std::runtime_error("insufficient healthy venue liquidity");
    }

    std::sort(candidates.begin(), candidates.end(),
              [side](const Candidate& left, const Candidate& right) {
                if (left.score != right.score) {
                  return side == Side::Buy ? left.score < right.score
                                           : left.score > right.score;
                }
                if (left.preference_rank != right.preference_rank) {
                  return left.preference_rank < right.preference_rank;
                }
                return left.quote.venue < right.quote.venue;
              });

    std::vector<RouteSlice> result;
    double remaining = quantity;
    for (const auto& candidate : candidates) {
      if (remaining == 0.0) {
        break;
      }
      const double routed = std::min(remaining, candidate.available);
      const double notional = routed * candidate.price;
      const double expected_fee =
          notional * candidate.quote.fee_bps / 10000.0;
      if (!std::isfinite(notional) || !std::isfinite(expected_fee)) {
        throw std::overflow_error("route notional overflow");
      }
      result.push_back({candidate.quote.venue, routed, candidate.price,
                        expected_fee, candidate.score});
      remaining -= routed;
    }
    if (remaining != 0.0) {
      throw std::logic_error("router quantity conservation failure");
    }
    return result;
  }

 private:
  double latency_penalty_bps_per_ms_;
};

class Scheduler {
 public:
  static std::vector<ScheduledSlice> twap(std::int64_t start_ns,
                                          std::int64_t end_ns,
                                          double total_quantity,
                                          std::size_t slice_count) {
    validate_schedule(start_ns, end_ns, total_quantity, slice_count);
    return weighted(start_ns, end_ns, total_quantity,
                    std::vector<double>(slice_count, 1.0));
  }

  static std::vector<ScheduledSlice> vwap(
      std::int64_t start_ns, std::int64_t end_ns, double total_quantity,
      const std::vector<double>& volume_weights) {
    validate_schedule(start_ns, end_ns, total_quantity,
                      volume_weights.size());
    return weighted(start_ns, end_ns, total_quantity, volume_weights);
  }

 private:
  static void validate_schedule(std::int64_t start_ns, std::int64_t end_ns,
                                double total_quantity,
                                std::size_t slice_count) {
    if (start_ns < 0 || end_ns < 0) {
      throw std::invalid_argument(
          "schedule timestamps must be non-negative");
    }
    if (end_ns < start_ns) {
      throw std::invalid_argument("schedule end precedes start");
    }
    require_finite_positive(total_quantity, "total quantity");
    if (slice_count == 0) {
      throw std::invalid_argument("schedule requires at least one slice");
    }
    if (slice_count > kMaximumScheduleSlices) {
      throw std::invalid_argument("schedule has too many slices");
    }
  }

  static std::int64_t timestamp_at(std::int64_t start_ns,
                                   std::int64_t end_ns, std::size_t index,
                                   std::size_t count) {
    if (count == 1) {
      return start_ns;
    }
    const auto span = static_cast<std::uint64_t>(end_ns - start_ns);
    const auto intervals = static_cast<std::uint64_t>(count - 1);
    const auto position = static_cast<std::uint64_t>(index);
    const auto quotient = span / intervals;
    const auto remainder = span % intervals;
    const auto offset =
        quotient * position + (remainder * position) / intervals;
    return start_ns + static_cast<std::int64_t>(offset);
  }

  static std::vector<ScheduledSlice> weighted(
      std::int64_t start_ns, std::int64_t end_ns, double total_quantity,
      const std::vector<double>& weights) {
    double maximum_weight = 0.0;
    std::size_t last_positive = 0;
    bool has_positive = false;
    for (std::size_t index = 0; index < weights.size(); ++index) {
      require_finite_non_negative(weights[index], "VWAP weight");
      if (weights[index] > 0.0) {
        maximum_weight = std::max(maximum_weight, weights[index]);
        last_positive = index;
        has_positive = true;
      }
    }
    if (!has_positive) {
      throw std::invalid_argument(
          "VWAP weights require at least one positive value");
    }

    long double normalized_sum = 0.0L;
    for (const double weight : weights) {
      normalized_sum +=
          static_cast<long double>(weight / maximum_weight);
    }

    std::vector<ScheduledSlice> result;
    result.reserve(weights.size());
    long double cumulative_weight = 0.0L;
    double allocated = 0.0;
    for (std::size_t index = 0; index < weights.size(); ++index) {
      double quantity = 0.0;
      if (weights[index] > 0.0) {
        if (index == last_positive) {
          quantity = total_quantity - allocated;
        } else {
          cumulative_weight +=
              static_cast<long double>(weights[index] / maximum_weight);
          const double target = static_cast<double>(
              static_cast<long double>(total_quantity) *
              cumulative_weight / normalized_sum);
          quantity = target - allocated;
        }
      }
      if (!std::isfinite(quantity) || quantity < 0.0) {
        throw std::logic_error("schedule quantity allocation failure");
      }
      allocated += quantity;
      result.push_back(
          {timestamp_at(start_ns, end_ns, index, weights.size()), quantity});
    }
    if (allocated != total_quantity) {
      throw std::logic_error("schedule quantity conservation failure");
    }
    return result;
  }
};

class RiskReservationBook {
 public:
  explicit RiskReservationBook(double maximum_reserved_notional)
      : owner_(std::this_thread::get_id()),
        identity_token_(allocate_identity_token()),
        maximum_reserved_notional_(maximum_reserved_notional) {
    require_finite_positive(maximum_reserved_notional_,
                            "maximum reserved notional");
  }

  bool reserve(const std::string& plan_id, const std::vector<Leg>& legs) {
    assert_owner();
    require_identifier(plan_id, "plan id");
    if (legs.empty()) {
      throw std::invalid_argument(
          "risk reservation requires at least one leg");
    }

    long double notional = 0.0L;
    std::ostringstream fingerprint;
    fingerprint << std::hexfloat;
    for (const auto& leg : legs) {
      validate_leg(leg);
      notional += static_cast<long double>(leg.quantity) *
                  static_cast<long double>(leg.reference_price);
      fingerprint << leg.symbol << ':' << static_cast<int>(leg.side) << ':'
                  << leg.quantity << ':' << leg.reference_price << ':'
                  << leg.hedge;
      for (const auto& venue : leg.venue_preferences) {
        fingerprint << ':' << venue;
      }
      fingerprint << ';';
    }
    if (notional >
        static_cast<long double>(std::numeric_limits<double>::max())) {
      throw std::overflow_error("risk reservation notional overflow");
    }
    const double required = static_cast<double>(notional);
    const std::string key = fingerprint.str();
    const auto existing = reservations_.find(plan_id);
    if (existing != reservations_.end()) {
      if (existing->second.fingerprint != key) {
        throw std::runtime_error(
            "plan id already reserves a different set of legs");
      }
      return true;
    }
    if (required > maximum_reserved_notional_ - reserved_notional_) {
      return false;
    }
    reservations_.emplace(plan_id, Reservation{required, key});
    reserved_notional_ += required;
    return true;
  }

  bool release(const std::string& plan_id) {
    assert_owner();
    require_identifier(plan_id, "plan id");
    const auto iterator = reservations_.find(plan_id);
    if (iterator == reservations_.end()) {
      return false;
    }
    reserved_notional_ -= iterator->second.notional;
    if (reserved_notional_ < 0.0) {
      reserved_notional_ = 0.0;
    }
    reservations_.erase(iterator);
    return true;
  }

  bool is_reserved(const std::string& plan_id) const {
    assert_owner();
    return reservations_.contains(plan_id);
  }

  double reserved_notional() const {
    assert_owner();
    return reserved_notional_;
  }

  double available_notional() const {
    assert_owner();
    return maximum_reserved_notional_ - reserved_notional_;
  }

  std::uint64_t identity_token() const {
    assert_owner();
    return identity_token_;
  }

 private:
  struct Reservation {
    double notional;
    std::string fingerprint;
  };

  static std::uint64_t allocate_identity_token() {
    auto candidate = next_identity_token_.load(std::memory_order_relaxed);
    while (true) {
      if (candidate == std::numeric_limits<std::uint64_t>::max()) {
        throw std::overflow_error("risk book identity tokens exhausted");
      }
      if (next_identity_token_.compare_exchange_weak(
              candidate, candidate + 1, std::memory_order_relaxed,
              std::memory_order_relaxed)) {
        return candidate;
      }
    }
  }

  void assert_owner() const {
    if (std::this_thread::get_id() != owner_) {
      throw std::runtime_error(
          "risk book accessed outside its owning thread");
    }
  }

  inline static std::atomic<std::uint64_t> next_identity_token_{1};
  std::thread::id owner_;
  const std::uint64_t identity_token_;
  double maximum_reserved_notional_;
  double reserved_notional_{0.0};
  std::unordered_map<std::string, Reservation> reservations_;
};

struct EventCompare {
  bool operator()(const Event& left, const Event& right) const {
    if (left.timestamp_ns != right.timestamp_ns) {
      return left.timestamp_ns > right.timestamp_ns;
    }
    return left.sequence > right.sequence;
  }
};

class SingleLoop {
 public:
  SingleLoop() : owner_(std::this_thread::get_id()) {}

  std::uint64_t enqueue(std::int64_t timestamp_ns,
                        const std::string& event_type,
                        const std::string& payload = "") {
    assert_owner();
    if (timestamp_ns < 0) {
      throw std::invalid_argument(
          "event timestamp must be non-negative");
    }
    require_identifier(event_type, "event type");
    if (next_sequence_ == std::numeric_limits<std::uint64_t>::max()) {
      throw std::overflow_error("event sequence exhausted");
    }
    const auto sequence = next_sequence_++;
    events_.push({timestamp_ns, sequence, event_type, payload});
    return sequence;
  }

  std::optional<Event> pop() {
    assert_owner();
    if (events_.empty()) {
      return std::nullopt;
    }
    Event event = events_.top();
    events_.pop();
    return event;
  }

  std::vector<Event> drain_until(std::int64_t timestamp_ns) {
    assert_owner();
    if (timestamp_ns < 0) {
      throw std::invalid_argument(
          "drain timestamp must be non-negative");
    }
    std::vector<Event> drained;
    while (!events_.empty() && events_.top().timestamp_ns <= timestamp_ns) {
      drained.push_back(events_.top());
      events_.pop();
    }
    return drained;
  }

  std::size_t size() const {
    assert_owner();
    return events_.size();
  }

 private:
  void assert_owner() const {
    if (std::this_thread::get_id() != owner_) {
      throw std::runtime_error(
          "EMS single loop accessed from a different thread");
    }
  }

  std::thread::id owner_;
  std::uint64_t next_sequence_{0};
  std::priority_queue<Event, std::vector<Event>, EventCompare> events_;
};

bool transition_allowed(PlanState from, PlanState to) {
  switch (from) {
    case PlanState::Created:
      return to == PlanState::RiskReserved ||
             to == PlanState::FailedSafe;
    case PlanState::RiskReserved:
      return to == PlanState::OrdersCreated ||
             to == PlanState::RecoveryRequired ||
             to == PlanState::FailedSafe;
    case PlanState::OrdersCreated:
      return to == PlanState::LegsDispatched ||
             to == PlanState::RecoveryRequired ||
             to == PlanState::CancelRemaining ||
             to == PlanState::FailedSafe;
    case PlanState::LegsDispatched:
      return to == PlanState::Executing ||
             to == PlanState::RecoveryRequired ||
             to == PlanState::CancelRemaining ||
             to == PlanState::FailedSafe;
    case PlanState::Executing:
      return to == PlanState::PartiallyFilled ||
             to == PlanState::Filled ||
             to == PlanState::HedgeRequired ||
             to == PlanState::RecoveryRequired ||
             to == PlanState::CancelRemaining ||
             to == PlanState::FailedSafe;
    case PlanState::PartiallyFilled:
      return to == PlanState::Executing ||
             to == PlanState::Filled ||
             to == PlanState::HedgeRequired ||
             to == PlanState::RecoveryRequired ||
             to == PlanState::CancelRemaining ||
             to == PlanState::FailedSafe;
    case PlanState::Filled:
      return to == PlanState::Reconciling || to == PlanState::Closed;
    case PlanState::HedgeRequired:
      return to == PlanState::Hedging ||
             to == PlanState::UnwindRequired ||
             to == PlanState::FailedSafe;
    case PlanState::Hedging:
      return to == PlanState::Filled ||
             to == PlanState::RecoveryRequired ||
             to == PlanState::UnwindRequired ||
             to == PlanState::FailedSafe;
    case PlanState::RecoveryRequired:
      return to == PlanState::CancelRemaining ||
             to == PlanState::UnwindRequired ||
             to == PlanState::Reconciling ||
             to == PlanState::FailedSafe;
    case PlanState::CancelRemaining:
      return to == PlanState::UnwindRequired ||
             to == PlanState::Reconciling ||
             to == PlanState::Closed ||
             to == PlanState::FailedSafe;
    case PlanState::UnwindRequired:
      return to == PlanState::Unwinding ||
             to == PlanState::FailedSafe;
    case PlanState::Unwinding:
      return to == PlanState::Reconciling ||
             to == PlanState::RecoveryRequired ||
             to == PlanState::FailedSafe;
    case PlanState::Reconciling:
      return to == PlanState::Closed ||
             to == PlanState::HedgeRequired ||
             to == PlanState::RecoveryRequired ||
             to == PlanState::FailedSafe;
    case PlanState::Closed:
    case PlanState::FailedSafe:
      return false;
  }
  return false;
}

class ExecutionManagementSystem {
 public:
  explicit ExecutionManagementSystem(
      double latency_penalty_bps_per_ms = 0.0)
      : owner_(std::this_thread::get_id()),
        router_(latency_penalty_bps_per_ms) {}

  void update_quote(const VenueQuote& quote) {
    assert_owner();
    validate_quote(quote);
    quotes_[quote.symbol][quote.venue] = quote;
  }

  std::vector<RouteSlice> route(const std::string& symbol, Side side,
                                double quantity) const {
    assert_owner();
    require_identifier(symbol, "symbol");
    const auto symbol_iterator = quotes_.find(symbol);
    if (symbol_iterator == quotes_.end()) {
      throw std::runtime_error("no venue quotes for symbol");
    }
    return router_.route(side, quantity,
                         quote_vector(symbol_iterator->second));
  }

  void create_plan(const std::string& plan_id, const std::vector<Leg>& legs) {
    assert_owner();
    require_identifier(plan_id, "plan id");
    if (plans_.contains(plan_id)) {
      throw std::runtime_error("multi-leg plan already exists");
    }
    if (legs.size() < 2) {
      throw std::invalid_argument("multi-leg plan requires at least two legs");
    }
    for (const auto& leg : legs) {
      validate_leg(leg);
    }
    plans_.emplace(
        plan_id,
        MultiLegPlan{plan_id, legs, PlanState::Created, false, 0});
  }

  bool reserve_plan(const std::string& plan_id, RiskReservationBook& risk) {
    assert_owner();
    auto& plan = require_plan(plan_id);
    if (plan.state != PlanState::Created) {
      throw std::runtime_error("plan is not in CREATED state");
    }
    if (!risk.reserve(plan_id, plan.legs)) {
      plan.state = PlanState::FailedSafe;
      return false;
    }
    plan.state = PlanState::RiskReserved;
    plan.risk_reserved = true;
    plan.risk_book_token = risk.identity_token();
    return true;
  }

  std::vector<std::vector<RouteSlice>> route_plan(
      const std::string& plan_id) {
    assert_owner();
    auto& plan = require_plan(plan_id);
    if (plan.state != PlanState::RiskReserved &&
        plan.state != PlanState::OrdersCreated) {
      throw std::runtime_error("plan risk must be reserved before routing");
    }

    if (plan.state == PlanState::RiskReserved) {
      plan.state = PlanState::OrdersCreated;
    }
    std::vector<std::vector<RouteSlice>> routes;
    routes.reserve(plan.legs.size());
    try {
      for (const auto& leg : plan.legs) {
        const auto symbol_iterator = quotes_.find(leg.symbol);
        if (symbol_iterator == quotes_.end()) {
          throw std::runtime_error("no venue quotes for leg symbol");
        }
        routes.push_back(router_.route_with_preferences(
            leg.side, leg.quantity, quote_vector(symbol_iterator->second),
            leg.venue_preferences));
      }
    } catch (...) {
      plan.state = PlanState::RecoveryRequired;
      throw;
    }
    plan.state = PlanState::LegsDispatched;
    return routes;
  }

  void transition_plan(const std::string& plan_id, PlanState state) {
    assert_owner();
    auto& plan = require_plan(plan_id);
    if (!transition_allowed(plan.state, state)) {
      throw std::runtime_error("invalid multi-leg plan state transition");
    }
    plan.state = state;
  }

  void recover_plan(const std::string& plan_id, PlanState recovery_state) {
    assert_owner();
    auto& plan = require_plan(plan_id);
    if (plan.state != PlanState::RecoveryRequired) {
      throw std::runtime_error(
          "plan is not in RECOVERY_REQUIRED state");
    }
    if (!transition_allowed(plan.state, recovery_state)) {
      throw std::invalid_argument("invalid recovery state");
    }
    plan.state = recovery_state;
  }

  void release_plan(const std::string& plan_id, RiskReservationBook& risk) {
    assert_owner();
    auto& plan = require_plan(plan_id);
    if (plan.state != PlanState::Closed &&
        plan.state != PlanState::FailedSafe) {
      throw std::runtime_error(
          "risk can only be released for a terminal plan");
    }
    if (!plan.risk_reserved) {
      return;
    }
    if (plan.risk_book_token != risk.identity_token()) {
      throw std::runtime_error(
          "plan must be released through its originating risk book");
    }
    if (!risk.release(plan_id)) {
      throw std::runtime_error(
          "plan reservation is not present in this risk book");
    }
    plan.risk_reserved = false;
    plan.risk_book_token = 0;
  }

  MultiLegPlan plan(const std::string& plan_id) const {
    assert_owner();
    const auto iterator = plans_.find(plan_id);
    if (iterator == plans_.end()) {
      throw std::runtime_error("multi-leg plan does not exist");
    }
    return iterator->second;
  }

  SingleLoop& loop() {
    assert_owner();
    return loop_;
  }

 private:
  using VenueMap = std::map<std::string, VenueQuote>;

  static std::vector<VenueQuote> quote_vector(const VenueMap& quotes) {
    std::vector<VenueQuote> result;
    result.reserve(quotes.size());
    for (const auto& [venue, quote] : quotes) {
      (void)venue;
      result.push_back(quote);
    }
    return result;
  }

  void assert_owner() const {
    if (std::this_thread::get_id() != owner_) {
      throw std::runtime_error(
          "EMS accessed outside its owning single-loop thread");
    }
  }

  MultiLegPlan& require_plan(const std::string& plan_id) {
    const auto iterator = plans_.find(plan_id);
    if (iterator == plans_.end()) {
      throw std::runtime_error("multi-leg plan does not exist");
    }
    return iterator->second;
  }

  std::thread::id owner_;
  SmartOrderRouter router_;
  std::map<std::string, VenueMap> quotes_;
  std::map<std::string, MultiLegPlan> plans_;
  SingleLoop loop_;
};

}  // namespace hft

PYBIND11_MODULE(_ems, module) {
  module.doc() =
      "Native single-owner execution management, scheduling, and routing core";

  py::enum_<hft::Side>(module, "Side")
      .value("BUY", hft::Side::Buy)
      .value("SELL", hft::Side::Sell);
  py::enum_<hft::OrderType>(module, "OrderType")
      .value("MARKET", hft::OrderType::Market)
      .value("LIMIT", hft::OrderType::Limit)
      .value("POST_ONLY", hft::OrderType::PostOnly);
  py::enum_<hft::PlanState>(module, "PlanState")
      .value("CREATED", hft::PlanState::Created)
      .value("RISK_RESERVED", hft::PlanState::RiskReserved)
      .value("ORDERS_CREATED", hft::PlanState::OrdersCreated)
      .value("LEGS_DISPATCHED", hft::PlanState::LegsDispatched)
      .value("EXECUTING", hft::PlanState::Executing)
      .value("PARTIALLY_FILLED", hft::PlanState::PartiallyFilled)
      .value("FILLED", hft::PlanState::Filled)
      .value("HEDGE_REQUIRED", hft::PlanState::HedgeRequired)
      .value("HEDGING", hft::PlanState::Hedging)
      .value("RECOVERY_REQUIRED", hft::PlanState::RecoveryRequired)
      .value("CANCEL_REMAINING", hft::PlanState::CancelRemaining)
      .value("UNWIND_REQUIRED", hft::PlanState::UnwindRequired)
      .value("UNWINDING", hft::PlanState::Unwinding)
      .value("RECONCILING", hft::PlanState::Reconciling)
      .value("CLOSED", hft::PlanState::Closed)
      .value("FAILED_SAFE", hft::PlanState::FailedSafe);

  py::class_<hft::VenueQuote>(module, "VenueQuote")
      .def(py::init<>())
      .def_readwrite("venue", &hft::VenueQuote::venue)
      .def_readwrite("symbol", &hft::VenueQuote::symbol)
      .def_readwrite("bid", &hft::VenueQuote::bid)
      .def_readwrite("ask", &hft::VenueQuote::ask)
      .def_readwrite("bid_size", &hft::VenueQuote::bid_size)
      .def_readwrite("ask_size", &hft::VenueQuote::ask_size)
      .def_readwrite("fee_bps", &hft::VenueQuote::fee_bps)
      .def_readwrite("latency_us", &hft::VenueQuote::latency_us)
      .def_readwrite("healthy", &hft::VenueQuote::healthy);
  py::class_<hft::RouteSlice>(module, "RouteSlice")
      .def_readonly("venue", &hft::RouteSlice::venue)
      .def_readonly("quantity", &hft::RouteSlice::quantity)
      .def_readonly("reference_price", &hft::RouteSlice::reference_price)
      .def_readonly("expected_fee", &hft::RouteSlice::expected_fee)
      .def_readonly("score", &hft::RouteSlice::score);
  py::class_<hft::ScheduledSlice>(module, "ScheduledSlice")
      .def_readonly("timestamp_ns", &hft::ScheduledSlice::timestamp_ns)
      .def_readonly("quantity", &hft::ScheduledSlice::quantity);
  py::class_<hft::Leg>(module, "Leg")
      .def(py::init<>())
      .def_readwrite("symbol", &hft::Leg::symbol)
      .def_readwrite("side", &hft::Leg::side)
      .def_readwrite("quantity", &hft::Leg::quantity)
      .def_readwrite("reference_price", &hft::Leg::reference_price)
      .def_readwrite("venue_preferences", &hft::Leg::venue_preferences)
      .def_readwrite("hedge", &hft::Leg::hedge);
  py::class_<hft::MultiLegPlan>(module, "MultiLegPlan")
      .def_readonly("plan_id", &hft::MultiLegPlan::plan_id)
      .def_readonly("legs", &hft::MultiLegPlan::legs)
      .def_readonly("state", &hft::MultiLegPlan::state)
      .def_readonly("risk_reserved", &hft::MultiLegPlan::risk_reserved);
  py::class_<hft::Event>(module, "Event")
      .def_readonly("timestamp_ns", &hft::Event::timestamp_ns)
      .def_readonly("sequence", &hft::Event::sequence)
      .def_readonly("event_type", &hft::Event::event_type)
      .def_readonly("payload", &hft::Event::payload);
  py::class_<hft::SmartOrderRouter>(module, "SmartOrderRouter")
      .def(py::init<double>(), py::arg("latency_penalty_bps_per_ms") = 0.0)
      .def("route", &hft::SmartOrderRouter::route);
  py::class_<hft::Scheduler>(module, "Scheduler")
      .def_static("twap", &hft::Scheduler::twap)
      .def_static("vwap", &hft::Scheduler::vwap);
  py::class_<hft::RiskReservationBook>(module, "RiskReservationBook")
      .def(py::init<double>())
      .def("reserve", &hft::RiskReservationBook::reserve)
      .def("release", &hft::RiskReservationBook::release)
      .def("is_reserved", &hft::RiskReservationBook::is_reserved)
      .def_property_readonly("reserved_notional",
                             &hft::RiskReservationBook::reserved_notional)
      .def_property_readonly("available_notional",
                             &hft::RiskReservationBook::available_notional);
  py::class_<hft::SingleLoop>(module, "SingleLoop")
      .def(py::init<>())
      .def("enqueue", &hft::SingleLoop::enqueue, py::arg("timestamp_ns"),
           py::arg("event_type"), py::arg("payload") = "")
      .def("pop", &hft::SingleLoop::pop)
      .def("drain_until", &hft::SingleLoop::drain_until)
      .def_property_readonly("size", &hft::SingleLoop::size);
  py::class_<hft::ExecutionManagementSystem>(
      module, "ExecutionManagementSystem")
      .def(py::init<double>(), py::arg("latency_penalty_bps_per_ms") = 0.0)
      .def("update_quote", &hft::ExecutionManagementSystem::update_quote)
      .def("route", &hft::ExecutionManagementSystem::route)
      .def("create_plan", &hft::ExecutionManagementSystem::create_plan)
      .def("reserve_plan", &hft::ExecutionManagementSystem::reserve_plan)
      .def("route_plan", &hft::ExecutionManagementSystem::route_plan)
      .def("transition_plan", &hft::ExecutionManagementSystem::transition_plan)
      .def("recover_plan", &hft::ExecutionManagementSystem::recover_plan)
      .def("release_plan", &hft::ExecutionManagementSystem::release_plan)
      .def("plan", &hft::ExecutionManagementSystem::plan)
      .def("loop", &hft::ExecutionManagementSystem::loop,
           py::return_value_policy::reference_internal);
}
