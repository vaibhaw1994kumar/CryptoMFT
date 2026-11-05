"""Queue model v2, step 5: cancellation models named by their arithmetic (guide s5).

C is the NET residual cancellation at our price level: the displayed decrease after the part explained by prints has been netted off (gross
decreases are never passed in). A = external size ahead of our order, B = external size behind it (the feed carries no order ids, so who left is unknown).
Any rule must respect the feasibility interval   max(0, C - B) <= C_ahead <= min(C, A)   and never divide by a zero total.

    behind_first   C_ahead = min(A, max(0, C - B))      the cancellations hit the orders behind us first
    ahead_first    C_ahead = min(C, A)                  the cancellations hit the orders ahead of us first
    proportional   C_ahead = C * A / (A + B)            every resting order is equally likely to leave
    power(n)       C_ahead = C * A^n / (A^n + B^n)      n > 1 favours the larger side, n < 1 the smaller; n = 1 is proportional
    min_baseline   the engine's all_ahead rule (a decrease below the level size we last saw is charged in full to the queue ahead); kept under its
                   old name "all_ahead" as a deprecated alias.
New displayed size joins behind every existing order and never pushes anyone back."""
from __future__ import annotations

MODELS = ("behind_first", "ahead_first", "proportional", "power", "min_baseline")


def cancel_ahead(model: str, C: float, A: float, B: float, n: float = 1.0) -> float:
    """How much of the net residual cancellation C came from the queue AHEAD of our order."""
    if C <= 0.0 or A <= 0.0:
        return 0.0
    A, B = max(A, 0.0), max(B, 0.0)
    C = min(C, A + B)                                   # a level cannot lose more than it holds
    lo, hi = max(0.0, C - B), min(C, A)
    if model == "behind_first":
        x = min(A, max(0.0, C - B))
    elif model == "ahead_first":
        x = min(C, A)
    elif model in ("proportional", "power"):
        p = n if model == "power" else 1.0
        den = A ** p + B ** p
        x = C * (A ** p) / den if den > 0.0 else 0.0
    elif model in ("min_baseline", "all_ahead"):
        x = min(C, A)                                   # the all_ahead rule charges the whole decrease to the queue ahead
    else:
        raise ValueError(f"unknown cancellation model {model!r}; known: {MODELS}")
    return min(max(x, lo), hi)                          # feasibility, always
