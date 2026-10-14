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

"""Speaker diarization: VAD-gated embeddings + clustering, speaker count estimated.

Pipeline:
  1. Voice activity detection (silero-vad) — embedding silence or windows that
     straddle speaker changes just adds noise, so we only embed speech chunks
     (merged into natural 1.5–8 s turns).
  2. Embeddings: SpeechBrain ECAPA-TDNN (default; multilingual, strong on
     conversational audio, CPU-friendly). Fallback: resemblyzer (lighter,
     English-centric) when speechbrain is not installed.
  3. Speaker count: agglomerative clustering (average linkage on cosine
     distance) for k = 2..8, keeping the k with the best silhouette score.
     Silence-free chunks make the silhouette curve peak cleanly at the true
     count; a weak peak means one speaker. Tiny clusters are noise (coughs,
     background TV) and get merged into the nearest real centroid.
"""
import logging
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("diar")

MAX_SPEAKERS = 8
_MIN_CHUNK_S = 1.5
_MAX_CHUNK_S = 8.0
_SILHOUETTE_MIN = 0.18   # best score below this -> single speaker
_TINY_CLUSTER = 0.03     # clusters below 3% of chunks are noise
_CENTROID_MERGE = 0.85   # merge centroids whose cosine sim exceeds this


@dataclass
class SpeakerSegment:
    start: float
    end: float
    text: str
    speaker: int = -1  # filled by assign_speakers()


# ------------------------------------------------------------------ VAD

def _speech_chunks(audio: np.ndarray, sr: int):
    """Return [(start, end)] speech chunks via silero-vad; falls back to a
    coarse energy gate when silero-vad is not installed."""
    try:
        import torch
        from silero_vad import load_silero_vad, get_speech_timestamps
        ts = get_speech_timestamps(torch.from_numpy(audio), load_silero_vad(),
                                   sampling_rate=sr, speech_pad_ms=150,
                                   min_speech_duration_ms=250)
        regions = [(t["start"] / sr, t["end"] / sr) for t in ts]
    except ImportError:
        log.info("silero-vad not installed; using energy gate for VAD")
        regions = _energy_regions(audio, sr)
    # merge regions separated by short pauses into natural turn-sized chunks
    chunks, cur = [], None
    for s, e in regions:
        if cur and s - cur[1] < 0.4:
            cur = (cur[0], e)
        else:
            if cur:
                chunks.append(cur)
            cur = (s, e)
    if cur:
        chunks.append(cur)
    chunks = [c for c in chunks if c[1] - c[0] >= _MIN_CHUNK_S]
    # split long monologues so one speaker can't dominate a whole cluster
    out = []
    for s, e in chunks:
        while e - s > _MAX_CHUNK_S:
            out.append((s, s + _MAX_CHUNK_S * 0.625))
            s += _MAX_CHUNK_S * 0.625
        out.append((s, e))
    return out


def _energy_regions(audio: np.ndarray, sr: int, win_s: float = 0.3):
    """Crude fallback gate: windows above 2.5x the low-energy floor."""
    win = max(1, int(win_s * sr))
    n = len(audio) // win
    rms = np.sqrt((audio[:n * win].reshape(n, win) ** 2).mean(axis=1))
    floor = np.percentile(rms, 20) + 1e-9
    voiced = rms > floor * 2.5
    regions, start = [], None
    for i, v in enumerate(voiced):
        if v and start is None:
            start = i * win_s
        elif not v and start is not None:
            regions.append((start, i * win_s))
            start = None
    if start is not None:
        regions.append((start, n * win_s))
    return regions


# ------------------------------------------------------------------ embeddings

def _embed_ecapa(chunks, audio: np.ndarray, sr: int):
    from speechbrain.inference.speaker import EncoderClassifier
    import torch
    model = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir="/tmp/sb_ecapa", run_opts={"device": "cpu"})
    embs = []
    min_len = int(1.6 * sr)
    with torch.no_grad():
        for s, e in chunks:
            seg = audio[int(s * sr):int(e * sr)]
            if len(seg) < min_len:
                seg = np.concatenate([seg, np.zeros(min_len - len(seg))])
            embs.append(model.encode_batch(
                torch.from_numpy(seg).unsqueeze(0)).squeeze().numpy())
    return np.stack(embs)


