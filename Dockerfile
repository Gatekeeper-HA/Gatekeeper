FROM python:3.11.16-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NNPACK_DISABLE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# Dependencies first, so code changes don't reinstall torch.
COPY requirements.lock .
RUN pip install -r requirements.lock

COPY pyproject.toml VERSION README.md LICENSE ./
COPY src ./src
RUN pip install --no-deps .

# Startup (loading Kokoro and Whisper, pre-synthesizing phrases) takes 1-3 min on a CPU.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5m --retries=3 \
    CMD ["python", "-m", "gatekeeper.health"]

CMD ["python", "-m", "gatekeeper"]
