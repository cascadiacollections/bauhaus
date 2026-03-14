FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY models/download_models.sh models/
RUN bash models/download_models.sh

COPY styles/ styles/
COPY src/ src/

ENTRYPOINT ["python", "src/main.py"]
