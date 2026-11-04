"""Queue model v2, step 6: cohort queue representation (guide s6). A calibrated L2 queue model: the cohorts are model labels, never recovered orders.

One LevelQueue per (venue, side, price) holds, front to back, the external size at that price as COHORTS BY ARRIVAL, with our own orders interleaved:
  - the size displayed when our first order joined is ONE cohort of unknown age (age = unknown_age_s, a stated assumption, not a measurement);
  - size that appears later joins BEHIND everyone already there as a younger cohort;
  - several own orders at one price share the traversal: the second sits behind the first and behind every cohort that arrived between them;
    a partial fill keeps priority; a reset (snapshot) re-bases the EXTERNAL size and never removes an own order.
Residual cancellations C (net of what prints explained) are removed from the external cohorts by an age hypothesis:
  youngest_first  the youngest cohorts leave first          oldest_first   the oldest leave first
  uniform         every unit of external size equally likely  hazard(theta)  weight = size * (1 + age_s) ** -theta  (theta = 0 is uniform; large theta -> youngest_first)
Prints consume from the front through cohorts AND own orders with ONE budget. A depth decrease whose print has not arrived yet is applied provisionally; when the print
arrives, `reclassify` takes the movement back out of wherever the hypothesis put it and re-applies it at the head (credit only for movement already applied)."""
from __future__ import annotations

HYPOTHESES = ("youngest_first", "oldest_first", "uniform", "hazard")
EPS = 1e-12


