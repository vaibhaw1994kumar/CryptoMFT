"""Run the queue-v2 test files with --junitxml and turn the result into junit_summary.json (the report prints evidence, it does not assert it)."""
import json, subprocess, xml.etree.ElementTree as ET
P = "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform"
FILES = ["test_queue_v2_manifest.py", "test_queue_v2_hypothetical_order.py", "test_queue_v2_ledger.py", "test_queue_v2_cancel_models.py", "test_queue_v2_cohort.py",
         "test_queue_v2_cohort_executor.py", "test_queue_v2_invariants.py", "test_public_book_invariant.py", "test_maker_observations.py", "test_queue_v2_survival.py"]
x = "/Users/vaibhav/Documents/Newproject/out/queue_v2/junit.xml"
r = subprocess.run(["python3.12", "-m", "pytest", *[f"{P}/tests/{f}" for f in FILES], "-q", "-p", "no:cacheprovider", f"--junitxml={x}"],
                   capture_output=True, text=True, env={"PYTHONPATH": f"{P}/src:{P}/tests", "PATH": "/usr/bin:/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin"}, cwd=P)
tests = []
for tc in ET.parse(x).getroot().iter("testcase"):
    cn = tc.get("classname", "").split(".")
    mod, cls = (cn[-2], cn[-1]) if len(cn) >= 2 else (cn[-1], "")
    out = "failed" if tc.find("failure") is not None or tc.find("error") is not None else ("skipped" if tc.find("skipped") is not None else "passed")
    tests.append({"file": f"{mod}::{cls}", "name": tc.get("name"), "outcome": out})
json.dump({"summary": r.stdout.strip().splitlines()[-1], "tests": tests}, open("/Users/vaibhav/Documents/Newproject/out/queue_v2/junit_summary.json", "w"), indent=1)
print(r.stdout.strip().splitlines()[-1])
