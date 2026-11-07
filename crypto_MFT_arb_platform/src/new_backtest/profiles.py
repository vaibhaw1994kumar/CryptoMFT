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
PLATFORM_DIR = ENGINE_DIR.parents[1]          # the platform checkout / release root (config/, profiles/ live here)


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
    Switch("maker.opposite_cross_fills", "CALIBRATED", note="True in v1 (PROVISIONAL): fill in full once the opposite best quote reaches our price (probe cross-check vs hftbacktest 2026-11-03)"),
    Switch("maker.opposite_cross_confirm_ms", "CALIBRATED", note="0 (off) in v1; v1.1 step 8 sets 500: a cross fills only with a reaching print on the same venue within +-500 ms, else it is dropped (EXPLORATORY window)"),
    Switch("maker.opposite_cross_cap_to_print", "CALIBRATED", note="False in v1; v1.1 step 9c sets True: a confirmed cross fills at most the reaching prints' unused volume (EXPLORATORY)"),
    Switch("arb.round_quotes_to_tick", "LEGACY_COMPAT", note="False in v1 (off-grid quotes); v1.1 step 9b sets True: bids round down, asks up, onto the declared venue tick"),
    Switch("message_atomic_book", "LEGACY_COMPAT", note="False in v1 / v1.1 (each delta is applied, repaired and evaluated on its own); v1.2 sets True: the deltas of one exchange message are one unit (cross judged at the message end; strategy, touch and maker queue updates run after the whole message)"),
    Switch("arb.join_touch_on_tick_rounding", "LEGACY_COMPAT", note="False in v1 / v1.1 (a quote rounded onto the touch is refused); v1.2 sets True: it joins the back of the touch queue if the edge at the rounded price passes"),
    Switch("hedge_arrival_book", "LEGACY_COMPAT", note="view in v1 (default); v1.1 step 9e sets truth: hedge IOCs execute on the venue-truth book at arrival"),
    Switch("arb.net_ceiling_multiple", "MANDATORY", note="1.0 in the perp profile — DECISION_PENDING (1x vs fill headroom)"),
    Switch("arb.p_fail", "CALIBRATED", note="PLACEHOLDER 0.07; estimate on training days only"),
    Switch("arb.loss_fail_bps", "CALIBRATED", note="hedge-failure study"),
    Switch("arb.expected_hedge_move_bps_bid", "CALIBRATED", note="training-day executed hedge move"),
    Switch("latency_preset", "CALIBRATED", note="ASSUMED until ollog measurements replace it"),
    Switch("margin", "MANDATORY", (None,), "margin model configured in a perp profile"),
    Switch("postonly_reject_model", "MANDATORY", ("off", None), "perp profile: PostOnly would-cross orders are rejected at exchange arrival time (lifecycle task 5); perp profiles only"),
    Switch("product_fee_consistency", "MANDATORY", (False,), "fee preset must match the venue's product"),
    Switch("book_tape_repair", "LEGACY_COMPAT", note="engine default True (and in v1): the trade tape lowers book levels, which double-depletes when the depth decrease arrives first; v1.1 sets False"),
    Switch("own_impact", "LEGACY_COMPAT", note="legacy (v1): own taker consumption mutates the public book; overlay (v1.1): a shadow overlay cleared at the next public update of that level"),
    Switch("maker.ledger_epoch", "LEGACY_COMPAT", note="off in v1; v1.1 sets True: a snapshot / reset invalidates old netting credit, ambiguous associations are flagged"),
    Switch("execution.primary.reprice", "CALIBRATED", note="none in v1 / v1.1 / v1_2 (a resting quote is never repriced); v1_3 carries the S0 rule with every number null and enabled false: the trader (or the live values) supplies them, nothing is fitted"),
    Switch("placement_book", "LEGACY_COMPAT", note="view in v1 / v1.1 (a passive order joins the strategy's feed-delayed book); v1_2 carries the switch: truth = the venue-truth book at the arrival's exchange time, the book PostOnly is judged on, when it is complete there, otherwise the view book (counted)"),
    Switch("fee_source", "LEGACY_COMPAT", note="presets in v1 / v1.1 / v1_2 / v1_3 / legacy_v0 (the backtester's own FEE_PRESETS); core in v1_4 (B2 step 4): the preset names are tier names of the core FeeSchedule (crypto_stat_arb.fees.FEE_TIERS) and every rate a run uses is that schedule's booked rate"),
    Switch("trust_local_ts", "CALIBRATED", note="ASSUMED. True on both venues in v1 / v1.1 (a recorded local_ts is the receive time); v1.2 sets False per venue for the March data, whose local stamps are constructed (exchange + exactly 2.000 ms): the latency preset feed delay applies instead, one sample per exchange message"),
    Switch("maker.fill_through_prints", "LEGACY_COMPAT", note="False in v1 / v1.1 (a print through our price only burns queue); v1.2 sets True together with through_print_volume_limited: the order fills when a print goes through its price, limited by the print's volume. True alone (the full version) is refused at startup"),
    Switch("maker.through_print_volume_limited", "LEGACY_COMPAT", note="False in v1 / v1.1; v1.2 True: the through-print fill is limited by the print volume, one budget shared by every order the print went through"),
    Switch("maker.depletion_ledger", "LEGACY_COMPAT", note="False in v1 / v1.1 (a print netted against an earlier depth decrease drains nothing more: book-first leaves the queue ahead further back with size behind us, and trade_after_front uses the whole print); v1.2 sets True: the decrease records the head movement it applied, the print moves the queue only by the rest, and netted pairs with no other proof are counted"),
    Switch("maker.unknown_queue_dist", "CALIBRATED", note="floor in v1 (hand-set 25); v1.1: fitted on strictly earlier days, floor 0, flagged fallback 25 where the fit has no data (unknown_queue_fallback)"),
    Switch("execution.price_cap_required", "CALIBRATED", note="execution layer: every non-unwind intent carries a worst price and every child stays inside it; true in both profiles (today's behaviour), CALIBRATED until the cap decision (brief step 11) is made"),
    Switch("execution.one_hedge_child", "MANDATORY", ("off",), "execution layer: one live hedge child per hedge intent (counted, or refused in enforce mode)"),
    Switch("execution.risk_reservation", "MANDATORY", ("off",), "execution layer: primary exposure is checked and reserved in one call and released at the terminal state (counted, or refused in enforce mode)"),
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
        if val in sw.forbidden or (val is None and key in ("timing.dead_feed_s", "breaker", "margin", "postonly_reject_model") and doc.get("product", "perp") == "perp"):
            if key in ("margin", "postonly_reject_model") and doc.get("product") != "perp":
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