def _embed_resemblyzer(chunks, audio: np.ndarray, sr: int):
    from resemblyzer import VoiceEncoder, preprocess_wav
    enc = VoiceEncoder("cpu")
    embs = []
    for s, e in chunks:
        prep = preprocess_wav(audio[int(s * sr):int(e * sr)], source_sr=sr)
        if len(prep) < int(0.5 * 16000):
            prep = np.concatenate([prep, np.zeros(int(0.5 * 16000) - len(prep))])
        embs.append(enc.embed_utterance(prep))
    if not embs:
        return np.zeros((0, 1))
    return np.stack(embs)


# ------------------------------------------------------------------ clustering

def _cluster_labels(En: np.ndarray):
    """Pick k by silhouette peak over 2..MAX_SPEAKERS. Returns (labels, k)."""
    from sklearn.metrics.pairwise import cosine_distances
    from sklearn.cluster import AgglomerativeClustering
    from sklearn.metrics import silhouette_score

    n = len(En)
    if n < 10:
        return np.zeros(n, dtype=int), 1
    D = cosine_distances(En)
    kmax = min(MAX_SPEAKERS, max(2, n // 25))
    best_k, best_sc, best_lab = 1, -1.0, None
    for kk in range(2, kmax + 1):
        lab = AgglomerativeClustering(n_clusters=kk, metric="precomputed",
                                      linkage="average").fit_predict(D)
        sc = silhouette_score(D, lab, metric="precomputed")
        log.info("k=%d silhouette=%.3f", kk, sc)
        if sc > best_sc:
            best_k, best_sc, best_lab = kk, sc, lab
    if best_k < 2 or best_lab is None or best_sc < _SILHOUETTE_MIN:
        return np.zeros(n, dtype=int), 1

    # drop noise clusters, reassign their windows to the nearest big centroid
    sizes = np.bincount(best_lab, minlength=best_k)
    keep = [c for c in range(best_k) if sizes[c] >= max(3, _TINY_CLUSTER * n)]
    if len(keep) < 2:
        return np.zeros(n, dtype=int), 1
    cents = np.stack([En[best_lab == c].mean(0) for c in keep])
    cents /= (np.linalg.norm(cents, axis=1, keepdims=True) + 1e-9)
    # merge near-duplicate centroids (one speaker split in two)
    while len(cents) > 1:
        cs = cents @ cents.T
        np.fill_diagonal(cs, -1.0)
        i, j = np.unravel_index(cs.argmax(), cs.shape)
        if cs[i, j] <= _CENTROID_MERGE:
            break
        newc = cents[[i, j]].mean(0)
        cents = np.vstack([np.delete(cents, [i, j], axis=0),
                           newc / (np.linalg.norm(newc) + 1e-9)])
    labels = (En @ cents.T).argmax(1)
    # relabel in order of first appearance
    mapping, out = {}, []
    for lab in labels:
        if lab not in mapping:
            mapping[lab] = len(mapping)
        out.append(mapping[lab])
    return np.array(out), len(mapping)


# ------------------------------------------------------------------ entry point

def diarize(audio: np.ndarray, sr: int, backend: str = "ecapa"):
    """Returns list of (start, end, speaker_id)."""
    log.info("diarizing %.1fs audio (backend=%s) ...", len(audio) / sr, backend)
    chunks = _speech_chunks(audio, sr)
    if not chunks:
        return []
    log.info("vad: %d speech chunks (%.1f min)",
             len(chunks), sum(e - s for s, e in chunks) / 60)
    E = None
    if backend == "ecapa":
        try:
            E = _embed_ecapa(chunks, audio, sr)
        except ImportError:
            log.warning("speechbrain not installed; falling back to resemblyzer")
    if E is None or len(E) == 0:
        E = _embed_resemblyzer(chunks, audio, sr)
    if E is None or len(E) == 0:
        return []
    En = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)
    labels, k = _cluster_labels(En)
    segs = [(float(s), float(e), int(lab)) for (s, e), lab in zip(chunks, labels)]
    log.info("diarization done: %d chunks -> %d speakers", len(segs), k)
    return segs


def assign_speakers(speaker_segs, segments):
    """Attach a speaker id to each transcript segment by overlap."""
    if not speaker_segs:
        for s in segments:
            s.speaker = 0
        return
    for s in segments:
        best, best_ov = -1, 0.0
        for (ss, se, lab) in speaker_segs:
            ov = min(s.end, se) - max(s.start, ss)
            if ov > best_ov:
                best_ov, best = ov, lab
        s.speaker = best if best >= 0 else 0
