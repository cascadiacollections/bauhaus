FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY models/download_models.sh models/
RUN bash models/download_models.sh

COPY styles/ styles/
COPY src/ src/

ENTRYPOINT ["uv", "run", "python", "src/main.py"]
