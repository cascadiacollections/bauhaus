FROM ghcr.io/jdx/mise:latest AS mise

FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY --from=mise /usr/local/bin/mise /usr/local/bin/mise

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY .mise.toml ./
RUN mise trust && mise install

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY models/download_models.sh models/
RUN bash models/download_models.sh

COPY styles/ styles/
COPY src/ src/

ENTRYPOINT ["uv", "run", "python", "src/main.py"]
