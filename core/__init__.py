"""Core text, manifest, queue, and audio primitives for the workbench."""

from .audio import AudioInfo, convert_wav_to_mp3, inspect_wav, merge_job_audio, merge_wav_files
from .manifest import JobManifest, SegmentRecord, manifest_from_text
from .queue import GenerationQueue, JobPaths, JobStore, JsonLogger, SequentialJobQueue
from .text import TextSegment, join_segments, normalize_text, read_text_file, split_text, validate_integrity
from .tts import FailOnceGenerator, FakeGenerator, IndexTTSGenerator
from .voices import CURATED_VOICES, VoiceStore

__all__ = [
    "AudioInfo",
    "IndexTTSGenerator",
    "CURATED_VOICES",
    "FakeGenerator",
    "FailOnceGenerator",
    "GenerationQueue",
    "JobManifest",
    "JobPaths",
    "JobStore",
    "JsonLogger",
    "SegmentRecord",
    "SequentialJobQueue",
    "TextSegment",
    "VoiceStore",
    "convert_wav_to_mp3",
    "inspect_wav",
    "join_segments",
    "manifest_from_text",
    "merge_job_audio",
    "merge_wav_files",
    "normalize_text",
    "read_text_file",
    "split_text",
    "validate_integrity",
]
