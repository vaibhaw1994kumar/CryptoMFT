"""Queue-position matching engines for simulated passive (post-only) orders.

Two interchangeable implementations of one contract:

* ``CppQueueEngine`` wraps ``sim_engine``'s compiled ``mm_matching_engine``
  (C++ ``mm::MatchingEngine``) directly, keeping an explicit
  ``uint64 engine id <-> client order id`` map;
* ``PythonQueueEngine`` mirrors the C++ arithmetic expression for expression,
  so both produce bit-identical fills (``tests/test_maker_engine.py`` checks
  this whenever the C++ module is importable).

Semantics (see ``sim_engine/include/mm/matching_engine.hpp``):

* level sizes come only from ``set_level`` (the book feed); prints never edit
  them;
* a resting order's queue ahead is ``queue_position_frac`` x the visible size
  at its price when it *arrives* (1.0 = back of the visible queue, the
  conservative default; it is a calibration parameter, not a constant); prints at its price burn it in FIFO order, and only our own
  fills absorb volume, so several of our orders at one level share a print;
* a book update can only cap the queue ahead at the visible size; the capped
  amount is credited against the next print at that level, so a print and its
  book delta burn the same queue in either arrival order;
* a print strictly through our price fills the order in full at its price.

Every fill is caused by a public print. The engines never invent fills from
book crossings or touch probabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
from pathlib import Path
import sys
import sysconfig
from typing import Any, Protocol


SIM_ENGINE_DIR = Path(__file__).with_name("sim_engine")
CPP_ENGINE_NAME = "cpp_mm_matching_engine"
PYTHON_ENGINE_NAME = "python_queue_model"


REQUIRED_CPP_ATTRIBUTES = ("MatchingEngine", "FeeConfig")
# Why the last load_cpp_module() call returned None ("" when it loaded).
_last_load_error = ""


def load_cpp_module() -> Any | None:
    """Import the compiled ``mm_matching_engine`` built into ``sim_engine/``.

    Returns None, and records the reason (``cpp_load_error()``), when it is missing,
    cannot be loaded (wrong platform or ABI, corrupt file) or lacks the classes used.
    """
    global _last_load_error
    if str(SIM_ENGINE_DIR) not in sys.path:
        sys.path.insert(0, str(SIM_ENGINE_DIR))
    try:
        module = importlib.import_module("mm_matching_engine")
    except (ImportError, OSError) as exc:
        _last_load_error = f"{type(exc).__name__}: {exc}"
        return None
    missing = [name for name in REQUIRED_CPP_ATTRIBUTES if not hasattr(module, name)]
    if missing:
        _last_load_error = f"mm_matching_engine at {getattr(module, '__file__', '?')} lacks {', '.join(missing)}"
        return None
    _last_load_error = ""
    return module


def cpp_load_error() -> str:
    return _last_load_error


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def cpp_source_sha256() -> str | None:
    """Hash of the C++ sources the compiled engine should be built from (sim_engine/).

    The same scheme the top-level CMakeLists.txt compiles into the binary: one
    "<relative path>:<sha256>\\n" line per file, sorted by path, then sha256 of that.
    """
    files = [*(SIM_ENGINE_DIR / "include").rglob("*.hpp"), *(SIM_ENGINE_DIR / "src").rglob("*.cpp")]
    if not files:
        return None
    lines = sorted(f"{path.relative_to(SIM_ENGINE_DIR).as_posix()}:{hashlib.sha256(path.read_bytes()).hexdigest()}\n"
                   for path in files)
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def engine_identity(engine: Any) -> dict[str, Any]:
    """Which engine ran and, for the C++ one, exactly which binary (GAP01)."""
    ext_suffix = sysconfig.get_config_var("EXT_SUFFIX") or ""
    if isinstance(engine, CppQueueEngine):
        binary = Path(getattr(engine._m, "__file__", "") or "")
        build_info = dict(getattr(engine._m, "__build_info__", None) or {})
        built_from = build_info.get("source_sha256") or None
        source = cpp_source_sha256()
        return {
            "engine": engine.name,
            "binary": binary.name,
            "binary_sha256": _sha256(binary) if binary.name else None,
            "interpreter_ext_suffix": ext_suffix,
            "built_for_this_interpreter": binary.name.endswith(ext_suffix) if ext_suffix else None,
            "source_sha256": source,
            "build_info": build_info,             # empty: built outside the packaged CMake build
            # True / False when the binary says what it was built from; None when it cannot.
            "built_from_current_source": (built_from == source) if built_from and source else None,
        }
    return {"engine": getattr(engine, "name", type(engine).__name__),
            "cpp_unavailable_reason": getattr(engine, "cpp_unavailable_reason", ""),
            "interpreter_ext_suffix": ext_suffix}


@dataclass(frozen=True, slots=True)
class EngineFill:
    client_order_id: str
    quantity: float
    price: float
    through: bool


class QueueEngine(Protocol):
    name: str

    def set_level(self, side: str, price: float, size: float) -> None: ...
    def place(self, client_order_id: str, side: str, price: float, size: float, ts_ns: int) -> float: ...
    def cancel(self, client_order_id: str) -> bool: ...
    def on_trade(self, price: float, quantity: float, aggressor_side: str) -> list[EngineFill]: ...
    def queue_ahead(self, client_order_id: str) -> float: ...
    def remaining(self, client_order_id: str) -> float: ...


class CppQueueEngine:
    name = CPP_ENGINE_NAME

    def __init__(self, module: Any, queue_position_frac: float = 1.0) -> None:
        self._m = module
        # Fees are applied by the simulator from the platform fee schedule.
        self._engine = module.MatchingEngine(module.FeeConfig(0.0, 0.0))
        self._engine.set_queue_position_frac(float(queue_position_frac))
        self.queue_position_frac = float(queue_position_frac)
        self._client_by_engine: dict[int, str] = {}
        self._engine_by_client: dict[str, int] = {}

    def set_level(self, side: str, price: float, size: float) -> None:
        if side == "bid":
            self._engine.update_bid_level(price, size)
        else:
            self._engine.update_ask_level(price, size)

    def place(self, client_order_id: str, side: str, price: float, size: float, ts_ns: int) -> float:
        engine_side = self._m.Side.BID if side == "bid" else self._m.Side.ASK
        engine_id = int(self._engine.place_order(
            engine_side, price, size, self._m.OrderType.POST_ONLY, int(ts_ns)
        ))
        self._client_by_engine[engine_id] = client_order_id
        self._engine_by_client[client_order_id] = engine_id
        return float(self._engine.queue_ahead_remaining(engine_id))

    def cancel(self, client_order_id: str) -> bool:
        engine_id = self._engine_by_client.get(client_order_id)
        return engine_id is not None and bool(self._engine.cancel_order(engine_id))

    def on_trade(self, price: float, quantity: float, aggressor_side: str) -> list[EngineFill]:
        aggressor = self._m.AggressorSide.BUY if aggressor_side == "BUY" else self._m.AggressorSide.SELL
        fills = []
        for result in self._engine.on_trade(price, quantity, aggressor):
            client = self._client_by_engine[int(result.order_id)]
            fills.append(EngineFill(
                client, float(result.filled_qty), float(result.fill_price),
                float(result.fill_price) != price,
            ))
        return fills

    def queue_ahead(self, client_order_id: str) -> float:
        return float(self._engine.queue_ahead_remaining(self._engine_by_client[client_order_id]))

    def remaining(self, client_order_id: str) -> float:
        order = self._engine.get_order(self._engine_by_client[client_order_id])
        return float(order.remaining_size) if order is not None and order.active else 0.0


@dataclass(slots=True)
class _PyOrder:
    client_order_id: str
    side: str
    price: float
    remaining: float
    queue_ahead: float
    clamp_credit: float = 0.0
    active: bool = True


class PythonQueueEngine:
    """Pure-Python mirror of the C++ engine's queue arithmetic."""

    name = PYTHON_ENGINE_NAME
    cpp_unavailable_reason = ""            # set by make_queue_engine("auto") when C++ did not load

    def __init__(self, queue_position_frac: float = 1.0) -> None:
        _check_frac(queue_position_frac)
        self.queue_position_frac = float(queue_position_frac)
        self._market: dict[str, dict[float, float]] = {"bid": {}, "ask": {}}
        self._fifo: dict[str, dict[float, list[str]]] = {"bid": {}, "ask": {}}
        self._orders: dict[str, _PyOrder] = {}

    def set_level(self, side: str, price: float, size: float) -> None:
        size = size if size > 0.0 else 0.0
        if size > 0.0:
            self._market[side][price] = size
        else:
            self._market[side].pop(price, None)
        for client in self._fifo[side].get(price, ()):
            order = self._orders[client]
            if not order.active:
                continue
            order.clamp_credit = 0.0
            if order.queue_ahead > size:
                order.clamp_credit = order.queue_ahead - size
                order.queue_ahead = size

    def place(self, client_order_id: str, side: str, price: float, size: float, ts_ns: int) -> float:
        ahead = self._market[side].get(price, 0.0) * self.queue_position_frac
        self._orders[client_order_id] = _PyOrder(client_order_id, side, price, size, ahead)
        self._fifo[side].setdefault(price, []).append(client_order_id)
        return ahead

    def cancel(self, client_order_id: str) -> bool:
        order = self._orders.get(client_order_id)
        if order is None or not order.active:
            return False
        order.active = False
        self._remove(order)
        return True

    def _remove(self, order: _PyOrder) -> None:
        queue = self._fifo[order.side].get(order.price)
        if queue is not None and order.client_order_id in queue:
            queue.remove(order.client_order_id)
            if not queue:
                del self._fifo[order.side][order.price]

    def on_trade(self, price: float, quantity: float, aggressor_side: str) -> list[EngineFill]:
        side = "ask" if aggressor_side == "BUY" else "bid"
        levels = sorted(self._fifo[side], reverse=(side == "bid"))
        fills: list[EngineFill] = []
        for level_price in levels:
            swept = level_price >= price if side == "bid" else level_price <= price
            if not swept:
                break
            through = level_price != price
            our_filled = 0.0
            for client in list(self._fifo[side].get(level_price, ())):
                order = self._orders[client]
                if not order.active:
                    self._remove(order)
                    continue
                if through:
                    order.queue_ahead = 0.0
                    order.clamp_credit = 0.0
                    fill_qty = order.remaining
                else:
                    passing = quantity - our_filled
                    if passing <= 0.0:
                        break
                    ahead = order.queue_ahead + order.clamp_credit
                    burn = min(ahead, passing)
                    credit_left = max(0.0, order.clamp_credit - passing)
                    order.queue_ahead = max(0.0, ahead - burn - credit_left)
                    order.clamp_credit = credit_left
                    fill_qty = min(order.remaining, passing - burn)
                if fill_qty > 0.0:
                    fills.append(EngineFill(client, fill_qty, level_price, through))
                    order.remaining -= fill_qty
                    our_filled += fill_qty
                    if order.remaining <= 0.0:
                        order.active = False
                        self._remove(order)
            if not through:
                break
        return fills

    def queue_ahead(self, client_order_id: str) -> float:
        return self._orders[client_order_id].queue_ahead

    def remaining(self, client_order_id: str) -> float:
        order = self._orders.get(client_order_id)
        return order.remaining if order is not None and order.active else 0.0


def _check_frac(frac: float) -> None:
    if not 0.0 <= float(frac) <= 1.0:
        raise ValueError("queue_position_frac must be within [0, 1]")


def make_queue_engine(preference: str = "auto", *, queue_position_frac: float = 1.0) -> QueueEngine:
    """``auto`` prefers C++, ``cpp`` requires it, ``python`` forces the mirror."""
    if preference not in {"auto", "cpp", "python"}:
        raise ValueError(f"unknown queue engine preference {preference!r}")
    _check_frac(queue_position_frac)
    if preference != "python":
        module = load_cpp_module()
        if module is not None:
            return CppQueueEngine(module, queue_position_frac)
        if preference == "cpp":
            raise RuntimeError(
                f"mm_matching_engine is not usable ({cpp_load_error()}); build it with "
                "cmake -S src/crypto_stat_arb/sim_engine -B build && cmake --build build"
            )
    engine = PythonQueueEngine(queue_position_frac)
    # "auto" falling back is reported, never silent: the reason travels with the engine.
    engine.cpp_unavailable_reason = "" if preference == "python" else (cpp_load_error() or "not loaded")
    return engine
