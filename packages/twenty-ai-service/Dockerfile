FROM python:3.11-slim

WORKDIR /app

# System deps for tokenizers / torch wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pipelines ./pipelines
COPY routers ./routers
COPY main.py ./

EXPOSE 8001

# HF weights download on first startup; mount a volume on HF_HOME to cache them.
ENV HF_HOME=/app/.cache/huggingface

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
