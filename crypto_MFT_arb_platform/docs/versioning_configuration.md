# Versioning, configuration and paper releases

`PaperReleaseRegistry` creates immutable, content-addressed paper releases. A release is not only a strategy tag: it snapshots and hashes every input needed to identify the decision and simulation environment.

## Required version set

| Artifact class | What is recorded |
| --- | --- |
| Strategy code | Logical name, explicit version, source path, immutable snapshot, SHA-256 and size |
| Feature definitions | One or more versioned calculation definitions with the same snapshot metadata |
| Models | Model binary or an explicit rules-model manifest; rules-based strategies cannot silently omit this class |
| Parameters | Entry/exit/risk and sizing parameter version |
| Data manifest | Dataset references, schema versions, counts and source hashes |
| Simulator configuration | Matching, latency, fee, OMS/RMS and venue configuration used by the paper runtime |

The release also records the strategy ID, build version, named environment, Python/runtime platform and a manifest SHA-256. Its deterministic release ID is derived from logical versions and content hashes, not the machine path. Creating the same content twice is idempotent.

## Approval and activation

Creating a release only produces a draft snapshot. `approve` requires a named approval owner and first verifies every artifact and the manifest. `deploy` requires that approval and records build, configuration manifest, environment, approval owner and deployment owner in an fsync-backed, hash-chained audit ledger. `active_release.json` is atomically replaced so observers never read a partial pointer.

Tampered, missing, incomplete or unapproved releases fail closed. Six artifact classes are mandatory.

## Rollback and reproduction

Rollback only accepts a different, known, verified and approved release. It requires a deployment owner and reason, records both source and target release IDs, and atomically makes the target active. Risk/position reconciliation remains a separate operational prerequisite before resuming a paper strategy.

`reproduce` verifies the stored release, materializes all exact artifact snapshots into an empty directory and writes `reproduction_manifest.json`. This reconstructs the versioned input bundle; the SDK or paper runner can then be launched from those pinned inputs.

The automated acceptance test creates and approves release v1, deploys v1, creates/deploys changed v2, rolls back to v1, verifies the audit hash chain and proves that reproduction restores v1's original parameter value.

## CLI

Artifact arguments use `KIND:NAME:VERSION=PATH`. Repeat `--artifact` once or more for each required class.

```bash
PYTHONPATH=src python3 -m crypto_stat_arb.cli paper-release create \
  --registry data/paper_releases \
  --strategy-id cross_venue_btc_v1 \
  --build-version 0.1.0 \
  --environment paper-research \
  --artifact STRATEGY_CODE:cross_venue_strategy:1.0.0=src/crypto_stat_arb/strategy.py \
  --artifact FEATURE_DEFINITION:top_of_book:1.0.0=config/releases/cross_venue_features_v1.json \
  --artifact MODEL:cross_venue_rules:1.0.0=config/releases/rules_model_v1.json \
  --artifact PARAMETERS:cross_venue_parameters:1.0.0=config/releases/cross_venue_parameters_v1.json \
  --artifact DATA_MANIFEST:two_venue_sample:1.0.0=data/manifests/two_venue_sample_v1.json \
  --artifact SIMULATOR_CONFIG:paper_simulator:1.0.0=config/research.json
```

Use the returned release ID:

```bash
PYTHONPATH=src python3 -m crypto_stat_arb.cli paper-release approve --registry data/paper_releases --release-id RELEASE_ID --owner portfolio-manager
PYTHONPATH=src python3 -m crypto_stat_arb.cli paper-release deploy --registry data/paper_releases --release-id RELEASE_ID --owner paper-operator
PYTHONPATH=src python3 -m crypto_stat_arb.cli paper-release verify --registry data/paper_releases --release-id RELEASE_ID
PYTHONPATH=src python3 -m crypto_stat_arb.cli paper-release rollback --registry data/paper_releases --release-id PREVIOUS_RELEASE_ID --owner paper-operator --reason "restore accepted release"
PYTHONPATH=src python3 -m crypto_stat_arb.cli paper-release reproduce --registry data/paper_releases --release-id PREVIOUS_RELEASE_ID --output-dir runs/reproduced_release
```

The release manifest contract is `docs/schemas/paper_release_manifest.schema.json`; a compact acceptance result is under `data/samples/releases/rollback_acceptance.json`.
