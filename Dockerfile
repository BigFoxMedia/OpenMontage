# OpenMontage — agentic video production suite
# Homelab deploy fork of calesthio/OpenMontage (base: https://github.com/calesthio/OpenMontage).
# Serves the Backlot board (FastAPI) on 0.0.0.0:4750. The repo root is the
# production workspace: an AI coding agent operating this repo runs the
# pipelines; Backlot visualizes what they write to /app/projects.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BACKLOT_PORT=4750

# ffmpeg: composition/post-production. Node 20: Remotion + HyperFrames runtimes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Core Python deps. piper-tts is the free offline TTS path (best-effort: the
# Makefile treats it as optional too). GPU reqs intentionally excluded — this
# host has no GPU passthrough.
COPY requirements.txt .
# pytest: the demo driver (scripts/backlot_simulate_run.py) imports the
# contract fixtures from tests/, so the deployed image needs it at runtime —
# baked in so the demo works out of the box (previously required a manual
# `pip install pytest` inside the container, which a redeploy wiped).
RUN pip install -r requirements.txt \
    && pip install pytest \
    && (pip install piper-tts || echo "[skip] piper-tts unavailable - TTS falls back to cloud providers")

COPY . .

# Remotion composer (React-based composition runtime)
RUN cd remotion-composer && npm install --no-audit --no-fund

# Warm the HyperFrames npx cache (best-effort; offline-safe)
RUN npx --yes hyperframes --version >/dev/null 2>&1 || echo "[skip] hyperframes cache warm"

# Persist productions (checkpoints, artifacts, renders) across deploys
VOLUME /app/projects

EXPOSE 4750

# The repo's `python -m backlot serve` binds 127.0.0.1 by design (local-only
# board). For containerized use we run uvicorn directly on 0.0.0.0.
CMD ["uvicorn", "backlot.server:app", "--host", "0.0.0.0", "--port", "4750", "--log-level", "warning"]
