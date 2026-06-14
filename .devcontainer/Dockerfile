FROM mcr.microsoft.com/devcontainers/base:bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://just.systems/install.sh | bash -s -- --to /usr/local/bin \
    && curl -fsSL https://astral.sh/uv/install.sh | sh \
    && curl -fsSL https://bun.sh/install | bash \
    && install -m 0755 /root/.local/bin/uv /usr/local/bin/uv \
    && ln -sf /root/.bun/bin/bun /usr/local/bin/bun \
    && ln -sf /root/.bun/bin/bunx /usr/local/bin/bunx
