# Verified Handoff

Last updated: 2026-08-24. This repository contains the independent IndexTTS-2.5 long-audio workbench. The server deployment uses `/data1/ximizhou/indextts-workbench`, official source `/data1/ximizhou/indextts`, model `/data1/ximizhou/indextts/checkpoints`, and environment `/data1/ximizhou/envs/conda/indextts`.

## Completed

- The active ChatTTS deployments were removed after exact process and path checks. Recoverable runtime-only data is archived at `/data1/ximizhou/tts-archive/chattts-20260824`.
- Official IndexTTS source is fixed at commit `ee40fa7d6c6b8a2c7f06105f9f1e65775b74868c`; the isolated Python 3.11 / torch 2.8.0+cu128 environment, IndexTTS-2.5 weights, auxiliary models and FFmpeg are installed.
- The responsive three-tab UI provides 11 official example references, uploaded/saved reference voices, emotion and sampling controls, durable long-text jobs, pause/resume/cancel/retry, segment playback and final WAV/MP3/SRT downloads.
- The upstream Gradio UI is available through a separate localhost-only wrapper on port 7860, with its own desktop entry, PID/log files, GPU selection and mutual exclusion against the workbench on port 8082. Its CLI arguments and installed Gradio 5.45.0 dependency were verified without starting a second model copy during the active workbench session.
- Server checks pass: `22 passed`, Python compilation and local JavaScript syntax validation.
- The launcher prefers physical GPU 3 whenever it meets the free-memory threshold, then falls back to the freest eligible GPU; both branches and a real GPU 3 start were verified.
- Real preview succeeded with a 5.06-second WAV. The 7,000-character task `20260824-155344-bcc3e0d8` completed 50/50 segments with zero failures and exact text integrity; its 1,256.456-second WAV and MP3 both decode with FFmpeg.
- UTF-8-BOM SRT generation uses measured segment WAV durations and configured pauses. A legacy completed job backfilled 236 cues whose final timestamp was within 1 ms of its 913.778-second WAV.
- Restart recovery was verified: two completed segments were preserved and only indices 2-11 were regenerated; the final 12-segment task retained exact text integrity.
