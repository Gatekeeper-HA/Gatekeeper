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

# Dependencies first, so code changes don't reinstall torch (nor CUDA library changes).
COPY requirements.lock ./
RUN pip install -r requirements.lock

# VARIANT=cuda (docker-compose.gpu.yml) adds the CUDA libraries that Whisper and the
# person detector (own_perception.py) need to run on an NVIDIA GPU (~1.5 GB), and swaps
# onnxruntime for its CUDA build (same version; both install the same package, so the CPU
# one goes first). CTranslate2 and onnxruntime find the libraries through
# LD_LIBRARY_PATH; on the CPU image those folders don't exist and it's ignored.
ARG VARIANT=cpu
COPY requirements-cuda.lock ./
RUN if [ "$VARIANT" = "cuda" ]; then \
      pip uninstall -y onnxruntime && pip install -r requirements-cuda.lock; \
    fi
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib:/usr/local/lib/python3.11/site-packages/nvidia/cuda_runtime/lib:/usr/local/lib/python3.11/site-packages/nvidia/cufft/lib:/usr/local/lib/python3.11/site-packages/nvidia/curand/lib:/usr/local/lib/python3.11/site-packages/nvidia/nvjitlink/lib

COPY pyproject.toml VERSION README.md LICENSE ./
COPY src ./src
RUN pip install --no-deps .

# Startup (loading Kokoro and Whisper, pre-synthesizing phrases) takes 1-3 min on a CPU.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5m --retries=3 \
    CMD ["python", "-m", "gatekeeper.health"]

CMD ["python", "-m", "gatekeeper"]
