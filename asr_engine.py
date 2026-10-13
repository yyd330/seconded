# Seconded — local meeting transcription & summarization
# Copyright (C) 2026 Seconded contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Pluggable ASR engine.

Backends (registry: BACKENDS):
  qwen            Qwen3-ASR (default) + ForcedAligner, via the `qwen-asr` package.
  faster-whisper  Any faster-whisper / Whisper model (large-v3, distil-*,
                  Systran/faster-whisper-* CT2 repos, local dirs, ...).

Select with env vars (or the matching CLI flags on server.py):
  ASR_BACKEND   qwen | faster-whisper          (default: qwen)
  ASR_MODEL     HF repo id or local model dir  (default: Qwen/Qwen3-ASR-1.7B)
  ASR_ALIGNER   qwen backend only: forced aligner for word timestamps
  ASR_DEVICE    cpu (default) | cuda

Both backends expose the same API:
  transcribe(audio, sr, language=None)                 -> (language, text)
  transcribe_with_timestamps(audio, sr, language=None) -> (language, text, items)
    items: objects with .start_time / .end_time / .text in global seconds.

Qwen long-audio note: Qwen3-ASR degrades (garbage output) when a single input
exceeds ~70 s, so the qwen backend splits audio into <= SAFE_CHUNK_SEC pieces
at low-energy boundaries and offsets timestamps back to global time.
faster-whisper handles long audio natively (no splitting needed).
"""
import logging
import os
import threading
import time
from types import SimpleNamespace

log = logging.getLogger("asr")

# Defaults — overridable per-instance (CLI) or via env vars in ASREngine().
DEFAULT_BACKEND = "qwen"
DEFAULT_MODEL = {"qwen": "Qwen/Qwen3-ASR-1.7B",
                 "faster-whisper": "Systran/faster-whisper-large-v3"}
DEFAULT_ALIGNER = "Qwen/Qwen3-ForcedAligner-0.6B"

# Keep each Qwen ASR call comfortably under the model's reliable range
# (45 s worked, 149 s produced garbage). 45 s leaves margin.
SAFE_CHUNK_SEC = 45


class BaseASRBackend:
    """Interface both backends implement."""

    def __init__(self, model: str, device: str = "cpu", aligner: str = None):
        self.model_id = model
        self.device = device
        self.aligner_id = aligner
        self.model = None

    def load(self):
        raise NotImplementedError

    def transcribe(self, audio, sr: int = 16000, language=None):
        raise NotImplementedError

    def transcribe_with_timestamps(self, audio, sr: int = 16000, language=None):
        raise NotImplementedError

    @property
    def description(self) -> str:
        return f"{self.model_id} ({type(self).__name__}, {self.device})"


class QwenASRBackend(BaseASRBackend):
    """Qwen3-ASR + optional ForcedAligner (word timestamps)."""

    def load(self):
        import torch
        from qwen_asr import Qwen3ASRModel
        t0 = time.time()
        log.info("loading ASR %s (aligner: %s) ...", self.model_id,
                 self.aligner_id or "none")
        kwargs = {}
        if self.aligner_id:
            kwargs["forced_aligner"] = self.aligner_id
            kwargs["forced_aligner_kwargs"] = dict(dtype=torch.bfloat16,
                                                   device_map=self.device)
        self.model = Qwen3ASRModel.from_pretrained(
            self.model_id, dtype=torch.bfloat16, device_map=self.device,
            max_inference_batch_size=2, max_new_tokens=4096, **kwargs)
        log.info("ASR ready in %.1fs", time.time() - t0)

    # ------------------------------------------------------------------ helpers

    def _chunks(self, audio, sr: int):
        """Split into (chunk_wav, offset_sec) pieces <= SAFE_CHUNK_SEC.
        Uses the package's low-energy-boundary splitter; a single chunk is
        returned for short audio."""
        from qwen_asr.inference.utils import split_audio_into_chunks
        if len(audio) / sr <= SAFE_CHUNK_SEC:
            return [(audio, 0.0)]
        parts = split_audio_into_chunks(wav=audio, sr=sr, max_chunk_sec=SAFE_CHUNK_SEC)
        log.info("splitting %.1fs audio into %d chunks (<= %ds each)",
                 len(audio) / sr, len(parts), SAFE_CHUNK_SEC)
        return parts

    @staticmethod
    def _offset_items(items, offset: float):
        """Shift aligner items to global time; returns SimpleNamespace list."""
        out = []
        for it in (items or []):
            st = getattr(it, "start_time", None)
            en = getattr(it, "end_time", None)
            tx = str(getattr(it, "text", "")).strip()
            if st is None and isinstance(it, (tuple, list)) and len(it) >= 3:
                st, en, tx = it[0], it[1], str(it[2]).strip()
            if st is None or en is None:
                continue
            out.append(SimpleNamespace(start_time=round(float(st) + offset, 3),
                                       end_time=round(float(en) + offset, 3),
                                       text=tx))
        return out

    # ------------------------------------------------------------------ API

    def transcribe(self, audio, sr: int = 16000, language=None):
        if self.model is None:
            raise RuntimeError("ASR model not loaded")
        t0 = time.time()
        parts = self._chunks(audio, sr)
        texts, langs = [], []
        for wav, _off in parts:
            r = self.model.transcribe(audio=(wav, sr), language=language)[0]
            if (r.text or "").strip():
                texts.append(r.text.strip())
                langs.append(r.language)
        from qwen_asr.inference.utils import merge_languages
        log.info("transcribed %.1fs audio (%d chunk%s) in %.1fs",
                 len(audio) / sr, len(parts), "s" if len(parts) != 1 else "",
                 time.time() - t0)
        return (merge_languages(langs) if langs else ""), "".join(texts)

    def transcribe_with_timestamps(self, audio, sr: int = 16000, language=None):
        if self.model is None:
            raise RuntimeError("ASR model not loaded")
        t0 = time.time()
        parts = self._chunks(audio, sr)
        texts, langs, all_items = [], [], []
        for wav, off in parts:
            r = self.model.transcribe(
                audio=(wav, sr), language=language,
                return_time_stamps=bool(self.aligner_id))[0]
            txt = (r.text or "").strip()
            if not txt:
                continue
            texts.append(txt)
            langs.append(r.language)
            all_items.extend(self._offset_items(
                getattr(r, "time_stamps", None), off))
        from qwen_asr.inference.utils import merge_languages
        log.info("timestamped %.1fs audio (%d chunk%s) in %.1fs",
                 len(audio) / sr, len(parts), "s" if len(parts) != 1 else "",
                 time.time() - t0)
        return (merge_languages(langs) if langs else ""), "".join(texts), all_items


class FasterWhisperBackend(BaseASRBackend):
    """Any faster-whisper (CTranslate2) model: large-v3, distil-large-v3,
    quantized CT2 repos, or a local directory. Timestamps come from
    word_timestamps; no separate aligner needed."""

    def load(self):
        from faster_whisper import WhisperModel
        t0 = time.time()
        log.info("loading faster-whisper model %s on %s ...",
                 self.model_id, self.device)
        self.model = WhisperModel(self.model_id, device=self.device,
                                  compute_type="auto")
        log.info("ASR ready in %.1fs", time.time() - t0)

    def _segments(self, audio, sr, language):
        if sr != 16000:
            import librosa
            import numpy as np
            audio = librosa.resample(np.asarray(audio, dtype="float32"),
                                     orig_sr=sr, target_sr=16000)
        seg_iter, info = self.model.transcribe(
            audio, language=language or None, word_timestamps=True,
            vad_filter=True)
        return seg_iter, (info.language or "")

    @staticmethod
    def _collect(seg_iter):
        texts, items = [], []
        for seg in seg_iter:
            txt = (seg.text or "").strip()
            if not txt:
                continue
            texts.append(txt)
            for w in (seg.words or []):
                items.append(SimpleNamespace(start_time=round(float(w.start), 3),
                                             end_time=round(float(w.end), 3),
                                             text=w.word.strip()))
        return texts, items

    def transcribe(self, audio, sr: int = 16000, language=None):
        if self.model is None:
            raise RuntimeError("ASR model not loaded")
        t0 = time.time()
        seg_iter, lang = self._segments(audio, sr, language)
        texts, _ = self._collect(seg_iter)
        log.info("transcribed %.1fs audio in %.1fs", len(audio) / sr, time.time() - t0)
        return lang, " ".join(texts)

    def transcribe_with_timestamps(self, audio, sr: int = 16000, language=None):
        if self.model is None:
            raise RuntimeError("ASR model not loaded")
        t0 = time.time()
        seg_iter, lang = self._segments(audio, sr, language)
        texts, items = self._collect(seg_iter)
        log.info("timestamped %.1fs audio in %.1fs", len(audio) / sr, time.time() - t0)
        return lang, " ".join(texts), items


BACKENDS = {
    "qwen": QwenASRBackend,
    "faster-whisper": FasterWhisperBackend,
    # friendly aliases
    "faster_whisper": FasterWhisperBackend,
    "whisper": FasterWhisperBackend,
}


class ASREngine:
    """Thread-safe facade over a selectable ASR backend.

    backend/model/aligner/device resolve as: explicit arg > env var
    (ASR_BACKEND / ASR_MODEL / ASR_ALIGNER / ASR_DEVICE) > default.
    """

    def __init__(self, backend: str = None, model: str = None,
                 aligner: str = None, device: str = None):
        self._lock = threading.Lock()  # one transcription at a time
        self.backend_name = (backend or os.environ.get("ASR_BACKEND")
                             or DEFAULT_BACKEND).lower()
        if self.backend_name not in BACKENDS:
            raise ValueError(f"unknown ASR backend {self.backend_name!r}; "
                             f"choose one of: {', '.join(sorted(BACKENDS))}")
        self.model_id = (model or os.environ.get("ASR_MODEL")
                         or DEFAULT_MODEL[self.backend_name])
        self.aligner_id = (aligner if aligner is not None
                           else os.environ.get("ASR_ALIGNER", DEFAULT_ALIGNER))
        if self.aligner_id == "none":
            self.aligner_id = None
        self.device = (device or os.environ.get("ASR_DEVICE") or "cpu").lower()
        self._backend = None  # constructed on first load

    @property
    def model(self):
        """Back-compat: server checks `engine.model is None` to know if loaded."""
        return self._backend.model if self._backend else None

    @model.setter
    def model(self, value):
        # back-compat alias used by old code paths; ignored for new backends
        if self._backend is None and value is not None:
            raise RuntimeError("set the backend via ASREngine(backend=...)")

    @property
    def loaded(self) -> bool:
        return bool(self._backend and self._backend.model is not None)

    @property
    def description(self) -> str:
        return (f"{self.model_id} via {self.backend_name} ({self.device})"
                + (f" + {self.aligner_id}" if self.aligner_id
                   and self.backend_name == "qwen" else ""))

    def load_final(self):
        """Load the configured backend (idempotent)."""
        with self._lock:
            if self._backend is None:
                cls = BACKENDS[self.backend_name]
                self._backend = cls(self.model_id, device=self.device,
                                    aligner=self.aligner_id)
            if self._backend.model is None:
                self._backend.load()

    def configure(self, backend=None, model=None, aligner=None, device=None):
        """Override selection before first load (used by server CLI flags)."""
        if self.loaded:
            raise RuntimeError("cannot reconfigure ASR after load")
        if backend:
            self.backend_name = backend.lower()
            if self.backend_name not in BACKENDS:
                raise ValueError(f"unknown ASR backend {backend!r}; "
                                 f"choose one of: {', '.join(sorted(BACKENDS))}")
        if model:
            self.model_id = model
        if aligner is not None:
            self.aligner_id = None if aligner in ("", "none") else aligner
        if device:
            self.device = device.lower()

    # kept for back-compat with server call sites
    def transcribe(self, audio, sr: int = 16000, language=None):
        if not self.loaded:
            raise RuntimeError("ASR model not loaded")
        with self._lock:
            return self._backend.transcribe(audio, sr, language)

    def transcribe_with_timestamps(self, audio, sr: int = 16000, language=None):
        if not self.loaded:
            raise RuntimeError("ASR model not loaded")
        with self._lock:
            return self._backend.transcribe_with_timestamps(audio, sr, language)
