"""Local FastAPI workbench for IndexTTS-2.5 long-form generation."""

from __future__ import annotations

import argparse
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from core.manifest import JobManifest
from core.queue import JobStore, SequentialJobQueue
from core.subtitles import write_srt_file
from core.text import normalize_text, read_text_file, split_text
from core.tts import FailOnceGenerator, IndexTTSGenerator
from core.voices import VoiceStore, validate_preview_id, validate_voice_id

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "app" / "static"
DEFAULT_TASKS_DIR = ROOT / "tasks"
DEFAULT_VOICES_DIR = ROOT / "voices"
DEFAULT_MODEL_DIR = "/data1/ximizhou/indextts/checkpoints"
DEFAULT_REFERENCE_DIR = "/data1/ximizhou/indextts/examples"
DEFAULT_FFMPEG = "/data1/ximizhou/envs/conda/indextts/bin/ffmpeg"
SEED_MAX = 2**32 - 1
LANGUAGES = {"zh", "en", "ja", "es", "ar"}


class GenerationSettings(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    seed: int = Field(default=42, ge=0, le=SEED_MAX)
    voice_id: str | None = Field(default=None, max_length=64)
    emotion_reference_voice_id: str | None = Field(default=None, max_length=64)
    lang: str = Field(default="zh", min_length=2, max_length=8)
    duration_factor: float = Field(default=1.0, ge=0.5, le=2.0)
    emo_alpha: float = Field(default=0.65, ge=0.0, le=1.0)
    emotion_vector: list[float] = Field(default_factory=lambda: [0.0] * 8, min_length=8, max_length=8)
    emotion_text: str = Field(default="", max_length=500)
    use_emo_text: bool = False
    emotion_random: bool = False
    max_text_tokens_per_segment: int = Field(default=120, ge=60, le=220)
    temperature: float = Field(default=0.65, ge=0.1, le=1.5)
    top_p: float = Field(default=0.72, ge=0.1, le=1.0)
    top_k: int = Field(default=25, ge=1, le=100)
    max_mel_tokens: int = Field(default=1500, ge=500, le=3000)
    repetition_penalty: float = Field(default=10.0, ge=1.0, le=20.0)
    num_beams: int = Field(default=3, ge=1, le=5)
    do_sample: bool = True

    @field_validator("voice_id", "emotion_reference_voice_id")
    @classmethod
    def valid_voice_id(cls, value: str | None) -> str | None:
        return validate_voice_id(value) if value else None

    @field_validator("lang")
    @classmethod
    def valid_language(cls, value: str) -> str:
        value = value.strip().lower().replace("zh-cn", "zh").replace("en-us", "en")
        if value not in LANGUAGES:
            raise ValueError(f"unsupported language: {value}")
        return value

    @field_validator("emotion_vector")
    @classmethod
    def valid_emotion_vector(cls, value: list[float]) -> list[float]:
        if len(value) != 8 or any(item < 0 or item > 1.2 for item in value):
            raise ValueError("emotion_vector must contain 8 values between 0 and 1.2")
        return [float(item) for item in value]


class JobCreate(GenerationSettings):
    text: str = Field(max_length=2_000_000)
    max_chars: int = Field(default=140, ge=80, le=240)
    pause_ms: int = Field(default=260, ge=0, le=10_000)
    terms: dict[str, str] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def text_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text cannot be empty")
        return value


class PreviewRequest(JobCreate):
    pass


class VoicePreviewRequest(GenerationSettings):
    text: str = Field(default="欢迎使用 IndexTTS 长音频工作台，这是一段参考音色试听。", max_length=500)

    @field_validator("text")
    @classmethod
    def preview_text_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("preview text cannot be empty")
        return value


class VoiceSaveRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    preview_id: str = Field(min_length=32, max_length=32)
    description: str = Field(default="", max_length=160)

    @field_validator("preview_id")
    @classmethod
    def valid_preview_id(cls, value: str) -> str:
        return validate_preview_id(value)


def _segment_dict(segment: Any) -> dict[str, Any]:
    if hasattr(segment, "to_dict"):
        value = dict(segment.to_dict())
    elif isinstance(segment, Mapping):
        value = dict(segment)
    else:
        text = str(getattr(segment, "text"))
        value = {"id": getattr(segment, "id"), "index": getattr(segment, "index"), "text": text, "char_count": len(text)}
    value.setdefault("char_count", len(str(value.get("text", ""))))
    value.setdefault("status", "pending")
    value.setdefault("attempts", 0)
    value.setdefault("audio_path", None)
    value.setdefault("error", None)
    if value.get("status") == "succeeded":
        value["status"] = "success"
    return value


def _public_settings(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key not in {"reference_audio", "emotion_reference_audio"}}


class JobManager:
    """Bridge HTTP actions to the durable core store and queue."""

    def __init__(self, tasks_dir: str | Path | None = None, *, generator: Any | None = None, ffmpeg: str | None = None, voices_dir: str | Path | None = None) -> None:
        configured = tasks_dir or os.getenv("INDEXTTS_WORKBENCH_TASKS") or DEFAULT_TASKS_DIR
        self.tasks_dir = Path(configured)
        self.store = JobStore(self.tasks_dir)
        voices_dir = voices_dir or os.getenv("INDEXTTS_WORKBENCH_VOICES") or (self.tasks_dir.parent / "voices" if tasks_dir is not None else DEFAULT_VOICES_DIR)
        reference_dir = os.getenv("INDEXTTS_REFERENCE_DIR", DEFAULT_REFERENCE_DIR)
        self.voices = VoiceStore(voices_dir, reference_dir=reference_dir)
        self.ffmpeg = ffmpeg or os.getenv("INDEXTTS_WORKBENCH_FFMPEG", DEFAULT_FFMPEG)
        if generator is None:
            use_qwen_emo = os.getenv("INDEXTTS_QWEN_EMO", "0").lower() in {"1", "true", "yes"}
            generator = IndexTTSGenerator(os.getenv("INDEXTTS_MODEL_DIR", DEFAULT_MODEL_DIR), use_qwen_emo=use_qwen_emo)
            injected_index = os.getenv("INDEXTTS_WORKBENCH_FAIL_INDEX_ONCE")
            if injected_index is not None:
                generator = FailOnceGenerator(generator, int(injected_index))
        self.generator = generator
        self.queue = SequentialJobQueue(self.store, self.generator, ffmpeg=self.ffmpeg)

    def _job_dir(self, job_id: str) -> Path:
        if not job_id or Path(job_id).name != job_id or "/" in job_id or "\\" in job_id:
            raise HTTPException(status_code=404, detail="invalid job id")
        return self.tasks_dir / job_id

    def _load(self, job_id: str) -> JobManifest:
        self._job_dir(job_id)
        try:
            return self.store.load(job_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @staticmethod
    def summary(manifest: JobManifest | Mapping[str, Any]) -> dict[str, Any]:
        payload = manifest.to_dict() if hasattr(manifest, "to_dict") else dict(manifest)
        segments = list(payload.get("segments") or [])
        counts: dict[str, int] = {"pending": 0, "running": 0, "success": 0, "failed": 0, "cancelled": 0}
        for segment in segments:
            status = "success" if segment.get("status") == "succeeded" else str(segment.get("status", "pending"))
            counts[status] = counts.get(status, 0) + 1
        total = len(segments)
        done = counts.get("success", 0)
        return {"job_id": payload.get("job_id"), "status": payload.get("status", "created"), "created_at": payload.get("created_at"), "updated_at": payload.get("updated_at"), "segment_count": total, "completed_count": done, "failed_count": counts.get("failed", 0), "progress": done / total if total else 0.0, "outputs": payload.get("outputs", {}), "error": payload.get("error")}

    @classmethod
    def api_manifest(cls, manifest: JobManifest) -> dict[str, Any]:
        value = manifest.to_dict()
        value["generation_params"] = _public_settings(manifest.generation_params)
        value["original_text"] = manifest.original_text
        value["original_text_hash"] = manifest.original_text_sha256
        value["settings"] = {"max_chars": manifest.max_chars, "pause_ms": manifest.pause_ms, "seed": manifest.seed, **_public_settings(manifest.generation_params)}
        value["segments"] = [_segment_dict(item) for item in value.get("segments", [])]
        value["chunks"] = value["segments"]
        value["integrity"] = "".join(item["text"] for item in value["segments"]) == manifest.normalized_text
        value["summary"] = cls.summary(value)
        return value

    def generation_settings(self, request: GenerationSettings) -> dict[str, Any]:
        voice_id = request.voice_id or "index-example-01"
        try:
            voice = self.voices.get(voice_id)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail="voice not found") from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        settings: dict[str, Any] = {
            "seed": request.seed,
            "voice_id": voice_id,
            "reference_audio": voice["reference_audio"],
            "lang": request.lang,
            "duration_factor": request.duration_factor,
            "emo_alpha": request.emo_alpha,
            "emotion_vector": request.emotion_vector,
            "emotion_text": request.emotion_text,
            "use_emo_text": request.use_emo_text,
            "emotion_random": request.emotion_random,
            "max_text_tokens_per_segment": request.max_text_tokens_per_segment,
            "temperature": request.temperature,
            "top_p": request.top_p,
            "top_k": request.top_k,
            "max_mel_tokens": request.max_mel_tokens,
            "repetition_penalty": request.repetition_penalty,
            "num_beams": request.num_beams,
            "do_sample": request.do_sample,
        }
        if request.emotion_reference_voice_id:
            try:
                emotion_voice = self.voices.get(request.emotion_reference_voice_id)
            except (KeyError, FileNotFoundError) as exc:
                raise HTTPException(status_code=400, detail="emotion reference voice not found") from exc
            settings["emotion_reference_voice_id"] = request.emotion_reference_voice_id
            settings["emotion_reference_audio"] = emotion_voice["reference_audio"]
        return settings

    def settings(self, request: JobCreate) -> dict[str, Any]:
        return {"max_chars": request.max_chars, "pause_ms": request.pause_ms, **_public_settings(self.generation_settings(request))}

    def preview(self, request: JobCreate) -> dict[str, Any]:
        normalized = normalize_text(request.text, terms=request.terms or None)
        segments = [_segment_dict(item) for item in split_text(normalized, max_chars=request.max_chars)]
        return {"original_text": request.text, "normalized_text": normalized, "segments": segments, "settings": self.settings(request), "integrity": "".join(item["text"] for item in segments) == normalized}

    def create(self, request: JobCreate) -> dict[str, Any]:
        preview = self.preview(request)
        settings = self.generation_settings(request)
        try:
            manifest = JobManifest.create(preview["normalized_text"], max_chars=request.max_chars, pause_ms=request.pause_ms, seed=request.seed, generation_params={key: value for key, value in settings.items() if key != "seed"}, job_id=datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8], original_text=request.text)
            self.store.create(manifest)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return self.api_manifest(manifest)

    def list_voices(self) -> dict[str, list[dict[str, Any]]]:
        return self.voices.list()

    def preview_voice(self, request: VoicePreviewRequest) -> dict[str, Any]:
        settings = self.generation_settings(request)
        voice = self.voices.get(settings["voice_id"])
        audio = self.generator(request.text, segment_index=0, seed=request.seed, params={key: value for key, value in settings.items() if key != "seed"})
        return self.voices.record_preview(audio, reference_audio=voice["reference_audio"], voice=voice, settings=_public_settings(settings))

    def save_voice(self, request: VoiceSaveRequest) -> dict[str, Any]:
        try:
            return self.voices.save_from_preview(name=request.name, preview_id=request.preview_id, description=request.description)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="voice preview not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def delete_voice(self, voice_id: str) -> None:
        try:
            self.voices.delete(voice_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="voice not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def voice_preview_audio(self, preview_id: str) -> Path:
        try:
            return self.voices.preview_audio(preview_id)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="voice preview not found") from exc

    def get(self, job_id: str) -> dict[str, Any]:
        return self.api_manifest(self._load(job_id))

    def list(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        paths = sorted(self.tasks_dir.glob("*/manifest.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        for path in paths:
            try:
                result.append(self.summary(self.store.load(path.parent.name)))
            except (OSError, ValueError, KeyError):
                continue
        return result

    def command(self, job_id: str, action: str) -> dict[str, Any]:
        self._load(job_id)
        try:
            if action == "start":
                self.queue.submit(job_id)
            elif action == "pause":
                self.queue.pause(job_id)
            elif action == "resume":
                self.queue.resume(job_id)
            elif action == "cancel":
                self.queue.cancel(job_id)
            elif action == "retry":
                self.queue.retry_failed(job_id)
            else:
                raise HTTPException(status_code=404, detail="unsupported action")
        except HTTPException:
            raise
        except (RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return self.get(job_id)

    def segment_audio(self, job_id: str, index: int) -> Path:
        manifest = self._load(job_id)
        if index < 0 or index >= len(manifest.segments):
            raise HTTPException(status_code=404, detail="segment not found")
        relative = manifest.segments[index].audio_path
        path = self._job_dir(job_id) / relative if relative else self._job_dir(job_id) / "segments" / f"{index:05d}-{manifest.segments[index].id}.wav"
        path = path.resolve()
        if self._job_dir(job_id).resolve() not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail="audio not generated")
        return path

    def output_file(self, job_id: str, kind: str) -> Path:
        if kind not in {"wav", "mp3", "srt"}:
            raise HTTPException(status_code=404, detail="unsupported output")
        manifest = self._load(job_id)
        if kind == "srt" and not manifest.outputs.get("srt"):
            if manifest.status != "completed" or any(segment.status != "succeeded" for segment in manifest.segments):
                raise HTTPException(status_code=404, detail="final output not ready")
            job_root = self._job_dir(job_id).resolve()
            subtitle_segments: list[tuple[str, Path]] = []
            for segment in manifest.segments:
                if not segment.audio_path:
                    raise HTTPException(status_code=404, detail="final output not ready")
                audio_path = (job_root / segment.audio_path).resolve()
                if job_root not in audio_path.parents or not audio_path.is_file():
                    raise HTTPException(status_code=404, detail="final output not ready")
                subtitle_segments.append((segment.text, audio_path))
            subtitle_path, cue_count = write_srt_file(
                subtitle_segments,
                job_root / "output" / "audio.srt",
                pause_ms=manifest.pause_ms,
            )
            manifest.outputs["srt"] = str(subtitle_path)
            manifest.outputs["subtitle_cues"] = cue_count
            self.store.save(manifest)
        value = manifest.outputs.get(kind)
        path = self._job_dir(job_id) / value if value else self._job_dir(job_id) / "output" / f"audio.{kind}"
        path = path.resolve()
        if self._job_dir(job_id).resolve() not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail="final output not ready")
        return path


def create_app(tasks_dir: str | Path | None = None, *, generator: Any | None = None, ffmpeg: str | None = None, voices_dir: str | Path | None = None) -> FastAPI:
    manager = JobManager(tasks_dir, generator=generator, ffmpeg=ffmpeg, voices_dir=voices_dir)
    application = FastAPI(title="IndexTTS LongAudio Workbench", version="1.0.0")
    application.state.jobs = manager
    if STATIC_DIR.is_dir():
        application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @application.get("/", response_class=HTMLResponse)
    async def index() -> str:
        index_path = STATIC_DIR / "index.html"
        return index_path.read_text(encoding="utf-8") if index_path.is_file() else "<h1>IndexTTS LongAudio Workbench</h1>"

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/health")
    async def api_health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/api/preview")
    async def preview(request: PreviewRequest) -> dict[str, Any]:
        return manager.preview(request)

    @application.post("/api/jobs")
    async def create_job(request: JobCreate) -> dict[str, Any]:
        return manager.create(request)

    @application.get("/api/voices")
    async def list_voices() -> dict[str, list[dict[str, Any]]]:
        return manager.list_voices()

    @application.post("/api/voices/preview")
    async def preview_voice(request: VoicePreviewRequest) -> dict[str, Any]:
        try:
            return await run_in_threadpool(manager.preview_voice, request)
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (FileNotFoundError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.get("/api/voices/previews/{preview_id}/audio")
    async def voice_preview_audio(preview_id: str) -> FileResponse:
        return FileResponse(manager.voice_preview_audio(preview_id), media_type="audio/wav", filename="voice-preview.wav")

    @application.post("/api/voices", status_code=201)
    async def save_voice(request: VoiceSaveRequest) -> dict[str, Any]:
        return manager.save_voice(request)

    @application.post("/api/voices/upload", status_code=201)
    async def upload_voice(file: UploadFile = File(...), name: str = Form(...), description: str = Form("")) -> dict[str, Any]:
        if not file.filename:
            raise HTTPException(status_code=400, detail="reference audio filename is required")
        data = await file.read()
        try:
            return manager.voices.add_uploaded(name=name, description=description, filename=file.filename, data=data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.delete("/api/voices/{voice_id}", status_code=204)
    async def delete_voice(voice_id: str) -> Response:
        manager.delete_voice(voice_id)
        return Response(status_code=204)

    @application.post("/api/jobs/upload")
    async def upload_job(
        file: UploadFile = File(...),
        max_chars: int = Form(140),
        pause_ms: int = Form(260),
        seed: int = Form(42),
        voice_id: str | None = Form(None),
        emotion_reference_voice_id: str | None = Form(None),
        lang: str = Form("zh"),
        duration_factor: float = Form(1.0),
        emo_alpha: float = Form(0.65),
        emotion_text: str = Form(""),
        use_emo_text: bool = Form(False),
        emotion_random: bool = Form(False),
        max_text_tokens_per_segment: int = Form(120),
        temperature: float = Form(0.65),
        top_p: float = Form(0.72),
        top_k: int = Form(25),
        max_mel_tokens: int = Form(1500),
        repetition_penalty: float = Form(10.0),
        num_beams: int = Form(3),
    ) -> dict[str, Any]:
        if not file.filename or Path(file.filename).suffix.lower() not in {".txt", ".md", ".markdown"}:
            raise HTTPException(status_code=400, detail="only .txt/.md/.markdown uploads are supported")
        raw = await file.read()
        if len(raw) > 20 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="text file exceeds 20 MiB")
        temporary = manager.tasks_dir / f".upload-{uuid.uuid4().hex}.txt"
        try:
            temporary.write_bytes(raw)
            text = read_text_file(temporary)
        except UnicodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            temporary.unlink(missing_ok=True)
        try:
            request = JobCreate(text=text, max_chars=max_chars, pause_ms=pause_ms, seed=seed, voice_id=voice_id, emotion_reference_voice_id=emotion_reference_voice_id, lang=lang, duration_factor=duration_factor, emo_alpha=emo_alpha, emotion_text=emotion_text, use_emo_text=use_emo_text, emotion_random=emotion_random, max_text_tokens_per_segment=max_text_tokens_per_segment, temperature=temperature, top_p=top_p, top_k=top_k, max_mel_tokens=max_mel_tokens, repetition_penalty=repetition_penalty, num_beams=num_beams)
        except ValidationError as exc:
            detail = [{"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]} for item in exc.errors(include_url=False, include_context=False, include_input=False)]
            raise HTTPException(status_code=422, detail=detail) from exc
        return manager.create(request)

    @application.get("/api/jobs")
    async def list_jobs() -> list[dict[str, Any]]:
        return manager.list()

    @application.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        return manager.get(job_id)

    @application.post("/api/jobs/{job_id}/{action}")
    async def job_action(job_id: str, action: str) -> dict[str, Any]:
        return manager.command(job_id, action)

    @application.get("/api/jobs/{job_id}/segments/{index}/audio")
    async def segment_audio(job_id: str, index: int) -> FileResponse:
        path = manager.segment_audio(job_id, index)
        return FileResponse(path, media_type="audio/wav", filename=path.name)

    @application.get("/api/jobs/{job_id}/download/{kind}")
    async def download(job_id: str, kind: str) -> FileResponse:
        path = manager.output_file(job_id, kind)
        media_types = {"wav": "audio/wav", "mp3": "audio/mpeg", "srt": "application/x-subrip; charset=utf-8"}
        return FileResponse(path, media_type=media_types[kind], filename=path.name)

    return application


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="IndexTTS LongAudio Workbench")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8082)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("service must bind to localhost only")
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()


__all__ = ["JobCreate", "JobManager", "PreviewRequest", "VoicePreviewRequest", "VoiceSaveRequest", "app", "create_app", "main"]
