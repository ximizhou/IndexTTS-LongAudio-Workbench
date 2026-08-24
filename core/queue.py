"""Single-GPU, sequential and resumable generation queue."""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .audio import merge_job_audio
from .manifest import JobManifest, SegmentRecord, utc_now
from .tts import IndexTTSGenerator


class SegmentGenerator(Protocol):
    """Generate one segment. Return WAV bytes or a path to a WAV file."""

    def __call__(
        self,
        text: str,
        *,
        segment_index: int,
        seed: int | None,
        params: Mapping[str, Any],
    ) -> bytes | bytearray | str | Path: ...


@dataclass(frozen=True)
class JobPaths:
    root: Path
    manifest: Path
    segments: Path
    output: Path
    log: Path

    @classmethod
    def for_job(cls, jobs_root: str | Path, job_id: str) -> "JobPaths":
        root = Path(jobs_root) / job_id
        return cls(root, root / "manifest.json", root / "segments", root / "output", root / "job.log.jsonl")

    def ensure(self) -> None:
        self.segments.mkdir(parents=True, exist_ok=True)
        self.output.mkdir(parents=True, exist_ok=True)


class JsonLogger:
    """A tiny structured logger which does not expose user text."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def event(self, event: str, **fields: Any) -> None:
        record = {"timestamp": utc_now(), "event": event, **fields}
        # Never log segment text; text remains only in the manifest.
        with self._lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def _write_wav_bytes(path: Path, value: bytes | bytearray) -> None:
    # Validate the RIFF/WAV envelope before persisting generator output.
    raw = bytes(value)
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("generator returned bytes that are not a WAV file")
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(raw)
        with wave.open(str(temporary), "rb") as reader:
            if reader.getnframes() <= 0:
                raise ValueError("generator returned an empty WAV file")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _materialize_wav(path: Path, value: bytes | bytearray | str | Path) -> None:
    if isinstance(value, (bytes, bytearray)):
        _write_wav_bytes(path, value)
        return
    source = Path(value)
    if not source.is_file():
        raise FileNotFoundError(source)
    temporary = path.with_name(f".{path.name}.tmp")
    shutil.copyfile(source, temporary)
    try:
        with wave.open(str(temporary), "rb") as reader:
            if reader.getnframes() <= 0:
                raise ValueError("generator returned an empty WAV file")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class JobStore:
    """Filesystem-backed manifest store for API processes and worker threads."""

    def __init__(self, jobs_root: str | Path) -> None:
        self.root = Path(jobs_root)
        self.root.mkdir(parents=True, exist_ok=True)

    def paths(self, job_id: str) -> JobPaths:
        return JobPaths.for_job(self.root, job_id)

    def create(self, manifest: JobManifest) -> JobManifest:
        paths = self.paths(manifest.job_id)
        paths.ensure()
        manifest.save(paths.manifest)
        return manifest

    def load(self, job_id: str) -> JobManifest:
        paths = self.paths(job_id)
        if not paths.manifest.is_file():
            raise FileNotFoundError(paths.manifest)
        return JobManifest.load(paths.manifest)

    def save(self, manifest: JobManifest) -> None:
        paths = self.paths(manifest.job_id)
        paths.ensure()
        manifest.save(paths.manifest)


class SequentialJobQueue:
    """One worker thread, one segment at a time, with crash-safe state."""

    def __init__(
        self,
        store: JobStore,
        generator: SegmentGenerator,
        *,
        ffmpeg: str = "ffmpeg",
        on_update: Callable[[JobManifest], None] | None = None,
    ) -> None:
        self.store = store
        self.generator = generator
        self.ffmpeg = ffmpeg
        self.on_update = on_update
        self._condition = threading.Condition()
        self._jobs: list[tuple[str, bool]] = []  # (job id, retry failed segments)
        self._controls: dict[str, dict[str, threading.Event]] = {}
        self._worker: threading.Thread | None = None
        self._shutdown = False

    def _controls_for(self, job_id: str) -> dict[str, threading.Event]:
        with self._condition:
            return self._controls.setdefault(job_id, {"pause": threading.Event(), "cancel": threading.Event()})

    def submit(self, manifest_or_id: JobManifest | str, *, retry_failed: bool = False) -> None:
        manifest = manifest_or_id if isinstance(manifest_or_id, JobManifest) else self.store.load(manifest_or_id)
        if isinstance(manifest_or_id, JobManifest):
            self.store.create(manifest)
        controls = self._controls_for(manifest.job_id)
        controls["pause"].clear()
        controls["cancel"].clear()
        if retry_failed:
            for segment in manifest.failed():
                segment.status = "pending"
                segment.error = None
                segment.audio_path = None
            manifest.error = None
            self.store.save(manifest)
        with self._condition:
            if not any(job_id == manifest.job_id for job_id, _ in self._jobs):
                self._jobs.append((manifest.job_id, retry_failed))
            manifest.set_status("queued")
            self.store.save(manifest)
            self._ensure_worker()
            self._condition.notify_all()

    def _ensure_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._shutdown = False
        self._worker = threading.Thread(target=self._run, name="indextts-queue", daemon=True)
        self._worker.start()

    def pause(self, job_id: str) -> None:
        controls = self._controls_for(job_id)
        controls["pause"].set()

    def cancel(self, job_id: str) -> None:
        controls = self._controls_for(job_id)
        controls["cancel"].set()

    def retry_failed(self, job_id: str) -> None:
        self.submit(job_id, retry_failed=True)

    def resume(self, job_id: str) -> None:
        self.submit(job_id, retry_failed=False)

    def stop(self, *, wait: bool = True) -> None:
        with self._condition:
            self._shutdown = True
            for controls in self._controls.values():
                controls["cancel"].set()
            self._condition.notify_all()
        if wait and self._worker:
            self._worker.join(timeout=30)

    def wait(self, job_id: str, timeout: float | None = None) -> JobManifest:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            manifest = self.store.load(job_id)
            if manifest.status in {"completed", "failed", "cancelled", "paused"}:
                return manifest
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if remaining == 0:
                return manifest
            time.sleep(min(0.2, remaining) if remaining is not None else 0.2)

    def _run(self) -> None:
        while True:
            with self._condition:
                if not self._jobs:
                    if self._shutdown:
                        return
                    self._condition.wait(timeout=0.5)
                    continue
                job_id, retry_failed = self._jobs.pop(0)
            try:
                self._run_job(job_id)
            except Exception:
                # A job-specific failure is persisted by _run_job.  Keep the
                # worker alive for later jobs rather than taking down the API.
                logging.exception("job worker error: %s", job_id)

    def _notify(self, manifest: JobManifest) -> None:
        self.store.save(manifest)
        if self.on_update:
            self.on_update(manifest)

    def _run_job(self, job_id: str) -> None:
        manifest = self.store.load(job_id)
        paths = self.store.paths(job_id)
        paths.ensure()
        logger = JsonLogger(paths.log)
        controls = self._controls_for(job_id)
        manifest.set_status("running")
        self._notify(manifest)
        logger.event("job_started", job_id=job_id, segment_count=len(manifest.segments))
        for segment in manifest.segments:
            if segment.status == "succeeded":
                if segment.audio_path and (paths.root / segment.audio_path).is_file():
                    continue
                # A manifest may survive a machine interruption after the
                # state write but before the audio rename.  Treat that one
                # segment as pending instead of producing a broken final file.
                segment.status = "pending"
                segment.audio_path = None
            if controls["cancel"].is_set():
                manifest.set_status("cancelled")
                self._notify(manifest)
                logger.event("job_cancelled", job_id=job_id)
                return
            if controls["pause"].is_set():
                manifest.set_status("paused")
                self._notify(manifest)
                logger.event("job_paused", job_id=job_id)
                return
            output = paths.segments / f"{segment.index:05d}-{segment.id}.wav"
            try:
                manifest.set_segment_status(segment.id, "running")
                self._notify(manifest)
                logger.event("segment_started", job_id=job_id, segment_id=segment.id, index=segment.index, attempt=segment.attempts)
                value = self.generator(
                    segment.text,
                    segment_index=segment.index,
                    seed=manifest.seed,
                    params=manifest.generation_params,
                )
                _materialize_wav(output, value)
                manifest.set_segment_status(segment.id, "succeeded", audio_path=str(output.relative_to(paths.root)))
                self._notify(manifest)
                logger.event("segment_succeeded", job_id=job_id, segment_id=segment.id, index=segment.index)
                # A pause/cancel request can arrive while the GPU is inside a
                # generation call.  Persist the completed segment, then stop
                # before starting another one or assembling the final files.
                if controls["cancel"].is_set():
                    manifest.set_status("cancelled")
                    self._notify(manifest)
                    logger.event("job_cancelled", job_id=job_id)
                    return
                if controls["pause"].is_set():
                    manifest.set_status("paused")
                    self._notify(manifest)
                    logger.event("job_paused", job_id=job_id)
                    return
            except Exception as exc:
                output.unlink(missing_ok=True)
                manifest.set_segment_status(segment.id, "failed", error=f"{type(exc).__name__}: {exc}")
                self._notify(manifest)
                logger.event("segment_failed", job_id=job_id, segment_id=segment.id, index=segment.index, error=str(exc))
        failed = manifest.failed()
        if failed:
            manifest.set_status("failed", error=f"{len(failed)} segment(s) failed")
            self._notify(manifest)
            logger.event("job_failed", job_id=job_id, failed_count=len(failed))
            return
        try:
            segment_paths = [paths.root / segment.audio_path for segment in manifest.segments if segment.audio_path]
            # Never expose a stale final file while rebuilding after retry or
            # resume.  Segment files remain available for playback/recovery.
            for key in ("wav", "mp3"):
                value = manifest.outputs.get(key)
                if not value:
                    continue
                candidate = Path(value)
                if not candidate.is_absolute():
                    candidate = paths.root / candidate
                try:
                    candidate.resolve().relative_to(paths.root.resolve())
                except ValueError:
                    continue
                candidate.unlink(missing_ok=True)
            manifest.outputs = {}
            self._notify(manifest)
            manifest.outputs = merge_job_audio(segment_paths, paths.output, pause_ms=manifest.pause_ms, ffmpeg=self.ffmpeg, stem="audio")
            manifest.set_status("completed")
            self._notify(manifest)
            logger.event("job_completed", job_id=job_id, outputs=manifest.outputs)
        except Exception as exc:
            manifest.set_status("failed", error=f"merge: {type(exc).__name__}: {exc}")
            self._notify(manifest)
            logger.event("merge_failed", job_id=job_id, error=str(exc))


class GenerationQueue:
    """Compatibility facade for the early WebUI queue constructor.

    The durable implementation is :class:`SequentialJobQueue`.  This facade
    accepts either a :class:`JobManifest` or a mapping loaded by the first UI
    draft and exposes no-argument ``start/pause/resume/cancel/retry_failed``
    methods, making it straightforward to migrate the HTTP layer without
    duplicating worker logic.
    """

    def __init__(
        self,
        manifest: JobManifest | Mapping[str, Any],
        *,
        manifest_path: str | Path | None = None,
        output_dir: str | Path | None = None,
        generate_fn: Callable[..., Any] | None = None,
        ffmpeg: str = "ffmpeg",
    ) -> None:
        if isinstance(manifest, JobManifest):
            self.manifest = manifest
        else:
            payload = dict(manifest)
            settings = dict(payload.get("settings") or {})
            payload.setdefault("max_chars", settings.get("max_chars", 140))
            payload.setdefault("pause_ms", settings.get("pause_ms", 300))
            payload.setdefault("seed", settings.get("seed", 42))
            payload.setdefault("generation_params", settings)
            payload.setdefault("normalized_text_sha256", "")
            payload.setdefault("original_text_sha256", payload.get("original_text_hash", ""))
            payload["segments"] = payload.get("segments") or payload.get("chunks") or []
            # Manifest schemas created by the UI may not include text hashes;
            # a fresh canonical object retains their exact segment stream.
            self.manifest = JobManifest.from_dict(payload)
        if manifest_path is not None:
            path = Path(manifest_path)
            self.store = JobStore(path.parent.parent)
            self.manifest_path = path
        else:
            root = Path(output_dir or "tasks")
            self.store = JobStore(root.parent if root.name == self.manifest.job_id else root)
            self.manifest_path = self.store.paths(self.manifest.job_id).manifest
        self.output_dir = Path(output_dir) if output_dir else self.store.paths(self.manifest.job_id).segments
        self._legacy_generator = generate_fn
        self._queue = SequentialJobQueue(self.store, self._generate, ffmpeg=ffmpeg)
        self.store.create(self.manifest)

    def _generate(self, text: str, *, segment_index: int, seed: int | None, params: Mapping[str, Any]) -> Any:
        if self._legacy_generator is None:
            generator = IndexTTSGenerator()
            return generator(text, segment_index=segment_index, seed=seed, params=params)
        segment = self.manifest.segments[segment_index]
        temporary = self.output_dir / f".legacy-{segment_index:05d}.wav"
        result = self._legacy_generator(segment, temporary, seed, params)
        if result is None:
            return temporary
        return result

    @property
    def job_id(self) -> str:
        return self.manifest.job_id

    def start(self) -> None:
        self._queue.submit(self.manifest)

    def resume(self) -> None:
        self._queue.resume(self.job_id)

    def pause(self) -> None:
        self._queue.pause(self.job_id)

    def cancel(self) -> None:
        self._queue.cancel(self.job_id)

    def retry_failed(self) -> None:
        self._queue.retry_failed(self.job_id)

    def stop(self) -> None:
        self._queue.stop()


__all__ = ["GenerationQueue", "JobPaths", "JobStore", "JsonLogger", "SegmentGenerator", "SequentialJobQueue"]