def apply_profile(doc: dict, dataset: str | None = None, **overrides):
    """ArbBacktestConfig from a loaded profile. `overrides` are run-specific fields (pair, dates, data_root, output_dir, …).
    `dataset` names the data the run reads ("mar2024", "jan2022"): the profile's `dataset_settings[dataset]` (owner 7 Nov: e.g. March Bybit's trade/depth windows) is merged over the profile
    for that dataset only; a profile without an entry for it, and every run that passes no dataset, is unchanged. What was applied is recorded in release_identity."""
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
                                                                               "fee_presets", "calibration", "execution", "hedge_move_calibration", "dataset_settings")}
    ds_applied = None
    ds_cfg = ((doc.get("dataset_settings") or {}).get(dataset) if dataset else None)
    if ds_cfg:
        body = {**body, **{k: ({**(body.get(k) or {}), **v} if isinstance(v, dict) and isinstance(body.get(k) or {}, dict) else v) for k, v in ds_cfg.items()}}
        ds_applied = {"dataset": dataset, "applied": ds_cfg}
    cfg_kw: dict[str, Any] = dict(body)
    cfg_kw["product_fee_consistency"] = bool(doc.get("product_fee_consistency", True))
    for k, v in (doc.get("fee_presets") or {}).items():
        cfg_kw[f"{k}_fee_preset"] = v
    timing_preset = None
    t = doc.get("timing")
    if t:
        t = dict(t)
        preset = timing_preset = t.pop("from_latency_preset", None) or doc.get("latency_preset")
        clock = t.pop("clock", None)
        if clock:
            t["clock"] = ClockConfig(**clock)
        floors = (t.pop("floors_ms", None) or {}).get(preset)        # ASSUMED floors per preset: they name the cross-region venue of THAT preset, so another preset gets none
        cfg_kw["timing"] = TimingConfig.from_latency_preset(LATENCY_PRESETS[preset], floors_ms=floors, **t)
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
    ex = doc.get("execution")
    if ex is not None:
        from .execution import ExecutionConfig, ExecutionConfigError
        try:
            cfg_kw["execution"] = ExecutionConfig(**ex)
        except (ExecutionConfigError, TypeError) as e:
            raise ProfileError(f"profile {doc.get('name')}: execution block: {e}") from e
    hf = cfg_kw.pop("hedge_faults", None)
    if isinstance(hf, dict):
        cfg_kw["hedge_faults"] = HedgeFaultConfig(**hf)
    cal = doc.get("calibration")
    if cal:
        path = resolve_calibration(doc, cal)
        al = dict(cfg_kw.get("ack_latency") or {})
        cfg_kw["ack_latency"] = AckLatencyConfig(source="empirical", calibration_path=str(path), period=cal.get("period", "all"),
                                                 **{k: v for k, v in al.items() if k not in ("source", "calibration_path", "period")})
    cfg_kw.update(overrides)
    sensitivity = cfg_kw.pop("latency_sensitivity", None)
    check_latency_preset(doc, cfg_kw.get("latency_preset"), timing_preset, sensitivity)
    cfg = _build(ArbBacktestConfig, cfg_kw, where=f"profile {doc.get('name')}",
                 nested={"arb": ArbConfig, "maker": MakerConfig, "venue_state": VenueStateConfig, "agent_flow": AgentFlowConfig,
                         "ack_latency": AckLatencyConfig})
    hm = doc.get("hedge_move_calibration")
    if hm:
        cfg.arb.expected_hedge_move_bps_bid, cfg.arb.expected_hedge_move_bps_ask, hm_info = load_hedge_move_calibration(doc, hm, cfg.start_date)
    cfg.release_identity = release_identity(cfg, doc)
    if ds_applied:
        cfg.release_identity["dataset_settings"] = ds_applied
    cfg.release_identity["latency_sensitivity"] = sensitivity
    if hm:
        cfg.release_identity["hedge_move_calibration"] = hm_info
    return cfg


