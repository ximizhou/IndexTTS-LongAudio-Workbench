"""Durable job and segment state used by the worker and WebUI."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .text import TextSegment, join_segments, split_text, validate_integrity


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class SegmentRecord:
    id: str
    index: int
    text: str
    char_count: int
    status: str = "pending"
    attempts: int = 0
    audio_path: str | None = None
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None

    @classmethod
    def from_text_segment(cls, segment: TextSegment) -> "SegmentRecord":
        return cls(segment.id, segment.index, segment.text, segment.char_count)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SegmentRecord":
        text = str(data.get("text", ""))
        status = str(data.get("status", "pending"))
        # Early WebUI manifests used ``success``; normalize that spelling on
        # load while emitting the canonical ``succeeded`` status thereafter.
        if status == "success":
            status = "succeeded"
        return cls(
            id=str(data["id"]),
            index=int(data["index"]),
            text=text,
            char_count=int(data.get("char_count", len(text))),
            status=status,
            attempts=int(data.get("attempts", 0)),
            audio_path=str(data["audio_path"]) if data.get("audio_path") else None,
            error=str(data["error"]) if data.get("error") else None,
            started_at=str(data["started_at"]) if data.get("started_at") else None,
            completed_at=str(data["completed_at"]) if data.get("completed_at") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "index": self.index,
            "text": self.text,
            "char_count": self.char_count,
            "status": self.status,
            "attempts": self.attempts,
            "audio_path": self.audio_path,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


@dataclass
class JobManifest:
    """A JSON-persisted job manifest.

    User text lives here only in the runtime data directory and is never
    intended to be committed.  Audio paths are relative to the job directory.
    """

    job_id: str
    normalized_text: str
    max_chars: int
    pause_ms: int = 300
    seed: int | None = 42
    generation_params: dict[str, Any] = field(default_factory=dict)
    segments: list[SegmentRecord] = field(default_factory=list)
    status: str = "created"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    outputs: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    source_name: str | None = None
    original_text: str | None = None
    original_text_sha256: str = ""
    normalized_text_sha256: str = ""
    schema_version: int = 1
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    @classmethod
    def create(
        cls,
        text: str,
        *,
        max_chars: int = 140,
        pause_ms: int = 300,
        seed: int | None = 42,
        generation_params: Mapping[str, Any] | None = None,
        job_id: str | None = None,
        source_name: str | None = None,
        original_text: str | None = None,
    ) -> "JobManifest":
        segments = split_text(text, max_chars=max_chars)
        now = utc_now()
        return cls(
            job_id=job_id or uuid.uuid4().hex,
            normalized_text=text,
            max_chars=max_chars,
            pause_ms=pause_ms,
            seed=seed,
            generation_params=dict(generation_params or {}),
            segments=[SegmentRecord.from_text_segment(item) for item in segments],
            created_at=now,
            updated_at=now,
            source_name=source_name,
            original_text=original_text,
            original_text_sha256=sha256_text(original_text if original_text is not None else text),
            normalized_text_sha256=sha256_text(text),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "JobManifest":
        text = str(data.get("normalized_text", ""))
        manifest = cls(
            job_id=str(data["job_id"]),
            normalized_text=text,
            max_chars=int(data.get("max_chars", 140)),
            pause_ms=int(data.get("pause_ms", 300)),
            seed=int(data["seed"]) if data.get("seed") is not None else None,
            generation_params=dict(data.get("generation_params") or {}),
            segments=[SegmentRecord.from_dict(item) for item in data.get("segments", [])],
            status=str(data.get("status", "created")),
            created_at=str(data.get("created_at", utc_now())),
            updated_at=str(data.get("updated_at", utc_now())),
            outputs={str(key): value for key, value in (data.get("outputs") or {}).items() if value is not None},
            error=str(data["error"]) if data.get("error") else None,
            source_name=str(data["source_name"]) if data.get("source_name") else None,
            original_text=str(data["original_text"]) if data.get("original_text") is not None else None,
            original_text_sha256=str(data.get("original_text_sha256", "")),
            normalized_text_sha256=str(data.get("normalized_text_sha256", "")),
            schema_version=int(data.get("schema_version", 1)),
        )
        manifest.validate()
        return manifest

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema_version": self.schema_version,
                "job_id": self.job_id,
                "normalized_text": self.normalized_text,
                "max_chars": self.max_chars,
                "pause_ms": self.pause_ms,
                "seed": self.seed,
                "generation_params": self.generation_params,
                "segments": [segment.to_dict() for segment in self.segments],
                "status": self.status,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "outputs": self.outputs,
                "error": self.error,
                "source_name": self.source_name,
                "original_text": self.original_text,
                "original_text_sha256": self.original_text_sha256,
                "normalized_text_sha256": self.normalized_text_sha256,
            }

    def validate(self) -> None:
        """Validate source hashes and segment ordering before a resume."""

        with self._lock:
            if self.max_chars < 1:
                raise ValueError("max_chars must be positive")
            if self.pause_ms < 0:
                raise ValueError("pause_ms cannot be negative")
            if self.normalized_text_sha256 and sha256_text(self.normalized_text) != self.normalized_text_sha256:
                raise ValueError("normalized text hash does not match manifest")
            if not validate_integrity(self.normalized_text, self.segments):
                raise ValueError("segments do not preserve normalized text order/content")

    def save(self, path: str | Path) -> Path:
        """Atomically save JSON so a process interruption cannot leave a half file."""

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self.updated_at = utc_now()
            payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Windows may briefly hold the target open while the status API is
            # reading it.  Retry the atomic rename for that narrow race; on
            # Linux the first attempt remains the normal path.
            for attempt in range(20):
                try:
                    os.replace(temp_name, target)
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.01)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        return target

    @classmethod
    def load(cls, path: str | Path) -> "JobManifest":
        with Path(path).open("r", encoding="utf-8") as stream:
            return cls.from_dict(json.load(stream))

    def segment(self, segment_id: str) -> SegmentRecord:
        for segment in self.segments:
            if segment.id == segment_id:
                return segment
        raise KeyError(f"unknown segment: {segment_id}")

    def pending(self) -> list[SegmentRecord]:
        return [segment for segment in self.segments if segment.status in {"pending", "cancelled"}]

    def failed(self) -> list[SegmentRecord]:
        return [segment for segment in self.segments if segment.status == "failed"]

    def completed(self) -> list[SegmentRecord]:
        return [segment for segment in self.segments if segment.status == "succeeded"]

    def set_status(self, status: str, error: str | None = None) -> None:
        if status not in {"created", "queued", "running", "paused", "cancelled", "completed", "failed"}:
            raise ValueError(f"invalid job status: {status}")
        with self._lock:
            self.status = status
            self.error = error
            self.updated_at = utc_now()

    def set_segment_status(
        self,
        segment_id: str,
        status: str,
        *,
        audio_path: str | None = None,
        error: str | None = None,
    ) -> SegmentRecord:
        if status not in {"pending", "running", "succeeded", "failed", "cancelled"}:
            raise ValueError(f"invalid segment status: {status}")
        with self._lock:
            segment = self.segment(segment_id)
            segment.status = status
            segment.error = error
            if audio_path is not None:
                segment.audio_path = audio_path
            if status == "running":
                segment.started_at = utc_now()
                segment.attempts += 1
            elif status == "succeeded":
                segment.completed_at = utc_now()
            self.updated_at = utc_now()
            return segment


def manifest_from_text(text: str, **kwargs: Any) -> JobManifest:
    """Convenience wrapper used by API code."""

    return JobManifest.create(text, **kwargs)


__all__ = ["JobManifest", "SegmentRecord", "manifest_from_text", "sha256_text", "utc_now"]
