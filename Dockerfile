FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Deliberately NOT setting UV_LINK_MODE=copy. The dependency set is ~5 GB of
# CUDA wheels, and copy mode duplicates every byte of it from the uv cache into
# .venv, so the layer stores the payload twice. That cost 196s of the 269s
# build in #100 — the layer commit alone, measured from the build log. uv's
# default hardlink mode is correct here: at build time the cache and the venv
# are on one overlay filesystem, not separate ones.
#
# only-system: the base image already pins the interpreter this project wants,
# so uv should use it rather than downloading a managed CPython of its own.
ENV UV_PYTHON_PREFERENCE=only-system

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
