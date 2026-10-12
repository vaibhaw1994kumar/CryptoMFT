# Publishing hft-platform-kit

## Build and validate

```bash
uv sync --frozen
uv run --frozen pytest -q
uv build --clear
```

Expected artifacts include an sdist and one native wheel. The exact wheel tags
depend on the build interpreter and platform:

```text
dist/hft_platform_kit-<version>-<python>-<abi>-<platform>.whl
dist/hft_platform_kit-<version>.tar.gz
```

Do not publish a `py3-none-any` wheel: this package contains
`hft_platform_kit._ems`.

## Release wheels for every platform

`uv build` produces a wheel only for the machine that runs it. A release must
instead ship wheels for Linux, macOS, and Windows, which is what
`.github/workflows/wheels.yml` does with `cibuildwheel`:

- targets CPython 3.12 and 3.13 (`[tool.cibuildwheel] build`)
- skips 32-bit, PyPy, and musllinux targets, which have no
  `pyarrow`/`hftbacktest`/`llvmlite` wheels
- runs the test suite on Linux, macOS, and Windows runners, including the
  slow-marked end-to-end run of AT-01 to AT-14
- keeps a downloadable evidence bundle from a Linux run, stamped with the
  commit under test
- imports `hft_platform_kit._ems` from each built wheel before uploading it

A `v*` tag attaches the `wheels-*` and `sdist` artifacts to a GitHub release
automatically, so Windows consumers install a prebuilt `win_amd64` wheel
rather than compiling the extension with MSVC. Downloading them by hand is
only needed to install or inspect an artifact outside a release.

Windows uses `winloop` in place of `uvloop`, which publishes no Windows
wheels. Verify the Windows dependency set resolves without building anything:

```bash
uv pip compile pyproject.toml --python-platform windows \
  --python-version 3.12 --no-build -o /dev/null
```

The sdist intentionally includes the root CMake project, `cpp/`, Markdown
documentation, `examples/`, and `config*.yaml`. It excludes virtual
environments, build outputs, caches, runtime `data/` and `reports/`, Office
temporary files, logs, editor state, and other machine-local artifacts.
Run the packaging regression before publishing:

```bash
uv run pytest -q tests/test_packaging.py
```

## Install the wheel locally

```bash
WHEEL="$(find dist -maxdepth 1 -type f \
  -name 'hft_platform_kit-*.whl' -print -quit)"
test -n "$WHEEL"
case "$WHEEL" in
  *-py3-none-any.whl) echo "unexpected pure-Python wheel: $WHEEL"; exit 1 ;;
esac

python - "$WHEEL" <<'PY'
import sys
from zipfile import ZipFile

with ZipFile(sys.argv[1]) as archive:
    members = archive.namelist()
    assert any(
        name.startswith("hft_platform_kit/_ems") and name.endswith((".so", ".pyd"))
        for name in members
    )
PY

VERIFY_ENV="$HOME/.cache/hft-platform-kit-wheel-test"
rm -rf "$VERIFY_ENV"
uv venv "$VERIFY_ENV" --python 3.12
uv pip install \
  --python "$VERIFY_ENV/bin/python" \
  "$WHEEL"

"$VERIFY_ENV/bin/python" -c \
  "import hft_platform_kit._ems as e; print(e.__file__)"
"$VERIFY_ENV/bin/hft-platform-kit" ems-demo
"$VERIFY_ENV/bin/hft-platform-kit" --help
"$VERIFY_ENV/bin/trading-platform-poc" --help
rm -rf "$VERIFY_ENV"
```

## Publish to TestPyPI

TestPyPI is a public index. Uploading there publishes the sdist, and the
sdist carries the full source, so this is a disclosure decision rather than a
dry run. Skip it while the repository is private; the wheel-install check
above already validates the artifact locally.

Create a TestPyPI API token and export it:

```bash
export UV_PUBLISH_TOKEN="pypi-..."
```

Publish:

```bash
uv publish \
  --publish-url https://test.pypi.org/legacy/ \
  --check-url https://test.pypi.org/simple/ \
  dist/*
```

Install from TestPyPI while resolving dependencies from PyPI:

```bash
pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  hft-platform-kit
```

## Publishing to an index

This package is **not** published to PyPI. The repository is private and a
PyPI upload is public and permanent: the sdist carries the full source, and a
version, once uploaded, cannot be deleted and re-used. Releases therefore go
to GitHub release assets and to GHCR, both of which inherit the repository's
access control.

Tagging performs the release. `.github/workflows/wheels.yml` runs the tests,
the acceptance suite, `cibuildwheel` and the sdist build on every push, and on
a `v*` tag adds a `release` job that:

- refuses a tag that disagrees with `project.version`, because an artifact
  published under the wrong number cannot be corrected afterwards
- re-verifies the evidence bundle from the `acceptance` job
- creates a GitHub release carrying every wheel, the sdist and
  `hft-platform-kit-<version>-evidence.zip`
- takes its notes from the matching `CHANGELOG.md` section, so a tag without
  one fails rather than publishing an empty description

`.github/workflows/image.yml` publishes the container image to GHCR on the
same tag.

If the package is ever meant to be public, publish by hand from the release
artifacts rather than adding a token to CI:

```bash
export UV_PUBLISH_TOKEN="pypi-..."
uv publish dist/*
```

Prefer PyPI trusted publishing over a stored token if that day comes: it
exchanges a workflow's OIDC identity for a short-lived credential, needs a
`pypi` GitHub environment and a publisher registered on PyPI, and for a
project that does not yet exist it must be registered as a *pending*
publisher from the PyPI account page. Note that a pending publisher does not
reserve the name; another user can still take it before the first upload.

Do not store tokens in shell history, source files, configuration, or CI logs.

## Release checklist

1. Update `project.version` in `pyproject.toml`.
2. Update the `_package_version` source-checkout fallback in
   `src/hft_platform_kit/__init__.py`, the pinned literal in
   `tests/test_public_sdk.py`, and the `hft-platform-kit` entry in `uv.lock`
   (edit that one line by hand; re-resolving the lock rewrites index URLs).
3. Add a `## <version> - <date>` section to `CHANGELOG.md`. The release job
   fails without it.
4. Run the complete tests and `uv run --frozen trading-platform-poc
   mr-acceptance --output <dir>`.
5. Run `tests/test_packaging.py` and build wheel and sdist with `--clear`.
6. Confirm the sdist has no local/build/runtime artifacts.
7. Confirm the wheel is platform-tagged, install it in a clean environment,
   inspect it for `hft_platform_kit/_ems`, and import the extension.
8. Run the native scheduling/SOR sample or `hft-platform-kit ems-demo`.
9. Verify both CLI entry points and a public SDK import.
10. Push an annotated `v<version>` tag. That builds the GitHub release and
    the GHCR image; nothing else needs to be run by hand.

Package names cannot be reused after another owner publishes them. The
`hft-platform-kit` name was not present on PyPI when this package was prepared,
but availability must be checked again immediately before publication.
