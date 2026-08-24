"""Audio helpers for durable segment output and final assembly.

The merger uses the standard-library ``wave`` module for WAV files and an
explicit ffmpeg subprocess for MP3 conversion.  No browser-side audio joining
is involved, so an interrupted browser cannot corrupt a completed output.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class AudioInfo:
    sample_rate: int
    channels: int
    sample_width: int
    frames: int

    @property
    def duration_seconds(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0


def inspect_wav(path: str | Path) -> AudioInfo:
    with wave.open(str(path), "rb") as reader:
        return AudioInfo(reader.getframerate(), reader.getnchannels(), reader.getsampwidth(), reader.getnframes())


def _silence(info: AudioInfo, duration_ms: int) -> bytes:
    if duration_ms <= 0:
        return b""
    frame_count = round(info.sample_rate * duration_ms / 1000)
    return b"\0" * frame_count * info.channels * info.sample_width


def merge_wav_files(
    paths: Sequence[str | Path],
    output_path: str | Path,
    *,
    pause_ms: int = 0,
) -> AudioInfo:
    """Concatenate compatible WAV files and atomically write ``output_path``.

    Segment order is the caller's sequence order.  Format mismatches are
    rejected instead of silently resampling, which protects output quality and
    makes a bad segment immediately visible in the job error state.
    """

    if not paths:
        raise ValueError("at least one WAV segment is required")
    if pause_ms < 0:
        raise ValueError("pause_ms cannot be negative")
    source_paths = [Path(path) for path in paths]
    if any(not path.is_file() for path in source_paths):
        missing = next(path for path in source_paths if not path.is_file())
        raise FileNotFoundError(missing)
    with wave.open(str(source_paths[0]), "rb") as first:
        params = first.getparams()
        info = AudioInfo(first.getframerate(), first.getnchannels(), first.getsampwidth(), first.getnframes())
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    total_frames = 0
    try:
        with wave.open(str(temporary), "wb") as writer:
            writer.setnchannels(params.nchannels)
            writer.setsampwidth(params.sampwidth)
            writer.setframerate(params.framerate)
            for index, path in enumerate(source_paths):
                with wave.open(str(path), "rb") as reader:
                    current = reader.getparams()
                    comparable = (current.nchannels, current.sampwidth, current.framerate, current.comptype)
                    expected = (params.nchannels, params.sampwidth, params.framerate, params.comptype)
                    if comparable != expected:
                        raise ValueError(f"WAV format mismatch at segment {index}: {path}")
                    frames = reader.readframes(reader.getnframes())
                    writer.writeframes(frames)
                    total_frames += reader.getnframes()
                if index < len(source_paths) - 1 and pause_ms:
                    silence = _silence(info, pause_ms)
                    writer.writeframes(silence)
                    total_frames += len(silence) // (params.nchannels * params.sampwidth)
            # ``wave`` updates the RIFF frame count as ``writeframes`` is
            # called.  Calling ``setnframes`` after writing would attempt to
            # mutate parameters on a stream that has already started.
        temporary.replace(output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return AudioInfo(info.sample_rate, info.channels, info.sample_width, total_frames)


def convert_wav_to_mp3(
    wav_path: str | Path,
    mp3_path: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    bitrate: str = "192k",
) -> Path:
    """Convert WAV to MP3 with ffmpeg, atomically and without shell parsing."""

    source = Path(wav_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    executable = shutil.which(ffmpeg) or (ffmpeg if Path(ffmpeg).is_file() else None)
    if executable is None and ffmpeg == "ffmpeg":
        # The shared 12-server environment keeps its private FFmpeg beside
        # the Conda prefix and does not add it to the login shell PATH.
        for candidate in (
            Path("/data1/ximizhou/envs/conda/indextts/bin/ffmpeg"),
        ):
            if candidate.is_file():
                executable = str(candidate)
                break
    if not executable:
        raise FileNotFoundError(f"ffmpeg executable not found: {ffmpeg}")
    target = Path(mp3_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Keep an ``.mp3`` suffix so ffmpeg can select its muxer even though the
    # temporary file is hidden until the conversion completes.
    temporary = target.with_name(f".{target.stem}.tmp.mp3")
    command = [executable, "-y", "-loglevel", "error", "-i", str(source), "-codec:a", "libmp3lame", "-b:a", bitrate, str(temporary)]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "ffmpeg failed").strip()
            raise RuntimeError(detail)
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def merge_job_audio(
    segment_paths: Iterable[str | Path],
    output_dir: str | Path,
    *,
    pause_ms: int = 300,
    ffmpeg: str = "ffmpeg",
    stem: str = "audio",
) -> dict[str, object]:
    """Produce both final WAV and MP3 and return inspectable output metadata."""

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    wav_path = output_root / f"{stem}.wav"
    mp3_path = output_root / f"{stem}.mp3"
    info = merge_wav_files(list(segment_paths), wav_path, pause_ms=pause_ms)
    convert_wav_to_mp3(wav_path, mp3_path, ffmpeg=ffmpeg)
    return {
        "wav": str(wav_path),
        "mp3": str(mp3_path),
        "sample_rate": info.sample_rate,
        "channels": info.channels,
        "duration_seconds": info.duration_seconds,
        "wav_bytes": wav_path.stat().st_size,
        "mp3_bytes": mp3_path.stat().st_size,
    }


__all__ = ["AudioInfo", "convert_wav_to_mp3", "inspect_wav", "merge_job_audio", "merge_wav_files"]

# Compatibility names for the first WebUI draft.  New code should use the
# explicit names above; these aliases keep early local scripts readable while
# the UI migrates to the durable queue contract.
merge_wav = merge_wav_files
transcode_mp3 = convert_wav_to_mp3
