# Minimal credential security

Offline backtests have no exchange-credential path. `StrategyRuntime` rejects any credential reference in `BACKTEST` mode, and JSON configuration loading recursively rejects populated `api_key`, `api_secret`, `secret_key`, `private_key`, `passphrase`, `access_token` and `refresh_token` fields. Configuration may contain only non-secret credential IDs and external secret-manager references.

## Paper and testnet policy

| Control | Enforcement |
| --- | --- |
| Secret storage | `CredentialBroker` accepts a `SecretManager` interface; production mode rejects the bundled test-only in-memory backend |
| Least privilege | Every credential has explicit venue, environment and permission scopes; every injection requests only the scope needed for that action |
| Withdrawal | `WITHDRAW` is permanently prohibited at registration and injection |
| Environment isolation | Paper credentials cannot be injected into testnet or offline mode, and vice versa |
| Venue isolation | A credential registered for Binance cannot be injected into a Bybit adapter |
| Secret lifetime | The broker reads secret bytes only for one consumer callback and overwrites its mutable buffer afterward |
| Configuration/log safety | Metadata and audit files contain IDs, references and versions, never secret values or secret fingerprints |
| Production backend | `CallbackSecretManager` wraps Vault, KMS-backed or cloud secret-manager clients without importing provider credentials into the platform |

The platform intentionally does not provide an environment-variable or plaintext-file secret backend. `InMemorySecretManager` is marked test-only, and `CredentialBroker(production=True)` refuses it.

## Adapter injection

`CredentialedTestnetTransport` sits between `TestnetVenueAdapter` and the external authenticated transport. Submit/replace requires `TRADE`; cancel requires `CANCEL`. The strategy, SDK context, canonical order request, release manifest and monitoring output never receive the secret.

Python can only perform best-effort zeroization. The broker uses a temporary `bytearray`/`memoryview` and overwrites it in `finally`; external secret-manager and transport implementations must not retain or convert that view into long-lived immutable strings.

## Rotation, revocation and audit

Registration, successful access, denied access, failed access, rotation and revocation are written to an fsync-backed hash-chained JSONL audit. Rows include actor, credential ID, venue, environment, version, requested permission, outcome and reason, with `secret_material_logged=false`.

Rotation atomically advances metadata to a new secret-manager version and makes the prior version unavailable through the manager contract. Revocation removes the active version, marks metadata `REVOKED`, and causes subsequent injection to fail before connector dispatch.

Run the acceptance tests:

```bash
PYTHONPATH=src python3 -m unittest tests.test_credentials -v
```

The tests cover offline isolation, plaintext-config rejection, production-backend enforcement, withdrawal denial, adapter injection, zeroization, rotation, revocation, denied reuse and audit-chain integrity. They use obvious non-production test bytes and assert those bytes never appear in metadata or audit artifacts.

Published contracts are `docs/schemas/credential_metadata.schema.json` and `docs/schemas/credential_audit.schema.json`. A secret-free lifecycle result is at `data/samples/security/credential_lifecycle_acceptance.json`.
