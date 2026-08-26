"""Generate SRT subtitles aligned to the assembled segment audio."""

from __future__ import annotations

import os
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .audio import inspect_wav
from .text import split_text

SUBTITLE_FORMAT_VERSION = 2


@dataclass(frozen=True)
class SubtitleCue:
    start_ms: int
    end_ms: int
    text: str


def _timestamp(milliseconds: int) -> str:
    value = max(0, int(milliseconds))
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _display_text(value: str) -> str:
    text = " ".join(value.split())
    while text and unicodedata.category(text[-1]).startswith("P"):
        text = text[:-1].rstrip()
    return text


def build_subtitle_cues(
    segments: Sequence[tuple[str, str | Path]],
    *,
    pause_ms: int = 300,
    max_chars: int = 28,
) -> list[SubtitleCue]:
    """Build readable cues using real WAV durations and proportional timing.

    IndexTTS does not expose word timestamps. Each generated segment is
    therefore timed exactly as a whole, while shorter subtitle cues inside it
    receive a proportional share of that segment's measured duration.
    """

    if pause_ms < 0:
        raise ValueError("pause_ms cannot be negative")
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    cues: list[SubtitleCue] = []
    cursor_seconds = 0.0
    for segment_index, (text, audio_path) in enumerate(segments):
        info = inspect_wav(audio_path)
        duration_seconds = max(0.001, info.duration_seconds)
        pieces = split_text(text, max_chars=max_chars)
        weighted_pieces: list[tuple[str, int]] = []
        pending_weight = 0
        for piece in pieces:
            weight = max(1, sum(not char.isspace() for char in piece.text))
            display_text = _display_text(piece.text)
            if display_text:
                weighted_pieces.append((display_text, weight + pending_weight))
                pending_weight = 0
            elif weighted_pieces:
                previous_text, previous_weight = weighted_pieces[-1]
                weighted_pieces[-1] = (previous_text, previous_weight + weight)
            else:
                pending_weight += weight
        if pending_weight and weighted_pieces:
            previous_text, previous_weight = weighted_pieces[-1]
            weighted_pieces[-1] = (previous_text, previous_weight + pending_weight)
        total_weight = sum(weight for _, weight in weighted_pieces)
        elapsed_weight = 0
        for display_text, weight in weighted_pieces:
            start_ms = round((cursor_seconds + duration_seconds * elapsed_weight / total_weight) * 1_000)
            elapsed_weight += weight
            end_ms = round((cursor_seconds + duration_seconds * elapsed_weight / total_weight) * 1_000)
            if end_ms <= start_ms:
                end_ms = start_ms + 1
            cues.append(SubtitleCue(start_ms=start_ms, end_ms=end_ms, text=display_text))
        cursor_seconds += duration_seconds
        if segment_index < len(segments) - 1:
            cursor_seconds += pause_ms / 1_000
    return cues


def render_srt(cues: Sequence[SubtitleCue]) -> str:
    blocks = [
        f"{index}\r\n{_timestamp(cue.start_ms)} --> {_timestamp(cue.end_ms)}\r\n{cue.text}"
        for index, cue in enumerate(cues, start=1)
    ]
    return "\r\n\r\n".join(blocks) + ("\r\n" if blocks else "")


def write_srt_file(
    segments: Sequence[tuple[str, str | Path]],
    output_path: str | Path,
    *,
    pause_ms: int = 300,
    max_chars: int = 28,
) -> tuple[Path, int]:
    """Atomically write a UTF-8-BOM SRT for broad editor compatibility."""

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    cues = build_subtitle_cues(segments, pause_ms=pause_ms, max_chars=max_chars)
    payload = render_srt(cues)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return target, len(cues)


__all__ = ["SUBTITLE_FORMAT_VERSION", "SubtitleCue", "build_subtitle_cues", "render_srt", "write_srt_file"]
