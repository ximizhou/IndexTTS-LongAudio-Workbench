"""Reference-audio voice library for the IndexTTS workbench."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SOURCE_URL = "https://github.com/index-tts/index-tts"
VOICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PREVIEW_ID_RE = re.compile(r"^[a-f0-9]{32}$")
REFERENCE_SUFFIXES = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}

# These point to clips downloaded with the official repository. The broad
# voice filters come from acoustic pitch analysis and are not identity labels.
_OFFICIAL_EXAMPLE_GENDERS = {
    "01": "偏女声",
    "02": "偏女声",
    "03": "偏女声",
    "04": "偏女声",
    "05": "偏男声",
    "06": "偏男声",
    "07": "偏男声",
    "08": "偏女声",
    "09": "偏女声",
    "11": "偏女声",
    "12": "偏男声",
}
CURATED_VOICES: tuple[dict[str, Any], ...] = tuple(
    {
        "id": f"index-example-{number}",
        "name": f"官方示例 {number}",
        "description": "上游随附参考音频；声线分类仅用于筛选，请先试听。",
        "gender": gender,
        "style": "官方示例",
        "use_case": "通用试听",
        "reference_file": f"voice_{number}.wav",
    }
    for number, gender in _OFFICIAL_EXAMPLE_GENDERS.items()
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_voice_id(value: str) -> str:
    if not isinstance(value, str) or not VOICE_ID_RE.fullmatch(value):
        raise ValueError("invalid voice id")
    return value


def validate_preview_id(value: str) -> str:
    if not isinstance(value, str) or not PREVIEW_ID_RE.fullmatch(value):
        raise ValueError("invalid preview id")
    return value


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


class VoiceStore:
    """Filesystem-backed reference audio library and preview cache."""

    def __init__(self, root: str | Path, *, reference_dir: str | Path | None = None) -> None:
        self.root = Path(root).resolve()
        self.library_path = self.root / "library.json"
        self.references_dir = self.root / "references"
        self.previews_dir = self.root / "previews"
        self.reference_dir = Path(reference_dir or os.getenv("INDEXTTS_REFERENCE_DIR", "")).resolve() if reference_dir or os.getenv("INDEXTTS_REFERENCE_DIR") else None
        self._lock = threading.RLock()

    @staticmethod
    def _public(value: Mapping[str, Any]) -> dict[str, Any]:
        result = {key: item for key, item in value.items() if key not in {"reference_audio", "reference_file"}}
        result["has_reference"] = bool(value.get("reference_audio"))
        return result

    def _safe_path(self, root: Path, relative: str) -> Path:
        candidate = (root / relative).resolve()
        if root.resolve() not in candidate.parents or not candidate.is_file():
            raise FileNotFoundError(candidate)
        return candidate

    def _preset_reference(self, item: Mapping[str, Any]) -> Path | None:
        if self.reference_dir is None:
            return None
        path = (self.reference_dir / str(item["reference_file"])).resolve()
        if self.reference_dir.resolve() not in path.parents or not path.is_file():
            return None
        return path

    @staticmethod
    def presets(reference_dir: str | Path | None = None) -> list[dict[str, Any]]:
        root = Path(reference_dir).resolve() if reference_dir else None
        result: list[dict[str, Any]] = []
        for item in CURATED_VOICES:
            path = (root / item["reference_file"]).resolve() if root else None
            result.append({
                **item,
                "kind": "preset",
                "source_url": SOURCE_URL,
                "has_reference": bool(path and root and root in path.parents and path.is_file()),
            })
        return result

    def _load_library(self) -> dict[str, Any]:
        if not self.library_path.is_file():
            return {"schema_version": 1, "voices": []}
        with self.library_path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("voices"), list):
            raise ValueError("invalid voice library")
        voices: list[dict[str, Any]] = []
        for item in value["voices"]:
            if not isinstance(item, dict):
                raise ValueError("invalid voice library entry")
            voice_id = validate_voice_id(item.get("id", ""))
            name = str(item.get("name", "")).strip()
            reference_file = str(item.get("reference_file", "")).strip()
            if not name or len(name) > 40 or not reference_file:
                raise ValueError("invalid voice library entry")
            try:
                reference = self._safe_path(self.references_dir, reference_file)
            except FileNotFoundError:
                reference = None
            voices.append({
                **item,
                "id": voice_id,
                "name": name,
                "reference_file": reference_file,
                "reference_audio": str(reference) if reference else None,
                "kind": "saved",
            })
        return {"schema_version": 1, "voices": voices}

    def list(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            presets = []
            for item in CURATED_VOICES:
                value = {**item, "reference_audio": str(self._preset_reference(item)) if self._preset_reference(item) else None, "kind": "preset", "source_url": SOURCE_URL}
                presets.append(self._public(value))
            saved = [self._public(item) for item in self._load_library()["voices"]]
        return {"presets": presets, "saved": saved}

    def get(self, voice_id: str) -> dict[str, Any]:
        voice_id = validate_voice_id(voice_id)
        for preset in CURATED_VOICES:
            if preset["id"] == voice_id:
                path = self._preset_reference(preset)
                if path is None:
                    raise FileNotFoundError(f"reference audio is not available for {voice_id}")
                return {**preset, "kind": "preset", "source_url": SOURCE_URL, "reference_audio": str(path)}
        with self._lock:
            for voice in self._load_library()["voices"]:
                if voice["id"] == voice_id:
                    if not voice.get("reference_audio"):
                        raise FileNotFoundError(f"reference audio is not available for {voice_id}")
                    return dict(voice)
        raise KeyError(voice_id)

    def public(self, voice_id: str) -> dict[str, Any]:
        return self._public(self.get(voice_id))

    def _preview_path(self, preview_id: str, suffix: str) -> Path:
        preview_id = validate_preview_id(preview_id)
        target = (self.previews_dir / f"{preview_id}{suffix}").resolve()
        if target.parent != self.previews_dir.resolve():
            raise ValueError("invalid preview path")
        return target

    def record_preview(self, audio: bytes | bytearray | str | Path, *, reference_audio: str, voice: Mapping[str, Any] | None, settings: Mapping[str, Any]) -> dict[str, Any]:
        reference = _validated_reference(reference_audio)
        preview_id = uuid.uuid4().hex
        self.previews_dir.mkdir(parents=True, exist_ok=True)
        audio_path = self._preview_path(preview_id, ".wav")
        temporary = audio_path.with_name(f".{audio_path.name}.tmp")
        try:
            if isinstance(audio, (bytes, bytearray)):
                temporary.write_bytes(bytes(audio))
            else:
                shutil.copyfile(Path(audio), temporary)
            with wave.open(str(temporary), "rb") as reader:
                if reader.getnframes() <= 0:
                    raise ValueError("voice preview is empty")
            temporary.replace(audio_path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        metadata = {
            "schema_version": 1,
            "preview_id": preview_id,
            "created_at": utc_now(),
            "reference_audio": str(reference),
            "voice": self._public(voice) if voice else None,
            "settings": dict(settings),
        }
        _atomic_json(self._preview_path(preview_id, ".json"), metadata)
        return {"preview_id": preview_id, "audio_url": f"/api/voices/previews/{preview_id}/audio", "voice": metadata["voice"], "created_at": metadata["created_at"]}

    def preview_audio(self, preview_id: str) -> Path:
        path = self._preview_path(preview_id, ".wav")
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def _load_preview(self, preview_id: str) -> dict[str, Any]:
        path = self._preview_path(preview_id, ".json")
        if not path.is_file():
            raise FileNotFoundError(path)
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict) or value.get("preview_id") != preview_id:
            raise ValueError("invalid preview metadata")
        value["reference_audio"] = str(_validated_reference(value.get("reference_audio", "")))
        return value

    def save_from_preview(self, *, name: str, preview_id: str, description: str = "") -> dict[str, Any]:
        name = name.strip()
        description = description.strip()
        if not name or len(name) > 40:
            raise ValueError("voice name must contain 1-40 characters")
        if len(description) > 160:
            raise ValueError("voice description must contain at most 160 characters")
        with self._lock:
            preview = self._load_preview(validate_preview_id(preview_id))
            source = Path(preview["reference_audio"])
            voice_id = f"voice-{uuid.uuid4().hex[:16]}"
            suffix = source.suffix.lower() if source.suffix.lower() in REFERENCE_SUFFIXES else ".wav"
            reference_file = f"{voice_id}{suffix}"
            self.references_dir.mkdir(parents=True, exist_ok=True)
            target = self._safe_target(self.references_dir, reference_file)
            shutil.copyfile(source, target)
            source_voice = preview.get("voice") if isinstance(preview.get("voice"), dict) else {}
            voice = {
                "id": voice_id,
                "name": name,
                "description": description,
                "style": "我的音色",
                "kind": "saved",
                "reference_file": reference_file,
                "reference_audio": str(target),
                "created_at": utc_now(),
            }
            for key in ("gender", "age"):
                if source_voice.get(key):
                    voice[key] = str(source_voice[key])[:40]
            library = self._load_library()
            library["voices"].append({key: value for key, value in voice.items() if key != "reference_audio"})
            library["voices"] = [
                {key: value for key, value in item.items() if key != "reference_audio"}
                for item in library["voices"]
            ]
            _atomic_json(self.library_path, library)
            return self._public(voice)

    def add_uploaded(self, *, name: str, description: str, filename: str, data: bytes) -> dict[str, Any]:
        suffix = Path(filename).suffix.lower()
        if suffix not in REFERENCE_SUFFIXES:
            raise ValueError("unsupported reference audio format")
        if not data or len(data) > 50 * 1024 * 1024:
            raise ValueError("reference audio must be between 1 byte and 50 MiB")
        name = name.strip()
        description = description.strip()
        if not name or len(name) > 40 or len(description) > 160:
            raise ValueError("invalid voice name or description")
        with self._lock:
            voice_id = f"voice-{uuid.uuid4().hex[:16]}"
            reference_file = f"{voice_id}{suffix}"
            self.references_dir.mkdir(parents=True, exist_ok=True)
            target = self._safe_target(self.references_dir, reference_file)
            temporary = target.with_name(f".{target.name}.tmp")
            temporary.write_bytes(data)
            temporary.replace(target)
            voice = {"id": voice_id, "name": name, "description": description, "style": "我的音色", "kind": "saved", "reference_file": reference_file, "created_at": utc_now()}
            library = self._load_library()
            library["voices"].append(voice)
            library["voices"] = [
                {key: value for key, value in item.items() if key != "reference_audio"}
                for item in library["voices"]
            ]
            _atomic_json(self.library_path, library)
            return self._public({**voice, "reference_audio": str(target)})

    def delete(self, voice_id: str) -> None:
        voice_id = validate_voice_id(voice_id)
        if any(item["id"] == voice_id for item in CURATED_VOICES):
            raise PermissionError("built-in voices cannot be deleted")
        with self._lock:
            library = self._load_library()
            remaining = [item for item in library["voices"] if item["id"] != voice_id]
            if len(remaining) == len(library["voices"]):
                raise KeyError(voice_id)
            removed = next(item for item in library["voices"] if item["id"] == voice_id)
            reference = self.references_dir / str(removed.get("reference_file", ""))
            if reference.is_file():
                reference.unlink()
            library["voices"] = remaining
            _atomic_json(self.library_path, library)

    def _safe_target(self, root: Path, filename: str) -> Path:
        target = (root / filename).resolve()
        if target.parent != root.resolve():
            raise ValueError("invalid reference path")
        return target


def _validated_reference(value: str | Path) -> Path:
    path = Path(str(value)).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() not in REFERENCE_SUFFIXES:
        raise FileNotFoundError(path)
    return path


__all__ = ["CURATED_VOICES", "SOURCE_URL", "VoiceStore", "validate_preview_id", "validate_voice_id"]
