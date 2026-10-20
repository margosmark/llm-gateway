# Personal LLM Gateway - immutable candidate image.
#
# There is no dependency install step, because the project has no third-party
# runtime dependency. The base image is pinned by digest so a rebuild of the
# same source produces the same layers.
#
# Build:
#   docker build \
#     --build-arg SOURCE_REVISION="$(git rev-parse HEAD)" \
#     --build-arg SOURCE_TREE="$(git rev-parse HEAD^{tree})" \
#     -t personal-llm-gateway:candidate .
#
# The image digest - not the tag - identifies the candidate. The release step
# writes it into the persistent volume so /version reports the exact
# artifact that is running.

FROM python@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

ARG SOURCE_REVISION=unknown
ARG SOURCE_TREE=unknown

LABEL org.opencontainers.image.title="personal-llm-gateway" \
      org.opencontainers.image.description="Personal, LAN-only OpenAI-compatible LLM gateway" \
      org.opencontainers.image.source="https://github.com/margosmark/llm-gateway" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.licenses="UNLICENSED"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    GATEWAY_DATA_DIR=/var/lib/gateway \
    GATEWAY_SOURCE_REVISION=${SOURCE_REVISION} \
    GATEWAY_SOURCE_TREE=${SOURCE_TREE}

# The runtime directory holds the configuration and the owner-only credential
# store. It is a volume, not part of the image.
RUN mkdir -p /var/lib/gateway && \
    groupadd --system --gid 10001 gateway && \
    useradd --system --uid 10001 --gid gateway --home /nonexistent --shell /usr/sbin/nologin gateway && \
    chown gateway:gateway /var/lib/gateway && \
    chmod 700 /var/lib/gateway

WORKDIR /app
COPY --chown=root:root src/ /app/src/
COPY --chown=root:root data/ /app/data/
COPY --chown=root:root scripts/ /app/scripts/

# Non-root, no shell, no new privileges.
USER 10001:10001

EXPOSE 8765 8766

# The healthcheck reads the metadata endpoint, which is anonymous. It uses
# the configured bind address so it works whether the gateway binds a LAN
# address or loopback.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; h=os.environ.get('GATEWAY_BIND','127.0.0.1'); p=os.environ.get('GATEWAY_PORT','8765'); sys.exit(0 if urllib.request.urlopen(f'http://{h}:{p}/health', timeout=4).status==200 else 1)"]

ENTRYPOINT ["python", "-m", "gateway"]
# No arguments: the bind address, the ports and the data directory all come
# from the environment, so the same image runs unchanged on any host. With
# host networking the gateway binds the explicit private LAN address
# directly - there is no flag that permits a wildcard bind.
CMD []
