from __future__ import annotations

import io
import wave
from pathlib import Path

from fastapi.testclient import TestClient

from app.web import create_app
from core.tts import FakeGenerator


def _reference_files(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name in (
        "voice_01.wav", "voice_02.wav", "voice_03.wav", "voice_04.wav",
        "voice_05.wav", "voice_06.wav", "voice_07.wav", "voice_08.wav",
        "voice_09.wav", "voice_11.wav", "voice_12.wav",
    ):
        path = root / name
        with wave.open(str(path), "wb") as writer:
            writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(22_050); writer.writeframes(b"\0\0" * 200)
    return root


def make_client(tmp_path: Path) -> TestClient:
    references = _reference_files(tmp_path / "examples")
    return TestClient(create_app(tmp_path / "tasks", generator=FakeGenerator(), ffmpeg="ffmpeg", voices_dir=tmp_path / "voices"))


def test_health_and_index(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INDEXTTS_REFERENCE_DIR", str(_reference_files(tmp_path / "examples")))
    client = make_client(tmp_path)
    assert client.get("/healthz").json() == {"status": "ok"}
    response = client.get("/")
    assert response.status_code == 200 and "IndexTTS" in response.text and "长音频工作台" in response.text


def test_preview_keeps_normalized_order(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INDEXTTS_REFERENCE_DIR", str(_reference_files(tmp_path / "examples")))
    client = make_client(tmp_path)
    response = client.post("/api/preview", json={"text": "# 标题\n\n第一句。第二句！", "max_chars": 80})
    assert response.status_code == 200
    payload = response.json()
    assert payload["integrity"] is True and "标题" in payload["normalized_text"]


def test_create_and_reload_job(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INDEXTTS_REFERENCE_DIR", str(_reference_files(tmp_path / "examples")))
    client = make_client(tmp_path)
    response = client.post("/api/jobs", json={"text": "一" * 181, "voice_id": "index-example-01"})
    assert response.status_code == 200
    job = response.json()
    assert job["summary"]["segment_count"] == 2
    assert client.get(f"/api/jobs/{job['job_id']}").json()["original_text"] == "一" * 181
    assert client.get("/api/jobs").json()[0]["job_id"] == job["job_id"]


def test_validation_and_text_upload(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INDEXTTS_REFERENCE_DIR", str(_reference_files(tmp_path / "examples")))
    client = make_client(tmp_path)
    assert client.post("/api/jobs", json={}).status_code == 422
    assert client.post("/api/jobs", json={"text": "测试", "lang": "fr"}).status_code == 422
    assert client.post("/api/jobs", json={"text": "测试", "max_chars": 79}).status_code == 422
    response = client.post("/api/jobs/upload", files={"file": ("稿件.txt", "你好。".encode(), "text/plain")}, data={"max_chars": "80"})
    assert response.status_code == 200 and response.json()["normalized_text"] == "你好。"
    bad = client.post("/api/jobs/upload", files={"file": ("sound.wav", b"RIFF", "audio/wav")})
    assert bad.status_code == 400


def test_voice_preview_upload_select_and_delete(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INDEXTTS_REFERENCE_DIR", str(_reference_files(tmp_path / "examples")))
    client = make_client(tmp_path)
    library = client.get("/api/voices").json()
    assert len(library["presets"]) == 11 and library["saved"] == []
    preview = client.post("/api/voices/preview", json={"text": "这是一段试听。", "voice_id": "index-example-01"})
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["voice"]["id"] == "index-example-01"
    audio = client.get(payload["audio_url"])
    assert audio.status_code == 200 and audio.content[:4] == b"RIFF"
    uploaded = client.post("/api/voices/upload", files={"file": ("my.wav", b"RIFFfake", "audio/wav")}, data={"name": "我的旁白", "description": "测试"})
    assert uploaded.status_code == 201
    voice = uploaded.json()
    assert voice["kind"] == "saved" and voice["id"].startswith("voice-")
    job = client.post("/api/jobs", json={"text": "使用已保存音色。", "voice_id": voice["id"]})
    assert job.status_code == 200 and job.json()["settings"]["voice_id"] == voice["id"]
    assert client.delete(f"/api/voices/{voice['id']}").status_code == 204
    assert client.delete("/api/voices/index-example-01").status_code == 409


def test_encoded_backslash_cannot_escape_job_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INDEXTTS_REFERENCE_DIR", str(_reference_files(tmp_path / "examples")))
    client = make_client(tmp_path)
    response = client.get("/api/jobs/..%5Coutside")
    assert response.status_code == 404 and response.json()["detail"] == "invalid job id"
