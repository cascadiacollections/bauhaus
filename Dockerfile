FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY models/download_models.py models/
RUN python models/download_models.py

COPY styles/ styles/
COPY src/ src/

ENTRYPOINT ["uv", "run", "python", "src/main.py"]
