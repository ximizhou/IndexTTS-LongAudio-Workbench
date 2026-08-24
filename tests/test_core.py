from __future__ import annotations

import hashlib
import json
import threading
import wave
from pathlib import Path

import pytest

from core.audio import inspect_wav, merge_wav_files
from core.manifest import JobManifest
from core.queue import JobStore, SequentialJobQueue
from core.text import join_segments, normalize_text, read_text_file, split_text, validate_integrity
from core.tts import FakeGenerator, _wav_bytes
from core.voices import VoiceStore, validate_voice_id
from scripts.download_hf_range import download


@pytest.mark.parametrize(("source", "max_chars"), [("第一句。第二句！第三句？", 5), ("没有标点的中文" * 12, 17), ("# 标题\n\n正文。", 8), ("超长句" * 500, 80)])
def test_splitter_preserves_normalized_text(source: str, max_chars: int) -> None:
    normalized = normalize_text(source)
    segments = split_text(normalized, max_chars=max_chars)
    assert segments and all(0 < item.char_count <= max_chars for item in segments)
    assert [item.index for item in segments] == list(range(len(segments)))
    assert join_segments(segments) == normalized
    assert validate_integrity(normalized, segments)


def test_normalize_markdown_and_terms() -> None:
    value = normalize_text("# 标题\n\n> **IndexTTS** [项目](https://example.com)。\n\n- LLM", terms={"LLM": "大语言模型"})
    assert "标题" in value and "IndexTTS" in value and "大语言模型" in value
    assert "https://example.com" not in value and "**" not in value


def test_read_text_file_supports_gb18030(tmp_path: Path) -> None:
    path = tmp_path / "input.txt"
    expected = "中文编码与数字 123"
    path.write_bytes(expected.encode("gb18030"))
    assert read_text_file(path) == expected


def test_manifest_round_trip_and_integrity(tmp_path: Path) -> None:
    manifest = JobManifest.create(normalize_text("第一段。第二段。"), max_chars=4, seed=123)
    path = tmp_path / "job" / "manifest.json"
    manifest.save(path)
    loaded = JobManifest.load(path)
    assert loaded.job_id == manifest.job_id and loaded.seed == 123
    assert validate_integrity(loaded.normalized_text, loaded.segments)
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1


def _make_wav(path: Path, frames: int, sample_rate: int = 8_000) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(b"\0\0" * frames)


def test_wav_merge_preserves_order_and_pause(tmp_path: Path) -> None:
    first, second, output = tmp_path / "first.wav", tmp_path / "second.wav", tmp_path / "merged.wav"
    _make_wav(first, 8); _make_wav(second, 12)
    info = merge_wav_files([first, second], output, pause_ms=100)
    assert output.is_file() and info.sample_rate == 8_000 and info.frames == 8 + 12 + 800
    assert inspect_wav(output).frames == info.frames


def test_wav_bytes_accepts_indextts_tuple() -> None:
    payload = _wav_bytes((16_000, [0.0, 0.25, -0.25]))
    with wave.open(__import__("io").BytesIO(payload), "rb") as reader:
        assert reader.getframerate() == 16_000 and reader.getnframes() == 3


def test_ranged_downloader_resumes_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"0123456789abcdef"
    ranges: list[tuple[int, int]] = []

    class Response:
        status_code = 206

        def __init__(self, start: int, end: int) -> None:
            self.start = start
            self.end = end

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def iter_content(self, chunk_size: int) -> list[bytes]:
            return [payload[self.start : self.end + 1]]

    class Session:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

        def get(self, _url: str, *, headers: dict[str, str], **_: object) -> Response:
            start, end = (int(value) for value in headers["Range"].removeprefix("bytes=").split("-"))
            ranges.append((start, end))
            return Response(start, end)

    monkeypatch.setattr("scripts.download_hf_range.hf_hub_url", lambda **_: "https://example.invalid/model")
    monkeypatch.setattr("scripts.download_hf_range.requests.Session", Session)
    target = tmp_path / "model.bin"
    part = target.with_name("model.bin.part")
    part.write_bytes(payload[:6])
    download("repo", "model.bin", target, len(payload), hashlib.sha256(payload).hexdigest(), chunk_size=4, retries=1)
    assert target.read_bytes() == payload
    assert ranges == [(6, 9), (10, 13), (14, 15)]


