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

"""Speaker diarization: embeddings + clustering with automatic speaker count.

Two embedding backends:
  ecapa       — SpeechBrain ECAPA-TDNN (default). Multilingual, strong on
                conversational audio; runs on CPU (~1x realtime per minute of
                audio on a desktop core).
  resemblyzer — lightweight English-centric fallback (used automatically when
                speechbrain is not installed).

The number of speakers is *estimated*, never assumed: we build a kNN graph of
window embeddings and take the eigengap of its normalized Laplacian — a
well-established heuristic for the number of clusters. A clear gap means that
many speakers; no gap means one speaker. Clustering itself is agglomerative
(average linkage on cosine distance), which is robust to the noisy windows
real meetings contain (music, overlap, background noise).
"""
import logging
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("diar")

MAX_SPEAKERS = 8
_EIGENGAP_MIN = 0.03      # below this the graph looks like one blob: 1 speaker
_CENTROID_MERGE = 0.85    # merge centroids whose cosine sim exceeds this
_TINY_CLUSTER = 0.02      # clusters below 2% of windows are noise


@dataclass
class SpeakerSegment:
    start: float
    end: float
    text: str
    speaker: int = -1  # filled by assign_speakers()


def _embed_ecapa(audio: np.ndarray, sr: int, win_s: float, hop_s: float):
    from speechbrain.inference.speaker import EncoderClassifier
    import torch
    model = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir="/tmp/sb_ecapa", run_opts={"device": "cpu"})
    win, hop = int(win_s * sr), int(hop_s * sr)
    embs, times = [], []
    with torch.no_grad():
        for i in range(0, max(1, len(audio) - win), hop):
            chunk = torch.from_numpy(audio[i:i + win]).unsqueeze(0)
            embs.append(model.encode_batch(chunk).squeeze().numpy())
            times.append(i / sr)
    return np.stack(embs), np.array(times)


def _embed_resemblyzer(audio: np.ndarray, sr: int, win_s: float, hop_s: float):
    from resemblyzer import VoiceEncoder, preprocess_wav
    enc = VoiceEncoder("cpu")
    win, hop = int(win_s * sr), int(hop_s * sr)
    embs, times = [], []
    for i in range(0, max(1, len(audio) - win), hop):
        chunk = audio[i:i + win]
        if len(chunk) < win:
            chunk = np.concatenate([chunk, np.zeros(win - len(chunk))])
        prep = preprocess_wav(chunk, source_sr=sr)
        if len(prep) < int(0.5 * 16000):  # skip near-silent windows
            continue
        embs.append(enc.embed_utterance(prep))
        times.append(i / sr)
    if not embs:
        return np.zeros((0, 1)), np.zeros(0)
    return np.stack(embs), np.array(times)


def _estimate_k(S: np.ndarray, kmax: int = MAX_SPEAKERS, knn: int = 15):
    """Speaker-count estimate via eigengap of the kNN-graph Laplacian."""
    n = len(S)
    if n < 10:
        return 1
    kmax = min(kmax, max(1, n // 10))
    A = np.zeros_like(S)
    for i in range(n):
        nbr = np.argsort(-S[i])[:knn]
        A[i, nbr] = S[i, nbr]
        A[nbr, i] = S[nbr, i]
    deg = A.sum(1) + 1e-10
    dm = 1.0 / np.sqrt(deg)
    L = np.eye(n) - (A * dm[:, None]) * dm[None, :]
    vals = np.sort(np.linalg.eigvalsh(L))
    gaps = [(k, vals[k + 1] - vals[k]) for k in range(1, kmax + 1)]
    best_k, best_gap = max(gaps, key=lambda t: t[1])
    if best_gap < _EIGENGAP_MIN:
        return 1
    return best_k


def _cluster_labels(E: np.ndarray):
    """Estimate speaker count and label every window. Returns (labels, k)."""
    from sklearn.metrics.pairwise import cosine_similarity, cosine_distances
    from sklearn.cluster import AgglomerativeClustering

    En = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)
    sub = np.linspace(0, len(E) - 1, min(1200, len(E))).astype(int)
    S_sub = cosine_similarity(En[sub])
    k = _estimate_k(S_sub)
    log.info("speaker-count estimate: %d", k)
    if k < 2:
        return np.zeros(len(E), dtype=int), 1

    D_sub = cosine_distances(En[sub])
    lab_sub = AgglomerativeClustering(n_clusters=k, metric="precomputed",
                                      linkage="average").fit_predict(D_sub)
    sizes = np.bincount(lab_sub)
    keep = [c for c in range(k) if sizes[c] >= max(5, _TINY_CLUSTER * len(sub))]
    if len(keep) < 2:
        return np.zeros(len(E), dtype=int), 1

    cents = np.stack([En[sub][lab_sub == c].mean(0) for c in keep])
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


def diarize(audio: np.ndarray, sr: int, backend: str = "ecapa"):
    """Returns list of (start, end, speaker_id)."""
    log.info("diarizing %.1fs audio (backend=%s) ...", len(audio) / sr, backend)
    win_s, hop_s = (3.0, 1.5) if backend == "ecapa" else (2.0, 1.5)
    E = times = None
    if backend == "ecapa":
        try:
            E, times = _embed_ecapa(audio, sr, win_s, hop_s)
        except ImportError:
            log.warning("speechbrain not installed; falling back to resemblyzer")
            backend = "resemblyzer"
    if E is None or len(E) == 0:
        E, times = _embed_resemblyzer(audio, sr, win_s, hop_s)
    if E is None or len(E) == 0 or times is None:
        return []
    labels, k = _cluster_labels(E)
    segs = [(float(t), float(t) + win_s, int(lab)) for t, lab in zip(times, labels)]
    log.info("diarization done: %d windows -> %d speakers", len(segs), k)
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