def load_hedge_move_calibration(doc: dict, hm: dict, run_start: str) -> tuple[float, float, dict]:
    """The expected hedge move (bps, bid and ask) a profile pins: a data file whose sha256 the profile records, estimated on TRAIN days that all end before the run starts.
    Refuses a missing file, a file whose bytes no longer match the recorded hash, and a file with no days or with a day on or after the run's start date."""
    p = Path(hm["file"])
    path = p if p.is_absolute() else PLATFORM_DIR / p
    if not path.exists():
        raise ProfileError(f"profile {doc.get('name')}: hedge_move_calibration file {path} does not exist")
    got = hashlib.sha256(path.read_bytes()).hexdigest()
    if got != hm.get("sha256"):
        raise ProfileError(f"profile {doc.get('name')}: hedge_move_calibration {path.name} has sha256 {got}, the profile pins {hm.get('sha256')} (file edited after it was pinned)")
    d = json.loads(path.read_text())
    days = d.get("days") or []
    if not days or any(str(x) >= str(run_start) for x in days):
        raise ProfileError(f"profile {doc.get('name')}: hedge_move_calibration {path.name} must come from TRAIN days strictly before the run start {run_start}; it lists {days}")
    mv = d["expected_hedge_move_bps"]
    return float(mv["bid"]), float(mv["ask"]), {"file": str(path), "sha256": got, "days": days, "horizon_s": d.get("horizon_s"),
                                                "n": {k: (d.get("stats", {}).get(k) or {}).get("n") for k in ("bid", "ask")},
                                                "method": d.get("method")}


# Latency presets that cannot exist in production (config.py: nobody is co-located with both venues). A non-legacy profile refuses
# them unless the run is declared a sensitivity run: apply_profile(..., latency_sensitivity="<why>"), recorded in release_identity.
SENSITIVITY_ONLY_LATENCY = ("colocated_both",)


def check_latency_preset(doc: dict, latency_preset, timing_preset, sensitivity) -> None:
    if doc.get("name") == "legacy_v0":              # the frozen comparison baseline keeps its historical preset
        return
    used = sorted({p for p in (latency_preset, timing_preset) if p in SENSITIVITY_ONLY_LATENCY})
    if used and not (isinstance(sensitivity, str) and sensitivity.strip()):
        raise ProfileError(
            f"profile {doc.get('name')}: latency preset {', '.join(used)} is physically impossible and allowed only in a sensitivity run; "
            f"pass latency_sensitivity='<reason>' to label the run, or use singapore / tokyo / neutral")


def _sha_files(paths) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(str(Path(p).name).encode())
        h.update(Path(p).read_bytes())
    return h.hexdigest()


# Phase A: engine code that moved into the core still belongs to the release identity. The directories are new core packages; the files are single moved modules.
CORE_DIR = ENGINE_DIR.parent / "crypto_stat_arb"
CORE_MOVED_DIRS = ("strategies", "simulation", "ems")
CORE_MOVED_FILES: tuple[str, ...] = ("order_state_machine.py", "circuit_breaker.py", "valuation.py")


def code_hash() -> dict:
    files = [p for p in ENGINE_DIR.glob("*.py")] + [p for p in (ENGINE_DIR / "execution").rglob("*.py")]
    files += [p for d in CORE_MOVED_DIRS if (CORE_DIR / d).is_dir() for p in sorted((CORE_DIR / d).rglob("*.py"))]
    files += [CORE_DIR / f for f in CORE_MOVED_FILES if (CORE_DIR / f).is_file()]
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


def read_calibration(path) -> dict:
    return json.loads(Path(path).read_text())


