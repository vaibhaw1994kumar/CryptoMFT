"""SDK04: a CycloneDX SBOM per wheel, from uv.lock and the wheel's own contents."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("sbom", ROOT / "scripts/sbom.py")
sbom = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sbom)


class SbomTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wheel = Path(self.tmp.name) / "hft_platform_kit-0.3.0-cp312-cp312-manylinux_2_28_x86_64.whl"
        with zipfile.ZipFile(self.wheel, "w") as archive:
            archive.writestr("hft_platform_kit/_ems.cpython-312-x86_64-linux-gnu.so", b"ems binary")
            archive.writestr("crypto_stat_arb/sim_engine/mm_matching_engine.cpython-312-x86_64-linux-gnu.so", b"mm")
            archive.writestr("crypto_stat_arb/__init__.py", b"")
        self.doc = sbom.build_sbom(self.wheel)
        self.lock = tomllib.loads((ROOT / "uv.lock").read_text())

    def tearDown(self):
        self.tmp.cleanup()

    def component(self, name):
        return next(c for c in self.doc["components"] if c["name"] == name)

    def test_it_is_cyclonedx_and_names_the_wheel_it_describes(self):
        self.assertEqual((self.doc["bomFormat"], self.doc["specVersion"]), ("CycloneDX", "1.5"))
        meta = self.doc["metadata"]["component"]
        self.assertEqual(meta["hashes"][0]["content"], hashlib.sha256(self.wheel.read_bytes()).hexdigest())
        self.assertEqual(meta["purl"], "pkg:pypi/hft-platform-kit@0.3.0")

    def test_native_extensions_are_listed_with_the_hash_of_the_shipped_file(self):
        native = {c["properties"][0]["value"]: c["hashes"][0]["content"] for c in self.doc["components"]
                  if c.get("description") == "native extension compiled into this wheel"}
        self.assertEqual(native, {
            "hft_platform_kit/_ems.cpython-312-x86_64-linux-gnu.so": hashlib.sha256(b"ems binary").hexdigest(),
            "crypto_stat_arb/sim_engine/mm_matching_engine.cpython-312-x86_64-linux-gnu.so":
                hashlib.sha256(b"mm").hexdigest(),
        })

    def test_runtime_dependencies_come_from_the_lock_and_dev_tools_do_not(self):
        numpy = self.component("numpy")
        locked = next(p for p in self.lock["package"] if p["name"] == "numpy")
        self.assertEqual(numpy["version"], locked["version"])
        self.assertEqual(numpy["hashes"][0]["content"], locked["sdist"]["hash"].split(":", 1)[1])
        names = {c["name"] for c in self.doc["components"]}
        self.assertNotIn("pytest", names)                      # dev group only
        self.assertIn("websockets", names)

    def test_platform_specific_dependencies_keep_their_marker(self):
        markers = {p["value"] for p in self.component("winloop")["properties"] if p["name"] == "environment-marker"}
        self.assertEqual(markers, {"sys_platform == 'win32'"})

    def test_build_tools_are_listed_as_unpinned_and_not_shipped(self):
        pybind = self.component("pybind11")
        self.assertEqual(pybind["scope"], "excluded")
        self.assertIn({"name": "pinned", "value": "false: resolved at build time"}, pybind["properties"])

    def test_gaps_are_stated_not_invented(self):
        props = {p["name"]: p["value"] for p in self.doc["metadata"]["properties"]}
        self.assertIn("gap:licenses", props)
        self.assertFalse(any("licenses" in c for c in self.doc["components"]))

    def test_output_is_deterministic(self):
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1700000000"}):
            first, second = sbom.build_sbom(self.wheel), sbom.build_sbom(self.wheel)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(first["metadata"]["timestamp"], "2023-11-14T22:13:20Z")


if __name__ == "__main__":
    unittest.main()
