"""Item 9 (owner 20 Nov, REPORT ONLY): run out/integration/day1/run_day.py with the core session's margin.missing_price_action set to `breach` (MarginConfig's own default) instead of the profile path's
`pause`. Run wrapper only: no profile and no code changes. Why a wrapper: the key cannot be set in a profile today (new_backtest.profiles.apply_profile builds the backtester's MarginModelConfig from the same
margin block and refuses the key: TypeError), so `--set margin.missing_price_action=breach` fails. The value is set on the raw config the session is built from, and the run is labelled.
usage: run_mpa.py ACTION run_day-args..."""
import runpy
import sys

SRC = "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src"
sys.path.insert(0, SRC)
action = sys.argv.pop(1)
import new_backtest.session_replay as sr  # noqa: E402

_orig = sr.raw_config_from_profile


def patched(*a, **k):
    raw, notes = _orig(*a, **k)
    raw["risk"]["margin"]["missing_price_action"] = action
    notes = dict(notes)
    notes["item9_missing_price_action"] = f"{action} (set by out/integration/n_order/item9/run_mpa.py on the raw config; the profile path's default is pause)"
    return raw, notes


sr.raw_config_from_profile = patched
sys.argv = ["run_day.py"] + sys.argv[1:]
runpy.run_path("/Users/vaibhav/Documents/Newproject/out/integration/day1/run_day.py", run_name="__main__")
