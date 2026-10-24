# syntax=docker/dockerfile:1

# Runtime image for the gateway. The gateway has no network listener: it speaks
# NDJSON over a Unix socket, so the state directory is the published interface,
# not a port. Mount it (and the identity secret) from the host.
FROM python:3.12-slim-bookworm

# nak is the relay transport — every relay worker is a `nak req` child — so it is
# part of the runtime, not an optional tool. Pinned and hash-checked: a moving
# `latest` would change the wire behaviour of a daemon with no other client.
ARG NAK_VERSION=v0.20.6
ARG NAK_SHA256=b44b36c792fbc3fb73b7ba3bbc94beda2219826271aa8d5f130f569c3817c3b9

# Match the host user that owns the mounted state directory and secret file.
# Override when they are not 1000:1000, or the daemon cannot create its socket
# and store on the volume: --build-arg UID=$(id -u) --build-arg GID=$(id -g).
ARG UID=1000
ARG GID=1000

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HNG_CONFIG=/etc/hermes-nostr-gateway/config.yaml

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o /usr/local/bin/nak \
      "https://github.com/fiatjaf/nak/releases/download/${NAK_VERSION}/nak-${NAK_VERSION}-linux-amd64" \
 && echo "${NAK_SHA256}  /usr/local/bin/nak" | sha256sum -c - \
 && chmod 0755 /usr/local/bin/nak

WORKDIR /app
COPY requirements.txt pyproject.toml README.md LICENSE ./
COPY src ./src
# requirements.txt first: it is the only install path carrying the patched
# nostr-sdk hash. The project install that follows resolves the remaining
# runtime dependencies (PyYAML, websockets, pydantic, structlog, media extras),
# and the assertion turns a resolver that swapped in the PyPI release of the
# same version into a build failure instead of a runtime Concord error.
RUN python -m pip install --no-cache-dir -r requirements.txt \
 && python -m pip install --no-cache-dir '.[media]' \
 && python -c "import nostr_sdk as sdk; assert callable(getattr(sdk, 'nip44_conversation_key', None)), 'the PyPI nostr-sdk is installed: nip44_conversation_key missing'"

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
