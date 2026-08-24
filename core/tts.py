"""Adapters for IndexTTS-2.5 and deterministic test generators."""

from __future__ import annotations

import io
import random
import threading
import wave
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping


def _wav_bytes(value: Any, sample_rate: int = 22_050) -> bytes:
    """Convert an IndexTTS result or waveform to mono 16-bit WAV bytes."""

    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], (int, float)):
        sample_rate = int(value[0])
        value = value[1]
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - server env supplies numpy
        raise RuntimeError("numpy is required to encode IndexTTS output") from exc
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    array = np.asarray(value)
    if array.size == 0:
        raise ValueError("IndexTTS returned an empty waveform")
    array = array.reshape(-1)
    if np.issubdtype(array.dtype, np.integer):
        pcm = np.clip(array, -32768, 32767).astype(np.int16)
    else:
        floats = array.astype(np.float32)
        peak = float(np.max(np.abs(floats))) if floats.size else 0.0
        if peak > 1.5:
            pcm = np.clip(floats, -32768.0, 32767.0).astype(np.int16)
        else:
            pcm = (np.clip(floats, -1.0, 1.0) * 32767.0).astype(np.int16)
    if not pcm.size:
        raise ValueError("IndexTTS returned an empty waveform")
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(int(sample_rate))
        writer.writeframes(pcm.tobytes())
    return output.getvalue()


@contextmanager
def _seeded(seed: int | None):
    """Keep previews/retries reproducible without changing process RNG state."""

    if seed is None:
        yield
        return
    python_state = random.getstate()
    random.seed(int(seed))
    try:
        import torch
    except ImportError:  # pragma: no cover
        try:
            yield
        finally:
            random.setstate(python_state)
        return
    devices = [torch.cuda.current_device()] if torch.cuda.is_available() else []
    try:
        with torch.random.fork_rng(devices=devices):
            torch.manual_seed(int(seed))
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(int(seed))
            yield
    finally:
        random.setstate(python_state)


def _audio_path(value: Any) -> str:
    if not value:
        raise ValueError("a reference audio file is required")
    path = Path(str(value)).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"reference audio not found: {path}")
    return str(path)