class LevelQueue:
    __slots__ = ("items", "unknown_age_s", "provisional", "log")

    def __init__(self, ext_size: float, now: float, unknown_age_s: float = 30.0) -> None:
        self.unknown_age_s = unknown_age_s
        self.items: list[dict] = []
        self.provisional: list[dict] = []          # decreases applied before their print: {ts, parts: [(item, amount)], size}
        self.log: list[dict] = []                  # counterfactual projections (ts, displayed, model, adjustment); bounded
        if ext_size > EPS:
            self.items.append({"t": "ext", "size": float(ext_size), "birth": now - unknown_age_s})

    # ── reading ──────────────────────────────────────────────────────────────
    def ext_total(self) -> float:
        return sum(i["size"] for i in self.items if i["t"] == "ext")

    def own_ids(self) -> list[int]:
        return [i["id"] for i in self.items if i["t"] == "own"]

    @property
    def has_own(self) -> bool:
        return any(i["t"] == "own" for i in self.items)

    def ahead(self, oid: int) -> float:
        s = 0.0
        for i in self.items:
            if i["t"] == "own" and i["id"] == oid:
                return s
            s += i["size"]
        raise KeyError(oid)

    def behind_external(self, oid: int) -> float:
        seen, s = False, 0.0
        for i in self.items:
            if seen and i["t"] == "ext":
                s += i["size"]
            if i["t"] == "own" and i["id"] == oid:
                seen = True
        return s

    # ── changes ──────────────────────────────────────────────────────────────
    def join(self, oid: int, size: float) -> float:
        """Our order joins at the very back: behind every cohort and every own order already there."""
        self.items.append({"t": "own", "id": oid, "size": float(size)})
        return self.ahead(oid)

    def add_external(self, size: float, now: float) -> None:
        if size > EPS:
            self.items.append({"t": "ext", "size": float(size), "birth": now})

    def sync_own(self, oid: int, remaining: float) -> None:
        for i in self.items:
            if i["t"] == "own" and i["id"] == oid:
                i["size"] = max(0.0, float(remaining))
                return

    def remove_own(self, oid: int) -> None:
        self.items = [i for i in self.items if not (i["t"] == "own" and i["id"] == oid)]

    def compact(self) -> None:
        if not self.provisional:
            self.items = [i for i in self.items if i["t"] == "own" or i["size"] > EPS]

    def expire_provisional(self, horizon_ts: float) -> None:
        self.provisional = [p for p in self.provisional if p["ts"] >= horizon_ts]
        self.compact()

    def cancel_external(self, C: float, now: float, hypothesis: str, theta: float = 1.0, provisional: bool = True) -> float:
        """Remove up to C of external size by the age hypothesis. Returns the amount removed. Nothing else moves."""
        ext = [i for i in self.items if i["t"] == "ext" and i["size"] > EPS]
        C = min(C, sum(i["size"] for i in ext))
        if C <= EPS:
            return 0.0
        parts: list[tuple[dict, float]] = []
        if hypothesis in ("youngest_first", "oldest_first"):
            order = sorted(ext, key=lambda i: i["birth"], reverse=(hypothesis == "youngest_first"))
            rem = C
            for i in order:
                take = min(i["size"], rem)
                parts.append((i, take))
                rem -= take
                if rem <= EPS:
                    break
        elif hypothesis in ("uniform", "hazard"):
            w = {id(i): i["size"] * (1.0 if hypothesis == "uniform" else (1.0 + max(0.0, now - i["birth"])) ** (-theta)) for i in ext}
            alloc = {id(i): 0.0 for i in ext}
            rem, live = C, list(ext)
            for _ in range(len(ext) + 1):                                  # water-filling: a cohort cannot give more than it holds
                tw = sum(w[id(i)] for i in live)
                if rem <= EPS or tw <= 0.0:
                    break
                capped = []
                for i in live:
                    share = rem * w[id(i)] / tw
                    room = i["size"] - alloc[id(i)]
                    if share >= room - EPS:
                        capped.append(i)
                if not capped:
                    for i in live:
                        alloc[id(i)] += rem * w[id(i)] / tw
                    rem = 0.0
                    break
                for i in capped:
                    take = i["size"] - alloc[id(i)]
                    alloc[id(i)] += take
                    rem -= take
                live = [i for i in live if i not in capped]
            parts = [(i, alloc[id(i)]) for i in ext if alloc[id(i)] > EPS]
        else:
            raise ValueError(f"unknown age hypothesis {hypothesis!r}; known: {HYPOTHESES}")
        removed = 0.0
        for i, amt in parts:
            i["size"] -= amt
            removed += amt
        if provisional and removed > EPS:
            self.provisional.append({"ts": now, "parts": parts, "size": removed})
        return removed

    def reclassify(self, amount: float) -> float:
        """The print that explains an earlier decrease arrived: take the provisional movement back out of where the hypothesis put it and apply it at the head
        (the execution was at the front). Only movement already applied can be credited; returns the amount credited."""
        credited, want = 0.0, amount
        while self.provisional and want > EPS:
            p = self.provisional[0]
            take = min(p["size"], want)
            frac = take / p["size"]
            for i, amt in p["parts"]:
                i["size"] += amt * frac
            self.cancel_head_external(take)
            p["size"] -= take
            p["parts"] = [(i, amt * (1.0 - frac)) for i, amt in p["parts"]]
            want -= take
            credited += take
            if p["size"] <= EPS:
                self.provisional.pop(0)
        self.compact()
        return credited

    def cancel_head_external(self, amount: float) -> float:
        """Remove external size from the front, stopping at the first own order (the head of the queue is ahead of us)."""
        rem = amount
        for i in self.items:
            if i["t"] == "own":
                break
            take = min(i["size"], rem)
            i["size"] -= take
            rem -= take
            if rem <= EPS:
                break
        return amount - rem

    def consume_print(self, volume: float) -> dict[int, float]:
        """One print, one budget: it goes through cohorts and own orders in queue order. Returns {own order id: quantity filled}."""
        self.provisional.clear()                                       # an arriving print settles every pending decrease at this level
        fills: dict[int, float] = {}
        rem = volume
        for i in self.items:
            if rem <= EPS:
                break
            take = min(i["size"], rem)
            i["size"] -= take
            rem -= take
            if i["t"] == "own" and take > EPS:
                fills[i["id"]] = fills.get(i["id"], 0.0) + take
        self.compact()
        return fills

    def clear_ahead(self, oid: int) -> None:
        """Price priority (a print through our price): everything ahead of this order is gone."""
        out = []
        for i in self.items:
            if i["t"] == "own" and i["id"] == oid:
                out.append(i)
                out += self.items[self.items.index(i) + 1:]
                break
        self.items = out

    def rebase_external(self, displayed: float, now: float, hypothesis: str, theta: float, note: str = "") -> tuple[float, float]:
        """Counterfactual projection at a settled update boundary: the displayed external size is the truth about the public book. Growth joins behind everyone;
        a decrease is removed by the hypothesis. Own orders are never touched and no fill is generated from the projection. Returns (added, removed)."""
        ext = self.ext_total()
        added = removed = 0.0
        if displayed > ext + EPS:
            self.add_external(displayed - ext, now)
            added = displayed - ext
        elif displayed < ext - EPS:
            removed = self.cancel_external(ext - displayed, now, hypothesis, theta)
        if (added or removed) and len(self.log) < 5000:
            self.log.append({"ts": now, "displayed": displayed, "model_external": ext, "added": added, "removed": removed, "note": note})
        return added, removed
