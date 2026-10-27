# syntax=docker/dockerfile:1

# Runtime image for the gateway. The gateway has no network listener: it speaks
# NDJSON over a Unix socket, so the state directory is the published interface,
# not a port. Mount it (and the identity secret) from the host.
FROM python:3.12-slim-bookworm

# No transport binary in the image. Reads and publishes both run on the
# `nostr-sdk` client inside the process, so there is nothing to download at
# build time and no `curl` in the runtime.

# Match the host user that owns the mounted state directory and secret file.
# Override when they are not 1000:1000, or the daemon cannot create its socket
# and store on the volume: --build-arg UID=$(id -u) --build-arg GID=$(id -g).
ARG UID=1000
ARG GID=1000

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HNG_CONFIG=/etc/hermes-nostr-gateway/config.yaml

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt pyproject.toml README.md LICENSE ./
COPY src ./src
# requirements.txt first: it is the only install path carrying the patched
# nostr-sdk hash. The project install that follows resolves the remaining
# runtime dependencies (PyYAML, websockets, pydantic, structlog), and the
# assertion turns a resolver that swapped in the PyPI release of the same
# version into a build failure instead of a runtime Concord error.
RUN python -m pip install --no-cache-dir -r requirements.txt \
 && python -m pip install --no-cache-dir '.' \
 && python -c "import nostr_sdk as sdk; assert callable(getattr(sdk, 'nip44_v2_conversation_key', None)), 'the PyPI nostr-sdk is installed: nip44_v2_conversation_key missing'"

COPY config.example.yaml /etc/hermes-nostr-gateway/config.example.yaml

RUN groupadd --gid "${GID}" gateway \
 && useradd --uid "${UID}" --gid "${GID}" --create-home --shell /usr/sbin/nologin gateway \
 && mkdir -p /var/lib/hermes-nostr-gateway \
 && chown gateway:gateway /var/lib/hermes-nostr-gateway

USER gateway
VOLUME ["/var/lib/hermes-nostr-gateway"]

# `docker run <image> status` and `<image> --config … serve` both work: the
# config path also comes from HNG_CONFIG, so no wrapper script rewrites argv.
ENTRYPOINT ["hng"]
CMD ["serve"]
