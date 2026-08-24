"""Repeatable 12-server acceptance checks for IndexTTS long-form generation."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import threading
import time
import urllib.request
import wave
from pathlib import Path

from core.manifest import JobManifest
from core.queue import JobStore, SequentialJobQueue
from core.text import normalize_text, validate_integrity
from core.tts import FakeGenerator


def request_json(url: str, *, method: str = "GET", payload: dict | None = None) -> dict | list:
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def build_long_text(length: int = 7000) -> str:
    sentence = "这是一个用于长文本语音验收的句子，必须保持原文顺序、标点和每一个汉字。"
    return (sentence * ((length // len(sentence)) + 1))[:length]


def real_webui(base_url: str, *, max_chars: int, timeout: int, existing_job: str | None = None) -> None:
    text = build_long_text()
    if existing_job:
        job_id = existing_job
    else:
        preview = request_json(base_url + "/api/preview", method="POST", payload={"text": text, "max_chars": max_chars})
        assert preview["integrity"] is True
        assert len("".join(item["text"] for item in preview["segments"])) == len(preview["normalized_text"])
        job = request_json(base_url + "/api/jobs", method="POST", payload={"text": text, "max_chars": max_chars, "pause_ms": 100, "seed": 42})
        job_id = job["job_id"]
        request_json(base_url + f"/api/jobs/{job_id}/start", method="POST")
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        state = request_json(base_url + f"/api/jobs/{job_id}")
        summary = state["summary"]
        marker = (summary["status"], summary["completed_count"], summary["failed_count"])
        if marker != last:
            print("REAL_PROGRESS", marker, flush=True)
            last = marker
        if summary["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(2)
    else:
        raise TimeoutError(f"7000-character job did not finish within {timeout}s")
    assert state["status"] == "completed", state
    assert state["integrity"] is True
    assert all(item["status"] in {"succeeded", "success"} for item in state["segments"])
    wav_path = Path(state["outputs"]["wav"])
    mp3_path = Path(state["outputs"]["mp3"])
    assert wav_path.is_file() and wav_path.stat().st_size > 44
    assert mp3_path.is_file() and mp3_path.stat().st_size > 0
    with wave.open(str(wav_path), "rb") as reader:
        duration = reader.getnframes() / reader.getframerate()
    print(
        "REAL_RESULT",
        json.dumps(
            {
                "job_id": job_id,
                "status": state["status"],
                "segments": len(state["segments"]),
                "success": sum(item["status"] in {"succeeded", "success"} for item in state["segments"]),
                "failed": sum(item["status"] == "failed" for item in state["segments"]),
                "text_integrity": state["integrity"],
                "duration_seconds": round(duration, 3),
                "wav_bytes": wav_path.stat().st_size,
                "mp3_bytes": mp3_path.stat().st_size,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    special = "# 标题\n\n数字 2026、English、**Markdown**、特殊符号 &<>。"
    special_preview = request_json(base_url + "/api/preview", method="POST", payload={"text": special, "max_chars": 80})
    assert special_preview["integrity"] is True
    print("SPECIAL_RESULT", json.dumps({"integrity": special_preview["integrity"], "segments": len(special_preview["segments"])}, ensure_ascii=False), flush=True)


class GateGenerator(FakeGenerator):
    def __init__(self) -> None:
        super().__init__()
        self.segment_one_started = threading.Event()
        self.release = threading.Event()

    def __call__(self, text: str, *, segment_index: int, seed: int | None, params: object) -> bytes:
        if segment_index == 1:
            self.segment_one_started.set()
            self.release.wait(10)
        return super().__call__(text, segment_index=segment_index, seed=seed, params={})


def recovery_check() -> None:
    root = Path(tempfile.mkdtemp(prefix="indextts-workbench-recovery-"))
    try:
        store = JobStore(root / "tasks")
        text = normalize_text("断点恢复测试。" * 12)
        manifest = JobManifest.create(text, max_chars=8, pause_ms=0, seed=9)
        store.create(manifest)
        first_generator = GateGenerator()
        first_queue = SequentialJobQueue(store, first_generator)
        first_queue.submit(manifest)
        assert first_generator.segment_one_started.wait(20)
        first_queue.cancel(manifest.job_id)
        first_generator.release.set()
        cancelled = first_queue.wait(manifest.job_id, timeout=30)
        first_queue.stop()
        assert cancelled.status == "cancelled"
        persisted = store.load(manifest.job_id)
        completed_before_restart = len(persisted.completed())
        second_generator = FakeGenerator()
        second_queue = SequentialJobQueue(store, second_generator)
        second_queue.resume(manifest.job_id)
        resumed = second_queue.wait(manifest.job_id, timeout=30)
        second_queue.stop()
        assert resumed.status == "completed"
        assert validate_integrity(resumed.normalized_text, resumed.segments)
        assert second_generator.calls and min(second_generator.calls) >= completed_before_restart
        print(
            "RECOVERY_RESULT",
            json.dumps(
                {
                    "before_restart_success": completed_before_restart,
                    "after_restart_status": resumed.status,
                    "after_restart_success": len(resumed.completed()),
                    "regenerated_indices": second_generator.calls,
                    "text_integrity": validate_integrity(resumed.normalized_text, resumed.segments),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8082")
    parser.add_argument("--max-chars", type=int, default=140)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--recovery-only", action="store_true")
    parser.add_argument("--job-id", help="validate an already completed WebUI job without generating it again")
    args = parser.parse_args()
    if not args.recovery_only:
        real_webui(args.base_url.rstrip("/"), max_chars=args.max_chars, timeout=args.timeout, existing_job=args.job_id)
    recovery_check()
    print("ACCEPTANCE_OK", flush=True)


if __name__ == "__main__":
    main()
