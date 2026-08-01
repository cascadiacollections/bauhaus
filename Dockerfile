FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy rather than hardlink, since the cache and the venv land on different
# layers. Deliberately NOT setting UV_COMPILE_BYTECODE: this image runs one
# batch generation and exits, so shaving import time off a process that then
# spends minutes in style transfer does not repay compiling all of torch on
# every build.
#
# only-system: the base image already pins the interpreter this project wants,
# so uv should use it rather than downloading a managed CPython of its own.
ENV UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-system

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
