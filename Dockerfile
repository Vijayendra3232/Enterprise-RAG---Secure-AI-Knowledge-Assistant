# Multi-Stage Hardened Production Dockerfile
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
RUN pip install --no-cache-dir --user torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir --user -r requirements.txt

# Pre-download and cache embedding model weights into HF_HOME during image build
RUN PYTHONPATH=/root/.local/lib/python3.12/site-packages python3 -c \
    "from langchain_huggingface import HuggingFaceEmbeddings; \
from huggingface_hub import snapshot_download; \
snapshot_download(repo_id='sentence-transformers/all-MiniLM-L6-v2'); \
HuggingFaceEmbeddings(model_name='all-MiniLM-L6-v2', model_kwargs={'trust_remote_code': True})"

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
req_files = ['config.json', 'modules.json']; \
missing_req = [f for f in req_files if not os.path.isfile(os.path.join(snapshot_dir, f))]; \
assert not missing_req, f'CRITICAL: Required model config files missing in snapshot: {missing_req}'; \
weight_files = ['model.safetensors', 'pytorch_model.bin']; \
found_weights = [w for w in weight_files if os.path.isfile(os.path.join(snapshot_dir, w))]; \
assert len(found_weights) > 0, f'CRITICAL: No model weight files ({weight_files}) found in snapshot {snapshot_dir}'; \
tok_files = ['tokenizer.json', 'tokenizer_config.json', 'vocab.txt']; \
found_tok = [t for t in tok_files if os.path.isfile(os.path.join(snapshot_dir, t))]; \
assert len(found_tok) > 0, f'CRITICAL: No tokenizer configuration files ({tok_files}) found in snapshot {snapshot_dir}'; \
artifacts = sorted(os.listdir(snapshot_dir)); \
print(f'[Docker Build] Verified snapshot artifact filenames ({len(artifacts)} files): {artifacts}'); \
print('[Docker Build] SUCCESS: all-MiniLM-L6-v2 model snapshot is complete.')"

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