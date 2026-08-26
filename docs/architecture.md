# Architecture

## Data flow

```text
browser / API
    -> normalize visible text
    -> split at natural boundaries without dropping characters
    -> atomically persist tasks/<job_id>/manifest.json
    -> one in-process queue, one segment at a time
    -> lazy-load IndexTTS2.5 and use one validated reference audio per task
    -> atomically persist each 22.05 kHz WAV segment
    -> concatenate WAV files in manifest order with configured pauses
    -> transcode final WAV to MP3 with FFmpeg
    -> create UTF-8 SRT cues from measured segment durations and configured pauses
```

The browser is a custom view and command client, not the upstream official Gradio WebUI. The filesystem-backed manifest is authoritative, so a refresh or service restart reconstructs the job list without relying on JavaScript memory.

## Components

| Component | Responsibility |
| --- | --- |
| `core/text.py` | Decode text files, remove Markdown decoration, normalize text, split and verify order/content integrity |
| `core/manifest.py` | Job/segment schema, hashes, validation and atomic JSON replacement |
| `core/queue.py` | Single sequential worker, controls, retry, durable segment transitions and structured events |
| `core/tts.py` | Lazy IndexTTS-2.5 adapter, deterministic seed scope and test generator |
| `core/voices.py` | Official reference-audio presets, user reference library, preview WAV cache and path validation |
| `core/audio.py` | Validate/merge compatible WAV files and atomically produce MP3 through FFmpeg |
| `core/subtitles.py` | Split readable cues and atomically create UTF-8-BOM SRT files from measured WAV durations |
| `app/web.py` | FastAPI projection, validation, downloads and queue commands |
| `app/static/` | Stateless browser workbench |

## Persistence and recovery

Each task directory contains its manifest, segment WAV files, final WAV/MP3/SRT outputs and `job.log.jsonl`. Manifest writes use a temporary file, `fsync`, and atomic replacement. A successful segment is reused only when its referenced audio file still exists; a missing file returns to pending. Final outputs are rebuilt after retry or resume, and stale final files are hidden before rebuilding. SRT timing uses each segment WAV's measured duration plus the configured inter-segment pause; shorter cues inside a segment are timed proportionally because IndexTTS does not expose word timestamps. Display text keeps internal punctuation but strips trailing Unicode punctuation. Punctuation-only pieces contribute timing to an adjacent cue instead of becoming empty or orphan subtitles. A manifest subtitle format version triggers lazy regeneration for legacy completed jobs.

Reference audio is copied into the ignored `voices/references/` directory for user-saved voices. The manifest stores the selected server-side reference path for resuming, while API projections omit that absolute path. Built-in references are resolved from the official IndexTTS example directory. Voice identity comes from real reference audio rather than a numeric speaker seed.

Restarting the service does not automatically enqueue incomplete jobs. The user selects the task and resumes it. Already successful segments with valid files are skipped. The manifest stores original and normalized text in plain text, so task directories are private runtime data rather than publishable artifacts.

## Concurrency boundary

The queue and pause/cancel signals are in-process, while manifests and audio are durable. Run exactly one Uvicorn worker and one Workbench service against a task directory. The official upstream Gradio service is a separate alternative on port 7860; launchers intentionally refuse to run both services at once because they share the model environment and selected GPU. Multiple processes can race on the same manifest or GPU.

Pause and cancel are cooperative: the current IndexTTS call finishes and is persisted before the worker stops. Retry resets every failed segment to pending and preserves successful segments.

IndexTTS also has an internal token-based text splitter. The workbench's outer character splitter exists for visible progress, pause/retry boundaries and merge control; `interval_silence=0` is used inside the adapter so pauses are inserted only by the workbench merger.

## State model

Jobs use `created`, `queued`, `running`, `paused`, `cancelled`, `completed`, and `failed`. Segments use `pending`, `running`, `succeeded`, `failed`, and `cancelled`. The HTTP projection maps internal segment `succeeded` to frontend `success`; progress counts only successful segments.

## Security boundary

The supported launcher binds to `127.0.0.1` and access is through an SSH tunnel. Paths returned by manifests are resolved beneath their job directory before download. The repository ignores runtime text, audio, logs, model weights and local secrets. It does not provide authentication and must not be exposed directly to a shared network or the public internet.
