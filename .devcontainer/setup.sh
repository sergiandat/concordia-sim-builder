#!/usr/bin/env bash
# Runs once when the Codespace is created.
set -euo pipefail

echo "==> CPU-only torch (the default build pulls ~2.5 GB of unused CUDA wheels)"
pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

echo "==> Python deps (installs gdm-concordia from a pinned git ref)"
pip install --no-cache-dir -r requirements.txt

echo "==> Pre-download the embedder so the first simulation does not stall"
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

echo "==> Build the SPA (FastAPI serves it from frontend/dist)"
cd frontend
npm ci
npm run build

echo
echo "Ready. Start the demo with:  ./run-demo.sh"
