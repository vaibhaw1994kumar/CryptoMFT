import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.release import (
    ArtifactInput,
    ArtifactKind,
    PaperReleaseRegistry,
    PaperReleaseSpec,
)


class Clock:
    def __init__(self):
        self.value = 1_800_000_000_000_000_000

    def __call__(self):
        self.value += 1
        return self.value


class PaperReleaseTests(unittest.TestCase):
    def inputs(self, directory, parameter_value="8"):
        root = Path(directory) / "inputs"
        root.mkdir(exist_ok=True)
        values = {
            ArtifactKind.STRATEGY_CODE: ("strategy", "1.0.0", "def signal(): return True\n"),
            ArtifactKind.FEATURE_DEFINITION: ("features", "1.0.0", '{"feature":"edge"}\n'),
            ArtifactKind.MODEL: ("model", "1.0.0", '{"type":"rules"}\n'),
            ArtifactKind.PARAMETERS: ("parameters", parameter_value, f'{{"entry_bps":{parameter_value}}}\n'),
            ArtifactKind.DATA_MANIFEST: ("data", "1.0.0", '{"sha256":"abc"}\n'),
            ArtifactKind.SIMULATOR_CONFIG: ("simulator", "1.0.0", '{"matcher":"l2"}\n'),
        }
        result = []
        for kind, (name, version, content) in values.items():
            path = root / f"{kind.value.lower()}.txt"
            path.write_text(content)
            result.append(ArtifactInput(kind, name, version, path))
        return tuple(result)

    @staticmethod
    def spec(artifacts, build="build-1"):
        return PaperReleaseSpec(
            "cross_venue_btc_v1", build, "paper-research", artifacts
        )

    def test_reproduce_and_rollback_known_approved_paper_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = PaperReleaseRegistry(root / "registry", clock_ns=Clock())
            inputs_v1 = self.inputs(root, "8")
            v1 = registry.create(self.spec(inputs_v1, "build-1"))
            registry.approve(v1["release_id"], "portfolio-manager")
            registry.deploy(v1["release_id"], "paper-operator")

            inputs_v2 = self.inputs(root, "9")
            v2 = registry.create(self.spec(inputs_v2, "build-2"))
            registry.approve(v2["release_id"], "portfolio-manager")
            registry.deploy(v2["release_id"], "paper-operator")
            active = registry.rollback(
                v1["release_id"], "paper-operator", "restore accepted edge threshold"
            )
            self.assertEqual(active["release_id"], v1["release_id"])
            self.assertEqual(active["action"], "ROLLBACK")
            self.assertEqual(active["approval_owner"], "portfolio-manager")

            reproduction = registry.reproduce(
                v1["release_id"], root / "reproduced-v1"
            )
            self.assertTrue(reproduction["verified"])
            parameter = next(
                row for row in reproduction["artifacts"]
                if row["kind"] == "PARAMETERS"
            )
            restored = root / "reproduced-v1" / parameter["materialized_path"]
            self.assertEqual(json.loads(restored.read_text())["entry_bps"], 8)

            audit = [json.loads(line) for line in registry.audit_path.read_text().splitlines()]
            self.assertEqual([row["action"] for row in audit], [
                "DEPLOY", "DEPLOY", "ROLLBACK",
            ])
            self.assertEqual(audit[1]["previous_hash"], audit[0]["record_hash"])
            self.assertEqual(audit[2]["from_release_id"], v2["release_id"])

    def test_release_is_content_addressed_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = PaperReleaseRegistry(root / "registry", clock_ns=Clock())
            spec = self.spec(self.inputs(root))
            first = registry.create(spec)
            second = registry.create(spec)
            self.assertEqual(first, second)
            self.assertTrue(registry.verify(first["release_id"])["verified"])

    def test_unapproved_or_tampered_release_cannot_deploy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = PaperReleaseRegistry(root / "registry", clock_ns=Clock())
            release = registry.create(self.spec(self.inputs(root)))
            with self.assertRaisesRegex(ValueError, "not approved"):
                registry.deploy(release["release_id"], "paper-operator")
            registry.approve(release["release_id"], "portfolio-manager")
            snapshot = (
                registry._release_dir(release["release_id"])
                / release["artifacts"][0]["snapshot_path"]
            )
            snapshot.write_text("tampered")
            self.assertFalse(registry.verify(release["release_id"])["verified"])
            with self.assertRaisesRegex(ValueError, "unverifiable"):
                registry.deploy(release["release_id"], "paper-operator")

    def test_every_versioned_artifact_class_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incomplete = self.inputs(root)[:-1]
            with self.assertRaisesRegex(ValueError, "SIMULATOR_CONFIG"):
                PaperReleaseRegistry(root / "registry").create(self.spec(incomplete))


if __name__ == "__main__":
    unittest.main()
