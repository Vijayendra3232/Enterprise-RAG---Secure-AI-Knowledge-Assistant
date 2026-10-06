# Multi-Stage Hardened Production Dockerfile (Backend context)
# Stage 1: Dependency Builder
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    python3-dev \
    libpq-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV HF_HOME=/root/.cache/huggingface

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --user -r requirements.txt

# Pre-download and cache ONNX embedding model weights and tokenizer into HF_HOME during image build
RUN PYTHONPATH=/root/.local/lib/python3.12/site-packages python3 -c \
    "from huggingface_hub import hf_hub_download, snapshot_download; \
snapshot_download(repo_id='sentence-transformers/all-MiniLM-L6-v2'); \
hf_hub_download(repo_id='sentence-transformers/all-MiniLM-L6-v2', filename='onnx/model.onnx')"

# Explicit build-stage model snapshot and artifact verification (Fails build immediately if incomplete)
RUN PYTHONPATH=/root/.local/lib/python3.12/site-packages python3 -c \
    "import os; \
repo_dir = '/root/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2'; \
snapshots_dir = os.path.join(repo_dir, 'snapshots'); \
assert os.path.isdir(snapshots_dir), f'CRITICAL: Snapshots directory missing at {snapshots_dir}'; \
snapshots = [os.path.join(snapshots_dir, d) for d in os.listdir(snapshots_dir) if os.path.isdir(os.path.join(snapshots_dir, d))]; \
assert len(snapshots) > 0, f'CRITICAL: No snapshot subdirectories found in {snapshots_dir}'; \
snapshot_dir = snapshots[0]; \
print(f'[Docker Build] Resolved snapshot directory: {snapshot_dir}'); \
tok_file = os.path.join(snapshot_dir, 'tokenizer.json'); \
assert os.path.isfile(tok_file), f'CRITICAL: tokenizer.json missing at {tok_file}'; \
onnx_file = os.path.join(snapshot_dir, 'onnx', 'model.onnx'); \
if not os.path.isfile(onnx_file): onnx_file = os.path.join(snapshot_dir, 'model.onnx'); \
assert os.path.isfile(onnx_file), f'CRITICAL: ONNX model missing at {onnx_file}'; \
artifacts = sorted(os.listdir(snapshot_dir)); \
print(f'[Docker Build] Verified ONNX snapshot artifacts ({len(artifacts)} files): {artifacts}'); \
print('[Docker Build] SUCCESS: Pure ONNX all-MiniLM-L6-v2 model snapshot is complete.')"

# Stage 2: Minimal Secure Runtime
FROM python:3.12-slim AS runtime

# Create non-root user and group
RUN groupadd -g 10001 appuser && \
    useradd -u 10001 -g appuser -s /bin/sh -d /home/appuser -m appuser

# Set environment
ENV PATH="/home/appuser/.local/bin:${PATH}" \
    PYTHONUNBUFFERED="1" \
    PYTHONDONTWRITEBYTECODE="1" \
    PYTHONPATH="/app" \
    HF_HOME="/home/appuser/.cache/huggingface" \
    HF_HUB_OFFLINE="1" \
    TRANSFORMERS_OFFLINE="1"

WORKDIR /app

# Copy dependencies from builder
COPY --from=builder --chown=appuser:appuser /root/.local /home/appuser/.local

# Copy pre-cached Hugging Face model files from builder
COPY --from=builder --chown=appuser:appuser /root/.cache/huggingface /home/appuser/.cache/huggingface

# Copy application source code
COPY --chown=appuser:appuser backend/ /app/

# Ensure scratch directory is writable for non-root execution
RUN mkdir -p /tmp/scratch && chown -R appuser:appuser /tmp/scratch

# Drop all privileges
USER 10001:10001

EXPOSE 10000

# Default entrypoint starts API service
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