def resolve_calibration(doc: dict, cal: dict) -> Path:
    """Path of the lifecycle calibration file a profile names, after checking it measured the profile's product.

    A perp profile refuses a file whose `product` is not 'perp' (the spot calibration config/lifecycle_calibration_bybit.json has no
    `product` key and is read as spot), and a spot profile refuses a perp file. The file's content hash must also match its own recorded hash."""
    p = Path(cal["file"])
    path = p if p.is_absolute() else PLATFORM_DIR / p
    if not path.exists():
        raise ProfileError(f"profile {doc.get('name')}: calibration file {path} does not exist")
    c = read_calibration(path)
    got = c.get("product", "spot")
    want = doc.get("product", "perp")
    if got != want:
        raise ProfileError(f"profile {doc.get('name')} (product {want}) refuses calibration {path.name}: it measured product {got!r}")
    if cal.get("venue") and c.get("venue") != cal["venue"]:
        raise ProfileError(f"profile {doc.get('name')}: calibration {path.name} is for venue {c.get('venue')!r}, profile asks for {cal['venue']!r}")
    body = {k: v for k, v in c.items() if k != "meta"}
    if (c.get("meta") or {}).get("content_sha256") != hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest():
        raise ProfileError(f"calibration {path.name}: content hash does not match its recorded content_sha256 (file edited after it was written)")
    return path


def _git_state(repo: Path) -> dict:
    """Phase B freeze: the commit a repository is at and whether tracked files differ from it (untracked files are not counted). Never raises: a repository that cannot be read says so."""
    import subprocess

    def run(*a: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, timeout=20, check=True).stdout.strip()
    try:
        dirty = [ln for ln in run("status", "--porcelain", "--untracked-files=no").splitlines() if ln.strip()]
        return {"repo": str(repo), "branch": run("rev-parse", "--abbrev-ref", "HEAD"), "commit": run("rev-parse", "HEAD"), "dirty": bool(dirty), "tracked_files_changed": len(dirty)}
    except Exception as e:  # pragma: no cover - depends on the checkout
        return {"repo": str(repo), "error": type(e).__name__}


def release_identity(cfg, doc: dict | None = None) -> dict:
    root = ENGINE_DIR.parents[2]
    fee_snapshot = hashlib.sha256(json.dumps({k: (v.maker_bps, v.taker_bps) for k, v in sorted(FEE_PRESETS.items())}).encode()).hexdigest()
    fee_schedule = None
    if getattr(cfg, "fee_source", "presets") == "core":                 # B2 step 4: the rows the run booked, labels included; the presets dict is not what this run used
        from .config import core_fee_specs
        _, rows = core_fee_specs(cfg.pair, cfg.bybit_fee_preset, cfg.binance_fee_preset)
        fee_schedule = {"source": "core crypto_stat_arb.fees.FEE_TIERS", "rows": rows, "rows_sha256": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()}
    cm = root / "data" / "contract_master"
    cm_files = sorted(cm.glob("*")) if cm.is_dir() else []
    calib = PLATFORM_DIR / "config" / "lifecycle_calibration_bybit.json"
    pcal = (doc or {}).get("calibration")
    calib_info = None
    if pcal:
        calib = resolve_calibration(doc, pcal)
        cj = read_calibration(calib)
        calib_info = {"file": str(calib), "file_sha256": hashlib.sha256(calib.read_bytes()).hexdigest(),
                      "content_sha256": (cj.get("meta") or {}).get("content_sha256"), "product": cj.get("product", "spot"),
                      "venue": cj.get("venue"), "schema": cj.get("schema"), "networks_with_data": (cj.get("provenance") or {}).get("networks_with_data")}
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
            "fee_snapshot_sha256": fee_snapshot, "fee_schedule": fee_schedule, "fee_presets": {"bybit": cfg.bybit_fee_preset, "binance": cfg.binance_fee_preset},
            "instrument_master_sha256": _sha_files(cm_files) if cm_files else None,
            "calibration_file": str(calib) if calib.exists() else None, "calibration_cutoff": cutoff, "calibration": calib_info,
            "git": {"outer": _git_state(root), "core": _git_state(CORE_DIR)},
            "seed": cfg.seed, "latency_preset": cfg.latency_preset,
            "execution": ({"config": cfg.execution.as_dict(), "config_sha256": cfg.execution.sha256()} if getattr(cfg, "execution", None) is not None else None)}


def switch_table(doc: dict) -> list[dict]:
    """Every switch with its classification and the value this profile sets (None = engine default)."""
    rows = []
    for sw in SWITCHES:
        rows.append({"switch": sw.key, "class": sw.cls, "value": _get(doc, sw.key, doc.get(sw.key.split(".")[0]) if "." not in sw.key else None),
                     "note": sw.note})
    return rows