class IndexTTSGenerator:
    """Lazy IndexTTS-2.5 adapter.

    The model is loaded only when the first preview or job segment is run.
    ``CUDA_VISIBLE_DEVICES`` is set by the launcher, so the adapter can use
    IndexTTS' default ``cuda:0`` mapping without claiming a shared GPU.
    """

    def __init__(
        self,
        model_dir: str | Path = "/data1/ximizhou/indextts/checkpoints",
        *,
        device: str | None = None,
        use_bf16: bool = True,
        use_qwen_emo: bool = False,
        use_cuda_kernel: bool = False,
    ) -> None:
        self.model_dir = Path(model_dir).resolve()
        self.device = device
        self.use_bf16 = bool(use_bf16)
        self.use_qwen_emo = bool(use_qwen_emo)
        self.use_cuda_kernel = bool(use_cuda_kernel)
        self.sample_rate = 22_050
        self.tts: Any | None = None
        self._load_lock = threading.RLock()
        self._infer_lock = threading.RLock()

    def _ensure_loaded(self) -> Any:
        with self._load_lock:
            if self.tts is not None:
                return self.tts
            if not self.model_dir.is_dir():
                raise FileNotFoundError(f"IndexTTS model directory not found: {self.model_dir}")
            try:
                from indextts.infer_v2_5 import IndexTTS2
            except ImportError as exc:  # pragma: no cover - exercised outside server env
                raise RuntimeError("IndexTTS is not installed in the active environment") from exc
            kwargs: dict[str, Any] = {
                "cfg_path": str(self.model_dir / "config.yaml"),
                "model_dir": str(self.model_dir),
                "use_bf16": self.use_bf16,
                "use_qwen_emo": self.use_qwen_emo,
                "use_cuda_kernel": self.use_cuda_kernel,
            }
            if self.device:
                kwargs["device"] = self.device
            self.tts = IndexTTS2(**kwargs)
            return self.tts

    def resolve_reference(self, reference_audio: str | Path) -> str:
        return _audio_path(reference_audio)

    def __call__(
        self,
        text: str,
        *,
        segment_index: int,
        seed: int | None,
        params: Mapping[str, Any],
    ) -> bytes:
        tts = self._ensure_loaded()
        reference_audio = _audio_path(params.get("reference_audio"))
        lang = str(params.get("lang", "zh")).lower()
        emotion_audio = params.get("emotion_reference_audio") or None
        if emotion_audio:
            emotion_audio = _audio_path(emotion_audio)
        vector = params.get("emotion_vector")
        emotion_vector = None
        if isinstance(vector, (list, tuple)) and any(float(item) != 0.0 for item in vector):
            emotion_vector = [float(item) for item in vector[:8]]
            emotion_vector += [0.0] * (8 - len(emotion_vector))
        use_emo_text = bool(params.get("use_emo_text", False))
        if use_emo_text and not self.use_qwen_emo:
            raise RuntimeError("text emotion control is disabled; start with INDEXTTS_QWEN_EMO=1")
        generation_kwargs = {
            "do_sample": bool(params.get("do_sample", True)),
            "top_p": float(params.get("top_p", 0.72)),
            "top_k": int(params.get("top_k", 25)),
            "temperature": float(params.get("temperature", 0.65)),
            "length_penalty": float(params.get("length_penalty", 0.0)),
            "num_beams": int(params.get("num_beams", 3)),
            "repetition_penalty": float(params.get("repetition_penalty", 10.0)),
            "max_mel_tokens": int(params.get("max_mel_tokens", 1500)),
        }
        with self._infer_lock, _seeded(seed):
            result = tts.infer(
                spk_audio_prompt=reference_audio,
                text=text,
                output_path=None,
                lang=lang,
                emo_audio_prompt=emotion_audio,
                emo_alpha=float(params.get("emo_alpha", 1.0)),
                emo_vector=emotion_vector,
                use_emo_text=use_emo_text,
                emo_text=str(params.get("emotion_text", "")) or None,
                use_random=bool(params.get("emotion_random", False)),
                interval_silence=0,
                verbose=False,
                max_text_tokens_per_segment=int(params.get("max_text_tokens_per_segment", 120)),
                duration_factor=float(params.get("duration_factor", 1.0)),
                text_normalization=True,
                **generation_kwargs,
            )
        return _wav_bytes(result, self.sample_rate)


class FakeGenerator:
    """Small deterministic generator for integration tests and UI dry-runs."""

    def __init__(self, *, fail_indices: set[int] | None = None, sample_rate: int = 22_050) -> None:
        self.fail_indices = set(fail_indices or set())
        self.sample_rate = sample_rate
        self.calls: list[int] = []

    def __call__(self, text: str, *, segment_index: int, seed: int | None, params: Mapping[str, Any]) -> bytes:
        self.calls.append(segment_index)
        if segment_index in self.fail_indices:
            self.fail_indices.remove(segment_index)
            raise RuntimeError(f"intentional test failure at segment {segment_index}")
        frames = max(1, min(self.sample_rate // 10, len(text) * 8))
        output = io.BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(self.sample_rate)
            writer.writeframes(b"\0\0" * frames)
        return output.getvalue()


class FailOnceGenerator:
    """Explicit failure-injection wrapper used by acceptance tests only."""

    def __init__(self, inner: Any, fail_index: int) -> None:
        self.inner = inner
        self.fail_index = int(fail_index)
        self._failed = False
        self._lock = threading.Lock()

    def __call__(self, text: str, *, segment_index: int, seed: int | None, params: Mapping[str, Any]) -> Any:
        with self._lock:
            should_fail = segment_index == self.fail_index and not self._failed
            if should_fail:
                self._failed = True
        if should_fail:
            raise RuntimeError(f"intentional acceptance failure at segment {segment_index}")
        return self.inner(text, segment_index=segment_index, seed=seed, params=params)


__all__ = ["FailOnceGenerator", "FakeGenerator", "IndexTTSGenerator"]
