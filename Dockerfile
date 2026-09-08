# syntax=docker/dockerfile:1

# Build stage: resolve dependencies into a venv we can copy wholesale.
#
# A compiler should not be needed -- every dependency currently ships a wheel for
# both linux/amd64 and linux/arm64, srp included -- but build tooling lives here
# rather than in the runtime image, so a dependency that later drops a wheel
# fails the build instead of bloating what ships.
ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Set to "[fmip]" to include the optional iCloud device backend (pyicloud and
# its rich/typer/click chain); the accessory path -- the reason this service
# exists -- needs none of it.
#
# Empty here so a local `docker build` stays lean, but the published image sets
# it (see .github/workflows/docker.yml): the backend is inert unless
# FINDMY_REST_ENABLE_FMIP turns it on, so shipping it costs a few MB and saves
# maintaining a second image variant.
ARG EXTRAS=""

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install ".${EXTRAS}"

# ── Runtime ──────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim

ARG VERSION=""
ARG COMMIT=""
ARG BUILD_TIME=""

LABEL org.opencontainers.image.title="findmy-rest" \
      org.opencontainers.image.description="REST API for Apple Find My accessory and device location and battery" \
      org.opencontainers.image.source="https://github.com/tsarna/findmy-rest" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${COMMIT}" \
      org.opencontainers.image.created="${BUILD_TIME}"

# The anisette provider emulates Apple libraries with Unicorn Engine, which is
# GPLv2. Its licence ships inside site-packages and must not be stripped.

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FINDMY_REST_STATE_DIR=/var/lib/findmy-rest \
    FINDMY_REST_KEYS_DIR=/etc/findmy-rest/accessories \
    FINDMY_REST_HOST=0.0.0.0 \
    FINDMY_REST_PORT=8080

COPY --from=builder /opt/venv /opt/venv

# Non-root, with a fixed uid so a PVC's ownership can be set once and stay right.
RUN useradd --system --uid 10001 --create-home --home-dir /home/findmy findmy \
    && mkdir -p /var/lib/findmy-rest /etc/findmy-rest/accessories \
    && chown -R findmy:findmy /var/lib/findmy-rest

USER 10001

# State must outlive the container: it holds the Apple session and the anisette
# identity. Losing it costs a password + 2FA round trip on every restart, and a
# stream of new "devices" logging in, which Apple rate-limits.
VOLUME ["/var/lib/findmy-rest"]

EXPOSE 8080

# No curl or wget in slim, and adding them for this would be silly.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('FINDMY_REST_PORT','8080')}/health\", timeout=4).status == 200 else 1)"]

CMD ["python", "-m", "findmy_rest"]
