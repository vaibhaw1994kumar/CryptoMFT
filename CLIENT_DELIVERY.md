# Sharing this platform with a client

The claim this repository makes is not "it computes a PnL number". It is
**"identical inputs produce identical outputs, and here is the evidence"** —
AT-06, the manifest checksums, and the build identity stamped into every
report.

That claim decides the delivery format. A number produced on a server the
client cannot inspect proves nothing, because they cannot tell which build,
seed, dataset or configuration produced it. A number they reproduce on their
own machine, matching your manifest hash, proves the whole thesis in one
command. **Ship something they run, not something they call.**

---

## Route 1 — Container image (recommended)

One command, no Python, no CMake, no compiler, no proxy fight:

```bash
docker run --rm -v "$PWD/out:/out" ghcr.io/ankurarrah/hft-platform-kit:0.3.0
```

This runs AT-01 to AT-14 and writes into `./out`:

| Artifact | What it is |
| --- | --- |
| `acceptance_report.md` | Signed report: results, owners, defect log, limitations, remediation dates, sign-off |
| `acceptance_report.json` | The same, machine-readable |
| `deliverables/` | The eight Section 10 documents (venue matrix, schemas, security master, fees, risk matrix, state machine, monitoring, features) |
| `at01/` … `at14/` | Per-test evidence, including raw and canonical data, ledgers, manifests and TCA |

The command exits non-zero if any test fails, so it works unattended in the
client's own CI.

To prove reproducibility rather than assert it, have them run it twice and
compare:

```bash
docker run --rm -v "$PWD/run1:/out" ghcr.io/ankurarrah/hft-platform-kit:0.3.0
docker run --rm -v "$PWD/run2:/out" ghcr.io/ankurarrah/hft-platform-kit:0.3.0
diff <(jq -S '.checks[] | select(.test_id=="AT-06") | .detail' run1/acceptance_report.json) \
     <(jq -S '.checks[] | select(.test_id=="AT-06") | .detail' run2/acceptance_report.json)
```

The image is built by `.github/workflows/image.yml`, which builds it, runs the
acceptance suite **inside** it, and verifies the bundle before publishing. The
commit is stamped into the image, so `build.git_revision` in the client's
report names the exact source that produced it.

**Access.** The repository is private, so the GHCR package it publishes is
private too and the `docker run` above fails with `unauthorized` for anyone
else. Pick one:

- Give the client a token with `read:packages` and have them
  `docker login ghcr.io` with it. Access stays revocable and auditable, and
  the repository stays closed.
- Send the image offline, which needs no registry access at all:

  ```bash
  docker pull ghcr.io/ankurarrah/hft-platform-kit:0.3.0
  docker save ghcr.io/ankurarrah/hft-platform-kit:0.3.0 \
    | gzip >hft-platform-kit-0.3.0-image.tar.gz
  # client side
  docker load <hft-platform-kit-0.3.0-image.tar.gz
  ```

Making the package public is a third option, but a Python wheel ships
readable source, so publishing the image publishes the code. Treat it as the
same decision as making the repository public.

Individual commands work too — the entrypoint is the CLI:

```bash
docker run --rm -v "$PWD/out:/out" ghcr.io/ankurarrah/hft-platform-kit:0.3.0 \
  mr-backtest --output /out/run --steps 400 --seed 11
```

## Route 2 — Evidence bundle only

For a client who wants to read rather than run, send
`hft-platform-kit-<version>-evidence.zip`, which the `release` job attaches to
every tagged GitHub release, or download the `acceptance-evidence` artifact
from a `wheels` or `image` workflow run. `acceptance_report.md` is designed to
be read on its own: it opens with the build identity and a pass/fail table,
and closes with the defect log, accepted limitations and the sign-off matrix.

This is the one route that needs no access grant at all — the zip is a file
you send.

## Route 3 — Wheels and the SDK

For a client who wants to write a strategy against the engine, ship the
wheels, not a service. `.github/workflows/wheels.yml` builds Linux, macOS and
Windows wheels with `cibuildwheel`, and a tagged release attaches them
directly. This package is deliberately not on PyPI: the repository is private
and a PyPI upload is public and permanent. Send the wheel for the client's
platform, or grant repository access so they can download the release. The
strategy and OMS/EMS interfaces are documented in `SDK_API.md`.

## Route 4 — Live walkthrough

`MR_DEMO_GUIDE.md` is a rehearsed fifteen-minute demo with the expected output
of every command already recorded, so a wrong number on stage is visible
immediately.

---

## Why not host it behind an API

It is the natural instinct, and it is the wrong first move here.

- **It destroys the claim.** The product is reproducibility. An endpoint
  returns a number the client must take on trust, with no way to tie it to a
  build, seed or dataset. Running it themselves is the proof; a response
  payload is the opposite of proof.
- **The requirements document does not grant that posture.** Section 11
  defers "HSM integration, full RBAC/MFA/JIT governance, regulatory
  reporting, surveillance", and Section 12 states that live-ready "is not
  granted by this document". A public endpoint ships precisely the governance
  surface that has not been built.
- **It invites a dangerous misreading.** A live socket on a trading platform
  reads as live trading. The document is explicit that this release does not
  authorize production capital, and a hosted endpoint spends the meeting
  walking that back.
- **The shape is wrong.** Runs take twenty seconds to two minutes. Wrapping
  batch jobs in a request/response API means a job queue, authentication,
  object storage and a status endpoint — real infrastructure, no added
  evidentiary value.

If the client wants a button rather than a command line, use a
`workflow_dispatch` GitHub Actions run: sandboxed, logged, tied to a commit,
and it emits a downloadable evidence bundle — the audit trail you want,
without an endpoint to secure.

Build a real service only when the client wants to run *their* strategy
against the engine continuously. At that point the answer is still the SDK
plus a deployment they control, and it needs the deferred authentication,
authorization and secret-management work from Section 11 first.

---

## Before you send it

1. `uv run --frozen pytest -q` — the full suite.
2. `uv run --frozen trading-platform-poc mr-acceptance --output out` — expect
   14/14.
3. Confirm `out/acceptance_report.md` shows a real `package_version` and
   `git_revision`, not `0.0.0+local` or `unavailable`.
4. Give the client read access to the repository, a `read:packages` token, or
   send the bundle and image tarball alone. Do not make the repository public
   to share it, and remember that publishing the image or a wheel to a public
   index discloses the source just as effectively.
5. Offline backtests use no exchange credentials, and the acceptance suite is
   hermetic — `tests/conftest.py` blocks outbound sockets. There is nothing
   to redact, but confirm `config.yaml` carries no account identifiers before
   sending.
