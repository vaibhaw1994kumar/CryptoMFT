import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.config import load_config
from crypto_stat_arb.credentials import (
    CredentialBroker,
    CredentialedTestnetTransport,
    CredentialEnvironment,
    CredentialPermission,
    InMemorySecretManager,
    reject_plaintext_credentials,
)
from crypto_stat_arb.runner import load_events
from crypto_stat_arb.sdk import (
    CrossVenueSDKStrategy,
    RuntimeMode,
    StrategyRuntime,
    TestnetVenueAdapter,
    immediate_fill_testnet_transport,
)


ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.value = 1_900_000_000_000_000_000

    def __call__(self):
        self.value += 1
        return self.value


class CredentialSecurityTests(unittest.TestCase):
    def broker(self, directory):
        manager = InMemorySecretManager()
        broker = CredentialBroker(
            manager,
            Path(directory) / "credential_metadata.json",
            Path(directory) / "credential_audit.jsonl",
            production=False,
            clock_ns=Clock(),
        )
        return broker, manager

    def register(self, broker, venue="BINANCE", credential_id="binance-testnet"):
        return broker.register(
            credential_id=credential_id,
            venue=venue,
            environment=CredentialEnvironment.TESTNET,
            permissions=(CredentialPermission.TRADE, CredentialPermission.CANCEL),
            secret_reference=f"testnet/{venue.lower()}/orders",
            secret=b"not-a-real-secret-v1",
            actor="security-owner",
        )

    def test_offline_backtest_rejects_any_credential_reference(self):
        config, _ = load_config(ROOT / "config/research.json")
        with self.assertRaisesRegex(ValueError, "offline backtests prohibit"):
            StrategyRuntime(
                config, CrossVenueSDKStrategy(config),
                mode=RuntimeMode.BACKTEST,
                credential_ids=("exchange-key",),
            )

    def test_withdrawal_scope_and_test_secret_manager_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "external secret manager"):
                CredentialBroker(
                    InMemorySecretManager(), Path(directory) / "metadata.json",
                    Path(directory) / "audit.jsonl",
                )
            broker, _ = self.broker(directory)
            with self.assertRaisesRegex(ValueError, "withdrawal permission is prohibited"):
                broker.register(
                    credential_id="unsafe", venue="BYBIT",
                    environment=CredentialEnvironment.TESTNET,
                    permissions=(CredentialPermission.TRADE, CredentialPermission.WITHDRAW),
                    secret_reference="testnet/bybit/unsafe",
                    secret=b"not-real", actor="security-owner",
                )

    def test_plaintext_secret_fields_are_rejected_from_configuration(self):
        with self.assertRaisesRegex(ValueError, "plaintext credential field"):
            reject_plaintext_credentials({
                "venue": {"api_key": "must-not-be-here"}
            })
        reject_plaintext_credentials({
            "venue": {"credential_id": "binance-testnet"}
        })

    def test_injection_rotation_and_revocation_are_audited_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            broker, _ = self.broker(directory)
            self.register(broker)
            retained_views = []

            def consume(secret):
                retained_views.append(secret)
                return bytes(secret)

            self.assertEqual(broker.inject(
                "binance-testnet", actor="connector", venue="BINANCE",
                environment=CredentialEnvironment.TESTNET,
                required_permissions=(CredentialPermission.TRADE,),
                consumer=consume,
            ), b"not-a-real-secret-v1")
            self.assertEqual(bytes(retained_views[-1]), b"\x00" * 20)

            broker.rotate(
                "binance-testnet", b"not-a-real-secret-v2", actor="security-owner"
            )
            self.assertEqual(broker.inject(
                "binance-testnet", actor="connector", venue="BINANCE",
                environment=CredentialEnvironment.TESTNET,
                required_permissions=(CredentialPermission.TRADE,),
                consumer=lambda secret: bytes(secret),
            ), b"not-a-real-secret-v2")
            broker.revoke(
                "binance-testnet", actor="security-owner", reason="rotation drill"
            )
            with self.assertRaisesRegex(ValueError, "revoked"):
                broker.inject(
                    "binance-testnet", actor="connector", venue="BINANCE",
                    environment=CredentialEnvironment.TESTNET,
                    required_permissions=(CredentialPermission.TRADE,),
                    consumer=lambda secret: bytes(secret),
                )

            metadata_text = broker.metadata_path.read_text()
            audit_text = broker.audit_path.read_text()
            self.assertNotIn("not-a-real-secret", metadata_text + audit_text)
            rows = [json.loads(line) for line in audit_text.splitlines()]
            self.assertEqual([row["action"] for row in rows], [
                "REGISTER", "INJECT", "ROTATE", "INJECT", "REVOKE", "INJECT",
            ])
            self.assertEqual(rows[-1]["outcome"], "DENIED")
            self.assertTrue(all(not row["secret_material_logged"] for row in rows))
            for previous, current in zip(rows, rows[1:]):
                self.assertEqual(current["previous_hash"], previous["record_hash"])

    def test_credential_is_injected_only_inside_testnet_adapter_call(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _ = load_config(ROOT / "config/research.json")
            broker, _ = self.broker(directory)
            observed = []
            adapters = {}
            for venue in config.venues:
                credential_id = f"{venue.lower()}-testnet"
                self.register(broker, venue, credential_id)

                def native_transport(request, secret, expected_venue=venue):
                    observed.append((expected_venue, bytes(secret)))
                    return immediate_fill_testnet_transport(request)

                secure_transport = CredentialedTestnetTransport(
                    broker, credential_id, venue, native_transport,
                )
                adapters[venue] = TestnetVenueAdapter(venue, secure_transport)

            runtime = StrategyRuntime(
                config, CrossVenueSDKStrategy(config),
                mode=RuntimeMode.TESTNET, adapters=adapters,
                credential_ids=tuple(adapters),
            )
            result = runtime.run(load_events(ROOT / "data/samples/two_venue_books.jsonl"))
            self.assertEqual(result.opportunity_count, 1)
            self.assertEqual({venue for venue, _ in observed}, set(config.venues))
            self.assertTrue(all(secret == b"not-a-real-secret-v1" for _, secret in observed))
            audit = broker.audit_path.read_text()
            self.assertNotIn("not-a-real-secret", audit)


if __name__ == "__main__":
    unittest.main()
