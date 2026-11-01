"""WP7 — execution profiles and release identity.

A profile is a YAML file under profiles/ that sets every switch the engine exposes, each one classified:

    MANDATORY      a control that must stay on in a non-legacy profile (causal fills, shared trade volume, in-flight / pending /
                   UNKNOWN exposure, depth-size fix, trade de-duplication, staleness and dead-feed gates, breaker, recovery,
                   ledger bridge, product / fee consistency). Startup REJECTS a profile that disables one, unless the profile
                   is legacy_v0 (the comparison baseline, kept bit-identical).
    CALIBRATED     a number that comes from a measurement (fill model, latencies, failure costs); its source is named.
    LEGACY_COMPAT  a switch that only exists so the old behaviour stays reachable.

Unknown keys are errors. `apply_profile` builds the ArbBacktestConfig; `release_identity` stamps every result with the profile
hash, the code hash (sha256 over the engine sources + git revision + dirty flag), the data manifest hash, the instrument / fee
snapshot versions, the calibration cutoff and the seed, so two results can be told apart by what produced them.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, fields, is_dataclass, replace
from pathlib import Path
from typing import Any

from .config import FEE_PRESETS, LATENCY_PRESETS

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"
ENGINE_DIR = Path(__file__).resolve().parent


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Switch:
    key: str                  # dotted path into the profile document (and into ArbBacktestConfig unless `target` says otherwise)
    cls: str                  # MANDATORY | CALIBRATED | LEGACY_COMPAT
    forbidden: tuple = ()     # MANDATORY only: values that count as "disabled" (rejected outside legacy_v0)
    note: str = ""


SWITCHES: tuple[Switch, ...] = (
    Switch("arb.profile", "LEGACY_COMPAT", note="legacy_v0 | perp_perp_maker_taker_v1 | spot_* (edge expressions and gate)"),
    Switch("arb.dispatch_mode", "MANDATORY", ("simultaneous",), "the hedge waits for the maker fill (Method A)"),
    Switch("valuation", "MANDATORY", ("last_mark",), "R11: open position marked at the common timestamp"),
    Switch("equity_bridge", "MANDATORY", ("off", None), "ledger identity checked at the end of every run"),
    Switch("enforce_book_state", "MANDATORY", (False,), "no pricing or fills from a non-valid book (WP4)"),
    Switch("trade_dedupe", "MANDATORY", ("off", None), "trades de-duplicated (native id when present)"),
    Switch("delta_uses_book_size", "MANDATORY", (False,), "depth-size fix"),
    Switch("maker.share_trade_volume", "MANDATORY", (False,), "one print fills own orders once in total"),
    Switch("maker.dedupe_trade_depth", "MANDATORY", (False,), "the same execution is charged once"),
    Switch("maker.exchange_time_eligibility", "MANDATORY", (False,), "causal fills: nothing executed before arrival fills us"),
    Switch("arb.count_pending_exposure", "MANDATORY", (False,), "in-flight / pending exposure counts toward the limits"),
    Switch("arb.count_unknown_exposure", "MANDATORY", (False,), "UNKNOWN orders count toward the limits"),
    Switch("arb.maker_max_age_s", "MANDATORY", (0, 0.0, None), "order lifetime set"),
    Switch("timing.dead_feed_s", "MANDATORY", (None, 0, 0.0), "dead-feed gate (WP3)"),
    Switch("breaker", "MANDATORY", (None, False), "circuit breaker configured"),
    Switch("hedge_faults.response", "MANDATORY", ("legacy",), "recovery policy, not the latency-free oracle"),
    Switch("cancel_time_basis", "MANDATORY", ("receive",), "cancels take effect on exchange time (D-03)"),
    Switch("timing.equal_time_policy", "LEGACY_COMPAT", note="market_first (v1) | cancel_first (legacy tie rule)"),
    Switch("timing.truth_books", "MANDATORY", (False,), "venue-truth books for arrivals (WP3)"),
    Switch("maker.cancel_model", "CALIBRATED", note="proportional (fill-evidence study 2026-10-30)"),
    Switch("maker.unknown_queue_model", "CALIBRATED", note="depth_aware; floor / distribution pending calibration (WP4)"),
    Switch("maker.vanish_clears_queue", "LEGACY_COMPAT", note="True in v1 (PROVISIONAL)"),
    Switch("maker.fill_evidence", "CALIBRATED", note="confirm_delta 20 ms in v1 (PROVISIONAL)"),
    Switch("maker.level_creator_trade_only", "LEGACY_COMPAT", note="opt-in"),
    Switch("arb.net_ceiling_multiple", "MANDATORY", note="1.0 in the perp profile — DECISION_PENDING (1x vs fill headroom)"),
    Switch("arb.p_fail", "CALIBRATED", note="PLACEHOLDER 0.07; estimate on training days only"),
    Switch("arb.loss_fail_bps", "CALIBRATED", note="hedge-failure study"),
    Switch("arb.expected_hedge_move_bps_bid", "CALIBRATED", note="training-day executed hedge move"),
    Switch("latency_preset", "CALIBRATED", note="ASSUMED until ollog measurements replace it"),
    Switch("margin", "MANDATORY", (None,), "margin model configured in a perp profile"),
    Switch("product_fee_consistency", "MANDATORY", (False,), "fee preset must match the venue's product"),
)
MANDATORY = {s.key: s for s in SWITCHES if s.cls == "MANDATORY"}


def _get(doc: dict, dotted: str, default=None):
    cur: Any = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def load_profile(path: str | Path) -> dict:
    import yaml
    p = Path(path)
    if not p.exists() and (PROFILES_DIR / f"{path}.yaml").exists():
        p = PROFILES_DIR / f"{path}.yaml"
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or "name" not in doc:
        raise ProfileError(f"{p}: a profile is a mapping with a 'name'")
    doc["_path"] = str(p)
    doc["_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    return doc


def check_mandatory(doc: dict) -> list[str]:
    """Which MANDATORY controls the profile disables. Empty for a valid non-legacy profile; legacy_v0 is exempt (and says so)."""
    if doc.get("name") == "legacy_v0":
        return []
    bad = []
    for key, sw in MANDATORY.items():
        val = _get(doc, key, None) if key != "product_fee_consistency" else doc.get("product_fee_consistency", True)
        if key == "breaker":
            val = doc.get("breaker")
        if key == "margin":
            val = doc.get("margin")
        if val in sw.forbidden or (val is None and key in ("timing.dead_feed_s", "breaker", "margin") and doc.get("product", "perp") == "perp"):
            if key == "margin" and doc.get("product") != "perp":
                continue
            bad.append(f"{key}={val!r} disables a MANDATORY control: {sw.note}")
    return bad


NESTED = {"arb": "ArbConfig", "maker": "MakerConfig", "venue_state": "VenueStateConfig", "agent_flow": "AgentFlowConfig",
          "ack_latency": "AckLatencyConfig"}


def _build(cls, data: dict, *, where: str, nested: dict[str, type] | None = None):
    """Instantiate a dataclass from a mapping; nested mappings become the dataclasses named in `nested`; unknown keys are errors."""
    names = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - names)
    if unknown:
        raise ProfileError(f"{where}: unknown field(s) {unknown}")
    kw = {}
    for k, v in data.items():
        if isinstance(v, dict) and nested and k in nested:
            kw[k] = _build(nested[k], v, where=f"{where}.{k}")
        else:
            kw[k] = v
    return cls(**kw)


def apply_profile(doc: dict, **overrides):
    """ArbBacktestConfig from a loaded profile. `overrides` are run-specific fields (pair, dates, data_root, output_dir, …)."""
    from .arb_strategy import ArbConfig
    from .backtester import ArbBacktestConfig
    from .background_agent_model import AgentFlowConfig
    from .circuit_breaker import CircuitBreakerConfig
    from .fill_engine import MakerConfig
    from .postonly import AckLatencyConfig
    from .venue_state import VenueStateConfig
    from .hedge_faults import HedgeFaultConfig
    from .margin_model import MarginModelConfig, MarginTier, VenueMargin
    from .timing import ClockConfig, TimingConfig

    bad = check_mandatory(doc)
    if bad:
        raise ProfileError(f"profile {doc.get('name')}: " + "; ".join(bad))
    body = {k: v for k, v in doc.items() if not k.startswith("_") and k not in ("name", "description", "classification", "decision_status",
                                                                               "product_fee_consistency", "timing", "breaker", "margin",
                                                                               "fee_presets", "calibration")}
    cfg_kw: dict[str, Any] = dict(body)
    cfg_kw["product_fee_consistency"] = bool(doc.get("product_fee_consistency", True))
    for k, v in (doc.get("fee_presets") or {}).items():
        cfg_kw[f"{k}_fee_preset"] = v
    t = doc.get("timing")
    if t:
        t = dict(t)
        preset = t.pop("from_latency_preset", None) or doc.get("latency_preset")
        clock = t.pop("clock", None)
        if clock:
            t["clock"] = ClockConfig(**clock)
        cfg_kw["timing"] = TimingConfig.from_latency_preset(LATENCY_PRESETS[preset], **t)
    b = doc.get("breaker")
    if b:
        cfg_kw["circuit_breaker"] = CircuitBreakerConfig(**b)
    m = doc.get("margin")
    if m:
        venues = {}
        for v, spec in (m.get("venues") or {}).items():
            spec = dict(spec)
            tiers = tuple(MarginTier(**x) for x in spec.pop("tiers"))
            venues[v] = VenueMargin(tiers=tiers, **spec)
        rest = {k: v for k, v in m.items() if k != "venues"}
        cfg_kw["margin"] = MarginModelConfig(venues=venues, **rest)
    hf = cfg_kw.pop("hedge_faults", None)
    if isinstance(hf, dict):
        cfg_kw["hedge_faults"] = HedgeFaultConfig(**hf)
    cfg_kw.update(overrides)
    cfg = _build(ArbBacktestConfig, cfg_kw, where=f"profile {doc.get('name')}",
                 nested={"arb": ArbConfig, "maker": MakerConfig, "venue_state": VenueStateConfig, "agent_flow": AgentFlowConfig,
                         "ack_latency": AckLatencyConfig})
    cfg.release_identity = release_identity(cfg, doc)
    return cfg


def _sha_files(paths) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(str(Path(p).name).encode())
        h.update(Path(p).read_bytes())
    return h.hexdigest()


def code_hash() -> dict:
    files = [p for p in ENGINE_DIR.glob("*.py")]
    root = ENGINE_DIR.parents[2]
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", str(ENGINE_DIR)], cwd=root, capture_output=True, text=True, timeout=10).stdout.strip())
    except Exception:  # pragma: no cover
        rev, dirty = "unknown", True
    return {"sha256_sources": _sha_files(files), "n_files": len(files), "git_rev": rev or "unknown", "git_dirty": dirty}


def data_manifest_hash(data_root: str | Path, pair: str, venues, start_date: str, end_date: str) -> dict:
    """Hash of the manifests under data/manifests when present, plus a listing hash (name + size) of the day directories used."""
    root = Path(data_root)
    manifests = sorted((root / "manifests").glob("*.json")) if (root / "manifests").is_dir() else []
    listing = hashlib.sha256()
    n = 0
    from .event_loader import _day_range, venue_root
    for v in venues:
        vr = venue_root(root, v)
        for day in _day_range(start_date, end_date):
            for stream in ("book_snapshots", "book_events", "trades"):
                d = vr / stream / f"pair={pair}" / f"date={day}"
                if d.is_dir():
                    for f in sorted(d.glob("*.jsonl.gz")):
                        listing.update(f"{v}/{stream}/{day}/{f.name}:{f.stat().st_size}".encode())
                        n += 1
    return {"manifests_sha256": _sha_files(manifests) if manifests else None, "manifest_files": [m.name for m in manifests],
            "day_listing_sha256": listing.hexdigest(), "files_listed": n}


def release_identity(cfg, doc: dict | None = None) -> dict:
    root = ENGINE_DIR.parents[2]
    fee_snapshot = hashlib.sha256(json.dumps({k: (v.maker_bps, v.taker_bps) for k, v in sorted(FEE_PRESETS.items())}).encode()).hexdigest()
    cm = root / "data" / "contract_master"
    cm_files = sorted(cm.glob("*")) if cm.is_dir() else []
    calib = root / "config" / "lifecycle_calibration_bybit.json"
    cutoff = None
    if calib.exists():
        try:
            c = json.loads(calib.read_text())
            cutoff = (c.get("meta") or {}).get("generated_utc")      # the calibration post-dates the Jan-2022 replay data
        except Exception:  # pragma: no cover
            cutoff = "unreadable"
    return {"profile": (doc or {}).get("name"), "profile_sha256": (doc or {}).get("_sha256"), "profile_path": (doc or {}).get("_path"),
            "decision_status": (doc or {}).get("decision_status"),
            "code": code_hash(),
            "data": data_manifest_hash(cfg.data_root, cfg.pair, cfg.venues, cfg.start_date, cfg.end_date),
            "fee_snapshot_sha256": fee_snapshot, "fee_presets": {"bybit": cfg.bybit_fee_preset, "binance": cfg.binance_fee_preset},
            "instrument_master_sha256": _sha_files(cm_files) if cm_files else None,
            "calibration_file": str(calib) if calib.exists() else None, "calibration_cutoff": cutoff,
            "seed": cfg.seed, "latency_preset": cfg.latency_preset}


def switch_table(doc: dict) -> list[dict]:
    """Every switch with its classification and the value this profile sets (None = engine default)."""
    rows = []
    for sw in SWITCHES:
        rows.append({"switch": sw.key, "class": sw.cls, "value": _get(doc, sw.key, doc.get(sw.key.split(".")[0]) if "." not in sw.key else None),
                     "note": sw.note})
    return rows
