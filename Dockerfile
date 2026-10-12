# Reproducible client image for the minimum-requirements platform.
#
# The client should be able to reproduce the acceptance evidence without
# installing Python, uv, CMake or a C++ compiler, and without fighting a
# corporate proxy. One command is the whole story:
#
#   docker run --rm -v "$PWD/out:/out" ghcr.io/ankurarrah/hft-platform-kit:latest
#
# That writes acceptance_report.md, the Section 10 deliverables and the
# per-test evidence into ./out, and exits non-zero if any test fails.
#
# The package builds a pybind11 extension (hft_platform_kit._ems), so CMake and
# a compiler are needed to produce the wheel but not to run it. Two stages keep
# the toolchain out of the image that ships.

# ---------------------------------------------------------------- build stage
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# scikit-build-core drives CMake, which needs a real toolchain. Without this
# the wheel build fails at the configure step.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        build-essential \
        cmake \
        ninja-build \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# An index override lets the image build behind a proxy that blocks PyPI.
ARG PIP_INDEX_URL=""
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

# CMakeLists.txt and cpp/ are part of the build, not decoration: omitting them
# makes the CMake configure step fail with a missing project file.
COPY pyproject.toml README.md LICENSE CMakeLists.txt ./
COPY cpp ./cpp
COPY src ./src

RUN python -m pip install --no-cache-dir build \
    && python -m build --wheel --outdir /dist

# -------------------------------------------------------------- runtime stage
FROM python:3.12-slim-bookworm AS runtime

# Stamped at build time so a report produced outside a git checkout can still
# name the commit that produced it. Anonymous evidence is not evidence.
ARG GIT_REVISION=unavailable
ENV HFT_PLATFORM_KIT_GIT_REVISION=${GIT_REVISION}

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG PIP_INDEX_URL=""
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

COPY --from=builder /dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir /tmp/*.whl \
    && rm -rf /tmp/*.whl \
    && python -c "import hft_platform_kit._ems"

COPY config.yaml /etc/hft-platform-kit/config.yaml

# The container writes evidence onto a bind mount the client owns, so it must
# not run as root or the output lands root-owned on their host.
RUN useradd --create-home --uid 1000 runner \
    && mkdir -p /out \
    && chown runner:runner /out
USER runner
WORKDIR /home/runner
VOLUME ["/out"]

LABEL org.opencontainers.image.title="hft-platform-kit" \
      org.opencontainers.image.description="Deterministic multi-venue statistical-arbitrage backtesting, paper trading and OMS/EMS acceptance evidence" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/ankurarrah/hft-platform-kit"

ENTRYPOINT ["trading-platform-poc"]
CMD ["mr-acceptance", "--output", "/out"]