def test_queue_failure_retry_and_resume(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs")
    generator = FakeGenerator(fail_indices={1})
    manifest = JobManifest.create("第一段。第二段。第三段。", max_chars=5, pause_ms=0)
    store.create(manifest)
    queue = SequentialJobQueue(store, generator, ffmpeg="ffmpeg")
    queue.submit(manifest)
    failed = queue.wait(manifest.job_id, timeout=30)
    assert failed.status == "failed" and len(failed.failed()) == 1
    queue.retry_failed(manifest.job_id)
    completed = queue.wait(manifest.job_id, timeout=30)
    queue.stop()
    assert completed.status == "completed" and all(item.status == "succeeded" for item in completed.segments)
    assert Path(completed.outputs["wav"]).is_file() and Path(completed.outputs["mp3"]).is_file()
    assert generator.calls.count(0) == 1 and generator.calls.count(1) == 2


class _BlockingGenerator(FakeGenerator):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, text: str, *, segment_index: int, seed: int | None, params: object) -> bytes:
        self.started.set()
        if not self.release.wait(10):
            raise TimeoutError("test generator release timed out")
        return super().__call__(text, segment_index=segment_index, seed=seed, params={})


def test_queue_pause_cancel_and_resume(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs")
    generator = _BlockingGenerator()
    manifest = JobManifest.create("第一段。第二段。", max_chars=5, pause_ms=0)
    store.create(manifest)
    queue = SequentialJobQueue(store, generator, ffmpeg="ffmpeg")
    queue.submit(manifest)
    assert generator.started.wait(10)
    queue.pause(manifest.job_id); generator.release.set()
    paused = queue.wait(manifest.job_id, timeout=30)
    assert paused.status == "paused" and paused.segments[0].status == "succeeded"
    generator.started.clear(); generator.release.clear()
    queue.resume(manifest.job_id); assert generator.started.wait(10)
    queue.cancel(manifest.job_id); generator.release.set()
    cancelled = queue.wait(manifest.job_id, timeout=30)
    generator.started.clear(); queue.resume(manifest.job_id)
    resumed = queue.wait(manifest.job_id, timeout=30); queue.stop()
    assert cancelled.status == "cancelled" and resumed.status == "completed"


def test_voice_store_uses_reference_audio_files(tmp_path: Path) -> None:
    examples = tmp_path / "examples"
    for name in (
        "voice_01.wav", "voice_02.wav", "voice_03.wav", "voice_04.wav",
        "voice_05.wav", "voice_06.wav", "voice_07.wav", "voice_08.wav",
        "voice_09.wav", "voice_11.wav", "voice_12.wav",
    ):
        _make_wav(examples / name, 100, 22_050) if examples.exists() else (examples.mkdir(parents=True), _make_wav(examples / name, 100, 22_050))
    store = VoiceStore(tmp_path / "voices", reference_dir=examples)
    library = store.list()
    assert len(library["presets"]) == 11 and all(item["has_reference"] for item in library["presets"])
    assert sum(item["gender"] == "偏女声" for item in library["presets"]) == 7
    assert sum(item["gender"] == "偏男声" for item in library["presets"]) == 4
    assert validate_voice_id("index-example-01") == "index-example-01"
    with pytest.raises(ValueError): validate_voice_id("../outside")
    audio = FakeGenerator()("试听", segment_index=0, seed=1, params={})
    saved = store.record_preview(audio, reference_audio=str(examples / "voice_01.wav"), voice=store.get("index-example-01"), settings={})
    assert store.preview_audio(saved["preview_id"]).is_file()
    user_voice = store.save_from_preview(name="我的旁白", preview_id=saved["preview_id"], description="测试")
    assert user_voice["kind"] == "saved" and user_voice["id"].startswith("voice-")
    assert len(store.list()["saved"]) == 1
    store.delete(user_voice["id"])
    assert store.list()["saved"] == []
    with pytest.raises(PermissionError): store.delete("index-example-01")
