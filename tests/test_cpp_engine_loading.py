"""GAP01: the C++ matching engine failing to load is reported, never silent, and is tested.

Each scenario runs in a fresh interpreter with ``maker_engine.SIM_ENGINE_DIR`` pointed at a
temporary directory, so a module cached by another test can neither mask the failure nor
leak out of it.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).parents[1]
EXT_SUFFIX = sysconfig.get_config_var("EXT_SUFFIX") or ".so"

PROBE = textwrap.dedent("""
    import json, sys
    from pathlib import Path
    from crypto_stat_arb import maker_engine
    maker_engine.SIM_ENGINE_DIR = Path(sys.argv[1])
    out = {}
    engine = maker_engine.make_queue_engine("auto")
    out["auto"] = maker_engine.engine_identity(engine)
    try:
        maker_engine.make_queue_engine("cpp")
        out["cpp"] = "loaded"
    except RuntimeError as exc:
        out["cpp"] = "RuntimeError: " + str(exc)
    print(json.dumps(out))
""")


def probe(directory: Path, code: str = PROBE) -> dict:
    import crypto_stat_arb

    # The subprocess imports the same package this test run uses (an installed wheel or src/).
    env = {**os.environ, "PYTHONPATH": str(Path(crypto_stat_arb.__file__).parents[1])}
    done = subprocess.run([sys.executable, "-c", code, str(directory)], env=env, capture_output=True,
                          text=True, timeout=120)
    if done.returncode != 0:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout.strip().splitlines()[-1])


FAKE_ENGINE = textwrap.dedent("""
    class FeeConfig:
        def __init__(self, maker, taker): pass

    class MatchingEngine:
        def __init__(self, fees): pass
        def set_queue_position_frac(self, frac): pass
        def update_bid_level(self, price, size): raise RuntimeError("engine fault in update_bid_level")
        def update_ask_level(self, price, size): raise RuntimeError("engine fault in update_ask_level")
""")


class LoadFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_missing_module_falls_back_with_the_reason_and_cpp_refuses(self):
        out = probe(self.dir)
        self.assertEqual(out["auto"]["engine"], "python_queue_model")
        self.assertIn("No module named 'mm_matching_engine'", out["auto"]["cpp_unavailable_reason"])
        self.assertTrue(out["cpp"].startswith("RuntimeError: mm_matching_engine is not usable (ModuleNotFoundError"))

    def test_a_corrupt_binary_for_this_interpreter_is_a_load_failure_not_a_crash(self):
        # Named with this interpreter's own extension suffix, so Python really tries to load it.
        # (The committed macOS binary does not match a Linux suffix and reads as "missing" there.)
        (self.dir / f"mm_matching_engine{EXT_SUFFIX}").write_bytes(b"\x00not a shared object\x00" * 64)
        out = probe(self.dir)
        self.assertEqual(out["auto"]["engine"], "python_queue_model")
        self.assertRegex(out["auto"]["cpp_unavailable_reason"], r"^(ImportError|OSError)")
        self.assertIn("not usable (", out["cpp"])

    def test_a_module_without_the_engine_classes_is_refused(self):
        (self.dir / "mm_matching_engine.py").write_text("VERSION = 'stub'\n")
        out = probe(self.dir)
        self.assertEqual(out["auto"]["engine"], "python_queue_model")
        self.assertIn("lacks MatchingEngine, FeeConfig", out["auto"]["cpp_unavailable_reason"])
        self.assertIn("lacks MatchingEngine, FeeConfig", out["cpp"])

    def test_an_engine_failing_mid_run_raises_and_never_switches_engines(self):
        (self.dir / "mm_matching_engine.py").write_text(FAKE_ENGINE)
        code = textwrap.dedent("""
            import json, sys
            from pathlib import Path
            from crypto_stat_arb import maker_engine
            maker_engine.SIM_ENGINE_DIR = Path(sys.argv[1])
            engine = maker_engine.make_queue_engine("auto")
            identity = maker_engine.engine_identity(engine)
            try:
                engine.set_level("bid", 100.0, 1.0)
                outcome = "no error"
            except RuntimeError as exc:
                outcome = str(exc)
            print(json.dumps({"identity": identity, "outcome": outcome, "after": engine.name}))
        """)
        out = probe(self.dir, code)
        self.assertEqual(out["identity"]["engine"], "cpp_mm_matching_engine")
        self.assertEqual(out["outcome"], "engine fault in update_bid_level")
        self.assertEqual(out["after"], "cpp_mm_matching_engine")              # no silent switch
        self.assertFalse(out["identity"]["built_for_this_interpreter"])      # a .py stand-in, not a build


class ReportTests(unittest.TestCase):
    def test_the_maker_report_says_which_engine_ran_and_why(self):
        from crypto_stat_arb.maker_sim import MakerDryRunSimulator

        report = MakerDryRunSimulator(["BYBIT"], "BTC/USDT", latency_model=None, engine="python").report()
        self.assertEqual(report["engine_build"]["engine"], "python_queue_model")
        self.assertEqual(report["engine_build"]["cpp_unavailable_reason"], "")    # forced, not a fallback

    @unittest.skipUnless(os.environ.get("REQUIRE_CPP_ENGINE") or __import__(
        "crypto_stat_arb.maker_engine", fromlist=["load_cpp_module"]).load_cpp_module() is not None,
        "compiled mm_matching_engine not importable here (REQUIRE_CPP_ENGINE=1 makes this a failure)")
    def test_a_loaded_cpp_engine_reports_its_binary(self):
        from crypto_stat_arb import maker_engine

        engine = maker_engine.make_queue_engine("cpp")
        identity = maker_engine.engine_identity(engine)
        binary = Path(engine._m.__file__)
        self.assertEqual(identity["binary_sha256"], maker_engine._sha256(binary))
        self.assertTrue(identity["built_for_this_interpreter"])
        self.assertEqual(identity["source_sha256"], maker_engine.cpp_source_sha256())


if __name__ == "__main__":
    unittest.main()
