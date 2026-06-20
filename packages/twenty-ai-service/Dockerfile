FROM python:3.11-slim

WORKDIR /app

# System deps for tokenizers / torch wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the whole service (.dockerignore keeps secrets/caches/tests out).
# main.py imports agent/, followup/, pipelines/, routers/, tracing.py,
# bridge_client.py — copying everything avoids missing-module crashes.
COPY . ./

EXPOSE 8001

# HF weights download on first startup; mount a volume on HF_HOME to cache them.
ENV HF_HOME=/app/.cache/huggingface

# Bind IPv6 (::) so Railway's private network can reach this service.
# Railway internal DNS (*.railway.internal) resolves to IPv6 only.
CMD ["uvicorn", "main:app", "--host", "::", "--port", "8001"]
