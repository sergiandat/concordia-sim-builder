# syntax=docker/dockerfile:1
#
# Single-container deploy: FastAPI serves the API *and* the built SPA from
# frontend/dist, so there is no second origin and no CORS to configure.
#
# Targets Hugging Face Spaces (Docker SDK, port 7860, UID 1000) but the CMD
# honours $PORT, so the same image runs on Render/Fly/Cloud Run unchanged.

# ---------- stage 1: build the SPA ----------
FROM node:22-slim AS frontend-build

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


# ---------- stage 2: runtime ----------
FROM python:3.13-slim

# git is required at build time: requirements.txt installs gdm-concordia from a
# pinned git ref, not from PyPI. python:*-slim does not ship it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces runs the container as UID 1000.
RUN useradd -m -u 1000 user
USER user

ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    HF_HOME=/home/user/.cache/huggingface \
    PYTHONUNBUFFERED=1 \
    TOKENIZERS_PARALLELISM=false

WORKDIR $HOME/app

# CPU-only torch first, so sentence-transformers does not pull the default CUDA
# build (~2.5 GB of nvidia-* wheels that are dead weight on a CPU instance).
RUN pip install --no-cache-dir --user \
        torch --index-url https://download.pytorch.org/whl/cpu

COPY --chown=user requirements.txt ./
RUN pip install --no-cache-dir --user -r requirements.txt

# Bake the embedder into the image so a cold start does not download it and so
# the container needs no HF token at runtime.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

COPY --chown=user backend/ ./backend/
COPY --chown=user --from=frontend-build /build/dist ./frontend/dist

# Written at runtime. Ephemeral on every free tier — see DEPLOY.md.
RUN mkdir -p logs

EXPOSE 7860
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
