FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Bytecode-compile on install so the first run doesn't pay for it, and copy
# rather than hardlink since the cache and the venv are on different layers.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY models/download_models.py models/
RUN uv run --script models/download_models.py

COPY styles/ styles/
COPY src/ src/

# --no-sync: dependencies are already installed above, and without it `uv run`
# re-checks the lock on every container start, which needs network at runtime.
ENTRYPOINT ["uv", "run", "--no-sync", "python", "src/main.py"]
