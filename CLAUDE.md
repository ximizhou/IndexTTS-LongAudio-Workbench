# Project Rules

IndexTTS LongAudio Workbench is a localhost-only FastAPI application backed by a durable, filesystem-based, single-worker generation queue. Read `README.md`, `docs/architecture.md`, `docs/api.md`, and `docs/server-12.md` before changing public behavior or deployment.

## Boundaries

- Keep the supported service bound to `127.0.0.1`; do not add public sharing, unauthenticated network exposure, or multiple Uvicorn workers.
- Never commit user text, task manifests, generated audio, voice-library data, logs, model weights, credentials or `.env` files. Runtime data belongs in ignored `tasks/`, `voices/`, `logs/`, and `run/`.
- IndexTTS source and model are external dependencies. Do not vendor upstream code, model weights, reference recordings or generated audio.
- Describe this UI as an independent workbench, never as the official IndexTTS Gradio WebUI.
- Preserve the core invariant: concatenating segment text in index order exactly equals normalized text. Persist state and outputs atomically where practical.
- Pause/cancel remain cooperative between segments; retry applies to all failed segments. Document intentional contract changes in README, architecture, API guide and server runbook.

## Verification

Run `python -m compileall -q app core tests scripts`, `python -m pytest -q`, and `node --check app/static/app.js`. Real IndexTTS acceptance is server-only and requires explicit GPU authorization; follow `docs/server-12.md`, recheck `nvidia-smi`, and use `scripts/start.sh`/`scripts/stop.sh` rather than broad process commands.
