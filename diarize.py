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

"""Speaker diarization via resemblyzer embeddings + agglomerative clustering.

Runs on CPU (12-core 9900X is plenty for a meeting's worth of audio).
Produces per-segment speaker labels that get merged into the transcript.
"""
import logging
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger("diar")


@dataclass
class SpeakerSegment:
    start: float
    end: float
    text: str
    speaker: int = -1  # filled by assign()


def _embeddings_for(audio: np.ndarray, sr: int):
    """Yield (start, end, embedding) for ~2s windows with 0.5s hop."""
    from resemblyzer import VoiceEncoder, preprocess_wav
    enc = VoiceEncoder("cpu")
    win = int(2.0 * sr)
    hop = int(1.5 * sr)
    out = []
    for i in range(0, max(1, len(audio) - win), hop):
        chunk = audio[i:i + win]
        if len(chunk) < win:
            pad = np.zeros(win - len(chunk))
            chunk = np.concatenate([chunk, pad])
        # resemblyzer expects 16kHz float32 in [-1, 1]; audio is already at SR=16000
        prep = preprocess_wav(chunk, source_sr=sr)
        if len(prep) < int(0.5 * 16000):  # skip near-silent windows
            continue
        emb = enc.embed_utterance(prep)
        out.append((i / sr, (i + win) / sr, emb))
    return out


def _cluster(embeddings: list[np.ndarray], threshold: float = 0.75):
    """Agglomerative clustering on cosine distance. Returns label per embedding."""
    from sklearn.cluster import AgglomerativeClustering
    from sklearn.metrics.pairwise import cosine_distances
    E = np.stack(embeddings)
    D = cosine_distances(E)
    n = len(D)
    if n == 1:
        return [0]
    # cap cluster count heuristic: ~n/25 speakers, min 1 max 8
    max_speakers = max(1, min(8, int(n / 25) + 1))
    clu = AgglomerativeClustering(
        n_clusters=None, distance_threshold=threshold,
        metric="precomputed", linkage="average")
    labels = clu.fit_predict(D)
    # if too many clusters, merge by re-clustering with fewer
    if len(set(labels)) > max_speakers:
        clu2 = AgglomerativeClustering(
            n_clusters=max_speakers, metric="precomputed", linkage="average")
        labels = clu2.fit_predict(D)
    # relabel to 0..k-1 in order of first appearance
    mapping, out = {}, []
    for lab in labels:
        if lab not in mapping:
            mapping[lab] = len(mapping)
        out.append(mapping[lab])
    return out


def diarize(audio: np.ndarray, sr: int):
    """Returns list of (start, end, speaker_id)."""
    log.info("diarizing %.1fs audio ...", len(audio) / sr)
    embs = _embeddings_for(audio, sr)
    if not embs:
        return []
    labels = _cluster([e for _, _, e in embs])
    segs = [(s, e, lab) for (s, e, _), lab in zip(embs, labels)]
    log.info("diarization done: %d windows -> %d speakers",
             len(segs), len(set(labels)))
    return segs


def assign_speakers(speaker_segs, segments: list[SpeakerSegment]):
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
