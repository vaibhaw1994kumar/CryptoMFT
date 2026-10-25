"""SDK02: config keys the platform does not read are rejected at load."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.config import load_config
from crypto_stat_arb.config_schema import SCHEMA, unknown_keys

ROOT = Path(__file__).parents[1]
SHIPPED = ("research", "paper_watch", "perp_research")


def shipped(name):
    return json.loads((ROOT / f"config/{name}.json").read_text())


class ConfigSchemaTests(unittest.TestCase):
    def test_shipped_configs_have_no_unknown_keys(self):
        for name in SHIPPED:
            self.assertEqual(unknown_keys(shipped(name)), [], name)

    def test_typos_are_reported_with_their_path_and_a_suggestion(self):
        raw = shipped("research")
        raw["strategy"]["entry_bpz"] = "5"                                          # top-level section
        raw["oms"]["timeouts"]["report_timeout"] = 100                              # nested
        raw["instruments"][1]["tick_sise"] = "0.01"                                 # list item
        raw["risk"]["margin_accounts"]["BINANCE"]["margn_enabled"] = True           # map value
        raw["telemetry"] = {}                                                       # unknown section
        self.assertEqual(sorted(unknown_keys(raw)), sorted([
            "strategy.entry_bpz (did you mean 'entry_bps'?)",
            "oms.timeouts.report_timeout (did you mean 'report_timeout_ms'?)",
            "instruments[1].tick_sise (did you mean 'tick_size'?)",
            "risk.margin_accounts.BINANCE.margn_enabled (did you mean 'margin_enabled'?)",
            "telemetry",
        ]))

    def test_keys_of_venue_instrument_and_strategy_maps_are_data(self):
        raw = shipped("perp_research")
        raw["risk"]["initial_capital_by_venue"]["OKX"] = "1000"            # any map key is allowed
        raw["risk"]["strategy_gross_limits"]["another_strategy"] = "1"
        self.assertEqual(unknown_keys(raw), [])

    def test_margin_tiers_accept_both_shapes_and_check_rows(self):
        raw = shipped("perp_research")
        tiers = raw["risk"]["margin"]["venues"]["BYBIT"]["tiers"]
        name = next(iter(tiers))
        tiers[name] = copy.deepcopy(tiers[name]["tiers"])                   # plain list form
        self.assertEqual(unknown_keys(raw), [])
        tiers[name][0]["maintenance_rte"] = "0.01"
        self.assertEqual(unknown_keys(raw),
                         [f"risk.margin.venues.BYBIT.tiers.{name}[0].maintenance_rte "
                          "(did you mean 'maintenance_rate'?)"])

    def test_underscore_keys_are_comments(self):
        raw = shipped("research")
        raw["_comment"] = "tuned 2026-10-25"
        raw["strategy"]["_why"] = "wider entry for the perp test"
        self.assertEqual(unknown_keys(raw), [])

    def test_load_config_fails_closed(self):
        raw = shipped("research")
        raw["strategy"]["min_net_edge_bp"] = "2"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, r"unknown config key\(s\): strategy\.min_net_edge_bp "
                                                    r"\(did you mean 'min_net_edge_bps'\?\)"):
                load_config(path)

    def test_every_documented_top_level_section_is_in_the_schema(self):
        seen = set().union(*(shipped(name).keys() for name in SHIPPED))
        self.assertLessEqual(seen, set(SCHEMA))


if __name__ == "__main__":
    unittest.main()
