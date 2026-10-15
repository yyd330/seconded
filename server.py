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

"""Seconded server.

Modes:
  python server.py                 # wait for a client to stream audio in
  python server.py --file X.wav    # run the full pipeline on a local file (test)
  python server.py --no-live       # skip live subtitles; only final pass at end

Flow (stream mode):
  POST /start            -> new session, load ASR model if needed
  POST /ingest           # raw s16le mono 16kHz PCM chunks (any size)
  GET  /events           # SSE: live subtitle lines + status updates
  POST /end              # stop ingest; final pass: ASR (+timestamps), diarize,
                         # summarize via local OpenAI-compatible backend;
                         # artifacts -> outputs dir
  POST /cancel           # force-stop the current run at its next checkpoint

Upload mode (web UI "Upload file"):
  POST /upload           # multipart field 'file' — any audio or video container;
                         # ffmpeg extracts the audio track, then same pipeline as above.

Web UI at http://<host>:8765/ shows live subtitles with speaker colors.
Downloads: /download/transcript.md, /download/summary.md, /download/vtt, /download/pdf
"""
import argparse
import faulthandler
import hmac
import json
import logging
import os
import re
import signal
import shutil
import socket
import subprocess
import tempfile
import threading
import time

# SIGUSR1 → full thread dump to stderr (non-fatal), for hang debugging
faulthandler.register(signal.SIGUSR1, all_threads=True)
from collections import deque
from datetime import datetime

import numpy as np
import soundfile as sf
from flask import Flask, Response, jsonify, request

from asr_engine import ASREngine, BACKENDS
from diarize import SpeakerSegment, assign_speakers, diarize
import summarize as _summarize_mod
from summarize import summarize, LMSTUDIO_TIMEOUT
import report

SR = 16000
LIVE_WINDOW_S = 12      # rolling window for live subtitles (demo mode)
OUTPUT_ROOT = os.path.expanduser(
    os.environ.get("MEETINGS_OUTPUT_DIR", "~/.local/share/seconded/meetings"))

# Live-subtitle mode: "final" (default — the configured ASR model does live +
# final) or None (--no-live).
LIVE_MODE = "final"
# Pre-transcribe the meeting in blocks while it runs (subtitles come from the
# same pass; the post-meeting wait shrinks to tail + diarization + summary).
INCREMENTAL = True
DIAR_BACKEND = os.environ.get("DIAR_BACKEND", "ecapa")  # ecapa | resemblyzer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("server")

app = Flask(__name__)
# uploads: allow up to 2 GB (long meeting recordings)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 ** 3
engine = ASREngine()

# ---------------------------------------------------------------- auth (opt-in)
# Off by default: local/LAN users need no credentials. Enable with
# --auth-token / SECONDED_TOKEN when the server might be reachable by others.
AUTH_TOKEN = None
AUTH_COOKIE = "seconded_token"
# paths reachable without a token: login itself + the CA download (needed to
# trust the HTTPS cert before the browser will talk to the app at all)
AUTH_EXEMPT = {"/login", "/rootca", "/favicon.ico"}

LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Seconded — sign in</title><style>
body{font:15px/1.6 -apple-system,"Segoe UI",Roboto,sans-serif;background:#0b0e14;
color:#e7ecf5;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.card{background:#131826;border:1px solid #252d42;border-radius:14px;padding:32px;
width:min(360px,90vw)}
h1{font-size:20px;margin:0 0 4px}.sub{color:#8b96ad;font-size:13px;margin-bottom:20px}
input{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;
border:1px solid #252d42;background:#0b0e14;color:#e7ecf5;font-size:15px}
button{margin-top:14px;width:100%;padding:10px;border:0;border-radius:8px;
background:#5b8cff;color:#fff;font-size:15px;cursor:pointer}
.err{color:#ff5d5d;font-size:13px;margin-top:10px;display:none}
</style></head><body><form class="card" method="POST" action="/login">
<h1>🎙 Seconded</h1><div class="sub">This server is protected. Enter its access token.</div>
<input type="password" name="token" placeholder="Access token" autofocus required>
<button type="submit">Sign in</button>
<div class="err" __ERR__>Wrong token — try again.</div>
</form></body></html>"""


def _token_ok(candidate):
    if not candidate or AUTH_TOKEN is None:
        return False
    # encode to bytes: compare_digest rejects non-ASCII str (a hostile client
    # sending unicode tokens must get 401, not a 500)
    return hmac.compare_digest(str(candidate).encode("utf-8"),
                               str(AUTH_TOKEN).encode("utf-8"))


def _wants_html():
    """True for browser navigations/form posts (prefer text/html over JSON)."""
    accept = request.accept_mimetypes
    return accept["text/html"] > 0 and accept["text/html"] >= accept["application/json"]


@app.before_request
def require_token():
    if not AUTH_TOKEN or request.path in AUTH_EXEMPT:
        return None
    if _token_ok(request.cookies.get(AUTH_COOKIE)) or \
       _token_ok(request.headers.get("X-Seconded-Token")) or \
       _token_ok(request.headers.get("Authorization", "").removeprefix("Bearer ").strip()) or \
       _token_ok(request.args.get("token")):   # SSE/EventSource can't set headers
        return None
    if request.path == "/" and request.method == "GET":
        return LOGIN_HTML.replace("__ERR__", 'style="display:block"' if request.args.get("bad") else "")
    if _wants_html() and request.method == "GET":
        return LOGIN_HTML.replace("__ERR__", "")
    return jsonify(error="unauthorized", hint="pass the access token via login cookie, "
                   "X-Seconded-Token header, or ?token="), 401


@app.route("/login", methods=["POST"])
def login():
    token = (request.form.get("token") or request.headers.get("X-Seconded-Token")
             or (request.get_json(silent=True) or {}).get("token", ""))
    if not _token_ok(token):
        if _wants_html():
            return LOGIN_HTML.replace("__ERR__", 'style="display:block"')
        return jsonify(error="bad token"), 401
    if _wants_html():
        resp = app.make_response("")
        resp.status_code = 302
        resp.headers["Location"] = "/"
    else:
        resp = jsonify(ok=True, redirect="/")
    resp.set_cookie(AUTH_COOKIE, token, httponly=True, samesite="Lax", max_age=86400 * 30)
    return resp


class Session:
    def __init__(self):
        self.buffer = deque()          # float32 samples
        self.total_samples = 0
        self.last_live_at = -1e9       # sample index last live-transcribed up to
        self.live_line = None          # (text, window_start) currently on screen
        self.committed = []            # list of dicts: start,end,text,speaker
        self.speakers = {}             # speaker_id -> color
        self.events = []               # SSE log for late joiners
        self.status = "idle"
        self.error = None
        self.meeting_dir = None      # set when final pass writes artifacts
        self.final_segments = []     # SpeakerSegment list from last final pass
        self.meta = {}               # language/duration/speakers for reports
        self.name = None             # user-chosen session name (UI)
        self.summary_template = "action"    # summary template id (see summarize.TEMPLATES)
        self.summary_length = "medium"      # summary length id (see summarize.LENGTHS)
        self.created_at = datetime.now().isoformat(timespec="seconds")
        self.lock = threading.Lock()
        self.ingest_bytes = 0        # bytes received via /ingest (diagnostics)
        self.last_ingest_at = None   # monotonic time of last ingest chunk
        self.abort_event = threading.Event()   # set by POST /cancel → stop processing
        # incremental transcription: blocks finished while the meeting runs,
        # so the post-meeting wait is only the tail + diarization + summary
        self.transcript_blocks = []  # [{start, lang, text, items}]
        self.blocks_done = 0         # samples fully transcribed
        self.worker_done = threading.Event()
        self.worker_done.set()       # set = no worker pending; cleared on start

    def push(self, pcm_bytes: bytes):
        arr = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        with self.lock:
            self.buffer.extend(arr.tolist())
            self.total_samples += len(arr)
            self.ingest_bytes += len(pcm_bytes)
            self.last_ingest_at = time.monotonic()

    def load_audio(self, arr: "np.ndarray"):
        """Load a full float32 mono array at once (file mode)."""
        with self.lock:
            self.buffer = deque(arr.tolist())
            self.total_samples = len(arr)

    def audio(self) -> np.ndarray:
        with self.lock:
            return np.array(self.buffer, dtype=np.float32) if self.buffer else np.zeros(0, np.float32)


session = Session()
sse_clients = []  # list of queues
sse_lock = threading.Lock()


def sse_broadcast(event: str, data: dict):
    payload = {"event": event, "data": data}
    with sse_lock:
        session.events.append(payload)
        if len(session.events) > 300:      # cap replay buffer (long-lived service)
            del session.events[:-300]
        for q in sse_clients:
            try:
                q.put_nowait(payload)
            except Exception:
                pass


def fmt_ts(sec: float) -> str:
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
    return f"{h:02d}:{m:02d}:{s:05.2f}"


def speaker_color(k: int) -> str:
    if k not in session.speakers:
        palette = ["#4da3ff", "#ff7a5c", "#6fd68b", "#e8c15a", "#c792ea", "#f07dc4"]
        session.speakers[k] = palette[k % len(palette)]
    return session.speakers[k]


# ---------------------------------------------------------------- live loop

def _transcribe_block(chunk: np.ndarray, sr: int):
    """Transcribe one <=45 s block with timestamps via the engine facade.
    Returns (lang, text, items)."""
    lang, text, items = engine.transcribe_with_timestamps(chunk, sr)
    return lang or "", text or "", list(items or [])


def worker_loop():
    """Incremental transcription: transcribe the audio in ~45 s blocks as it
    arrives, so the post-meeting wait is only the unprocessed tail. Each
    finished block doubles as the live subtitle (one ASR pass does both)."""
    BLOCK_S = 45.0
    last_progress = 0.0
    try:
        _worker_body(BLOCK_S, last_progress)
    finally:
        session.worker_done.set()


def _worker_body(BLOCK_S, last_progress):
    while True:
        if session.abort_event.is_set():
            break
        live = session.status == "live"
        total = session.total_samples
        done = session.blocks_done
        if total - done < int(3 * SR) and not live:
            break
        if total - done < int(BLOCK_S * SR) and live:
            time.sleep(1)
            continue
        if engine.model is None:
            if not live:
                break
            time.sleep(2)
            continue
        with session.lock:
            buf = np.array(session.buffer, dtype=np.float32)[done:]
        take = min(len(buf), int(BLOCK_S * SR))
        chunk = buf[:take]
        try:
            lang, text, items = _transcribe_block(chunk, SR)
            base = done / SR
            for it in items:
                it.start_time = round(it.start_time + base, 2)
                it.end_time = round(it.end_time + base, 2)
            session.transcript_blocks.append(
                {"start": base, "lang": lang, "text": text, "items": items})
            session.blocks_done = done + take
            if text and LIVE_MODE is not None:
                sse_broadcast("live", {"text": " ".join(text.split()),
                                       "t": round((done + take) / SR, 1)})
        except Exception as e:
            log.exception("incremental transcribe failed")
            sse_broadcast("status", {"msg": f"live ASR error: {e}"})
            time.sleep(5)
        now = time.time()
        if now - last_progress >= 30:
            last_progress = now
            sse_broadcast("status", {
                "msg": f"transcribed {session.blocks_done / SR / 60:.0f} of "
                       f"{session.total_samples / SR / 60:.0f} min"})


# ---------------------------------------------------------------- final pass

def write_outputs(meeting_dir: str, lang: str, text: str, timestamps, segments):
    os.makedirs(meeting_dir, exist_ok=True)
    # transcript.txt with speaker labels
    lines = []
    for seg in segments:
        spk = f"Speaker {seg.speaker + 1}" if seg.speaker >= 0 else "Speaker ?"
        lines.append(f"[{fmt_ts(seg.start)}] {spk}: {seg.text}")
    with open(os.path.join(meeting_dir, "transcript.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")

    # transcript.vtt (speaker in cue header)
    vtt = ["WEBVTT", ""]
    for i, seg in enumerate(segments):
        spk = f"Speaker {seg.speaker + 1}" if seg.speaker >= 0 else "Speaker ?"
        vtt += [str(i + 1), f"{fmt_ts(seg.start)} --> {fmt_ts(seg.end)}",
                f"<b>{spk}</b> {seg.text}", ""]
    with open(os.path.join(meeting_dir, "transcript.vtt"), "w") as f:
        f.write("\n".join(vtt))

    # summary.md (written by the summarizer thread)
    return os.path.join(meeting_dir, "summary.md")


def _finish_cancelled():
    """Worker reached an abort checkpoint: discard everything, go idle."""
    session.status = "idle"
    sse_broadcast("status", {"msg": "cancelled — nothing was saved"})


def final_pass():
    t0 = time.time()
    timings = {}   # stage -> seconds, recorded into meta.json for the reports
    if engine.model is None:
        sse_broadcast("status", {"msg": f"loading ASR ({engine.description})…"})
        try:
            engine.load_final()
        except Exception as e:
            log.exception("final model load failed")
            session.status = "error"
            sse_broadcast("status", {"msg": f"failed to load final ASR: {e}"})
            return
    if session.abort_event.is_set():
        _finish_cancelled()
        return
    audio = session.audio()
    if len(audio) < SR:
        session.status = "error"
        if session.ingest_bytes == 0:
            msg = ("no audio received — the browser never sent any mic data. "
                   "Check that you allowed microphone access, and use https:// (or localhost).")
        elif time.monotonic() - (session.last_ingest_at or 0) > 5:
            msg = f"only {len(audio)/SR:.1f}s of audio received — stream stopped early?"
        else:
            msg = f"too little audio ({len(audio)/SR:.1f}s) to transcribe"
        sse_broadcast("status", {"msg": msg})
        return

    # incremental worker: wait for it to drain, then transcribe the tail.
    # (With --no-live/--no-incremental there are no blocks; the tail is all of it.)
    sse_broadcast("status", {"msg": "finishing transcription…"})
    session.worker_done.wait(timeout=6 * 3600)
    done = session.blocks_done
    tail = audio[done:]
    blocks = list(session.transcript_blocks)
    if len(tail) >= int(1.0 * SR):
        sse_broadcast("status", {"msg": f"transcribing last "
                                        f"{len(tail)/SR:.0f}s…"})
        t_tail = time.time()
        lang_t, text_t, items_t = engine.transcribe_with_timestamps(tail, SR)
        items_t = list(items_t or [])
        for it in items_t:
            it.start_time = round(it.start_time + done / SR, 2)
            it.end_time = round(it.end_time + done / SR, 2)
        blocks.append({"start": done / SR, "lang": lang_t, "text": text_t,
                       "items": items_t})
        timings["tail_s"] = round(time.time() - t_tail, 1)
    langs = [b["lang"] for b in blocks if b.get("lang")]
    try:
        from qwen_asr.inference.utils import merge_languages
        lang = merge_languages(langs) if langs else ""
    except ImportError:
        lang = max(set(langs), key=langs.count) if langs else ""
    text = " ".join(b["text"] for b in blocks if b["text"])
    timestamps = [it for b in blocks for it in b["items"]]
    timings["transcription_s"] = round(time.time() - t0, 1)
    log.info("final transcript: %d chars in %.1fs (%d incremental blocks)",
             len(text), time.time() - t0, len(blocks))

    # build segments from aligner timestamps (word/char level -> merge to ~sentence lines)
    segs = []
    try:
        items = list(timestamps) if timestamps is not None else []
        for it in items:
            start = getattr(it, "start_time", None)
            end = getattr(it, "end_time", None)
            txt = str(getattr(it, "text", "")).strip()
            if start is None and isinstance(it, (tuple, list)) and len(it) >= 3:
                # fallback to tuple form
                start, end, txt = it[0], it[1], str(it[2]).strip()
            if start is None or end is None:
                continue
            segs.append(SpeakerSegment(float(start), float(end), txt))
    except Exception:
        log.exception("aligner timestamp parse failed; falling back to single segment")
        segs = []
    if not segs or sum(len(s.text) for s in segs) < len(text) // 2:
        # fallback: one big segment (no timestamps usable)
        segs = [SpeakerSegment(0.0, len(audio) / SR, text)]

    # merge tiny fragments into readable lines (~<=140 chars or ~8s)
    merged = []
    for s in segs:
        if not s.text:
            continue
        if merged and (len(merged[-1].text) + len(s.text) < 120 and
                       s.end - merged[-1].start < 9):
            merged[-1].end = s.end
            merged[-1].text += " " + s.text
        else:
            merged.append(SpeakerSegment(s.start, s.end, s.text))
    segs = merged

    # diarize (non-fatal — fall back to single speaker if it fails)
    sse_broadcast("status", {"msg": "diarizing speakers…"})
    t_diar = time.time()
    try:
        spk_segs = diarize(audio, SR, backend=DIAR_BACKEND)
        assign_speakers(spk_segs, segs)
    except Exception as e:
        log.exception("diarization failed; using single speaker")
        for s in segs:
            s.speaker = 0
    timings["diarization_s"] = round(time.time() - t_diar, 1)
    if session.abort_event.is_set():
        _finish_cancelled()
        return
    n_spk = max((s.speaker for s in segs), default=0) + 1

    # outputs
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    meeting_dir = os.path.join(OUTPUT_ROOT, f"meeting-{stamp}")
    summary_path = write_outputs(meeting_dir, lang, text, timestamps, segs)
    with open(os.path.join(meeting_dir, "audio.wav"), "wb") as f:
        sf.write(f, audio, SR, format="WAV", subtype="PCM_16")

    # remember for the download endpoints + UI
    session.meeting_dir = meeting_dir
    session.final_segments = segs
    transcript_text = "\n".join(
        f"[{fmt_ts(s.start)}] Speaker {s.speaker + 1}: {s.text}" for s in segs)
    # summary language: trust the ASR-detected language when recognized
    # (script detection can't tell Spanish from French); fall back to script
    # detection on the transcript (handles mixed-language meetings)
    from summarize import detect_dominant_language, normalize_lang
    sum_lang = normalize_lang(lang) or detect_dominant_language(transcript_text)
    session.meta = {"Language": lang, "Duration": fmt_ts(len(audio) / SR),
                    "Speakers": n_spk}

    # persist session record so history survives restarts and is downloadable anytime
    with open(os.path.join(meeting_dir, "meta.json"), "w") as f:
        json.dump({
            "name": session.name or f"Meeting {stamp}",
            "created_at": session.created_at,
            "language": lang,
            "summary_language": sum_lang,
            "duration_s": round(len(audio) / SR, 1),
            "speakers": n_spk,
            "timings": timings,
            "models": {"asr": engine.description,
                       "summary": f"{_summarize_mod.MODEL} @ {_summarize_mod.LMSTUDIO_URL}"},
            "segments": [{"start": s.start, "end": s.end, "text": s.text,
                          "speaker": s.speaker} for s in segs],
        }, f, ensure_ascii=False, indent=1)

    # final transcript to UI
    sse_broadcast("final", {
        "segments": [{"start": round(s.start, 2), "end": round(s.end, 2),
                      "text": s.text, "speaker": s.speaker} for s in segs],
        "speakers": n_spk,
        "dir": meeting_dir,
    })

    # summarize
    def do_summary():
        if session.abort_event.is_set():
            _finish_cancelled()
            return
        try:
            sse_broadcast("status", {"msg": f"summarizing with {_summarize_mod.MODEL} (this takes a while)…"})
            t_sum = time.time()
            from concurrent.futures import ThreadPoolExecutor, TimeoutError
            def _do_summarize():
                return summarize(transcript_text, language=sum_lang,
                                 template=session.summary_template, length=session.summary_length)
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_do_summarize)
                    md = future.result(timeout=_summarize_mod.LMSTUDIO_TIMEOUT)
            except TimeoutError:
                raise RuntimeError(f"summarize timed out after {_summarize_mod.LMSTUDIO_TIMEOUT}s")
            except Exception as e:
                raise RuntimeError(f"summarize failed: {e}")
            timings["summary_s"] = round(time.time() - t_sum, 1)
            # persist summary timing BEFORE writing the file so its header can include it
            try:
                with open(os.path.join(meeting_dir, "meta.json")) as f:
                    _m = json.load(f)
                _m["timings"] = timings
                with open(os.path.join(meeting_dir, "meta.json"), "w") as f:
                    json.dump(_m, f, ensure_ascii=False, indent=1)
            except Exception:
                pass
            _write_summary_file(meeting_dir, md, lang, n_spk, len(audio) / SR, stamp,
                                session.summary_template, session.summary_length)
            # auto-name: if the user never named this session, derive a short name
            # from the summary's first content line (e.g. its opening sentence).
            _maybe_auto_name(meeting_dir, md)
            session.status = "done"
            sse_broadcast("status", {"msg": f"done → {meeting_dir}"})
        except Exception as e:
            log.exception("summary failed")
            session.status = "error"
            msg = str(e)
            sse_broadcast("status", {"msg": f"summary failed: {msg} (transcript saved)"})

    threading.Thread(target=do_summary, daemon=True).start()


def _maybe_auto_name(d: str, md_body: str):
    """If the session has no user-chosen name, set one from the summary's opening text.

    Skips section headers (## …) and takes the first real content line — for
    Chinese that's the start of 摘要; truncated to a short phrase.
    """
    try:
        with open(os.path.join(d, "meta.json")) as f:
            meta = json.load(f)
        if not (meta.get("name") or "").startswith("Meeting 20"):
            return  # user already named it
        for ln in md_body.splitlines():
            t = ln.strip()
            if not t or t.startswith("#") or t.startswith("- "):
                continue
            name = re.sub(r"\s+", " ", t)
            # cut at the first sentence boundary so the name stays one short phrase
            for sep in ("。", ". ", "；", "; "):
                i = name.find(sep, 12)
                if i >= 0:
                    name = name[:i + (1 if sep == "。" else 0)]
                    break
            meta["name"] = name[:80]
            with open(os.path.join(d, "meta.json"), "w") as f:
                json.dump(meta, f, ensure_ascii=False, indent=1)
            log.info("auto-named session %r", meta["name"])
            return
    except Exception:
        pass  # naming is cosmetic — never fail the pipeline for it


def _write_summary_file(d, md_body, lang, n_spk, dur_s, stamp, template, length):
    """Write summary.md (header + body) and record the settings in meta.json."""
    from summarize import TEMPLATES
    tpl = TEMPLATES.get(template) or TEMPLATES["default"]
    header = (f"# Meeting summary — {stamp}\n\n"
              f"Language: {lang} · Template: {tpl['label_en']}\n"
              f"Duration: {fmt_ts(dur_s)}\nSpeakers: {n_spk}\n")
    # processing-time + model notes (from meta.json, if present)
    try:
        with open(os.path.join(d, "meta.json")) as f:
            m = json.load(f)
        t = m.get("timings") or {}
        bits = []
        if t.get("transcription_s"):
            bits.append(f"transcription {_fmt_secs(t['transcription_s'])}")
        if t.get("diarization_s"):
            bits.append(f"speaker separation {_fmt_secs(t['diarization_s'])}")
        if t.get("summary_s"):
            bits.append(f"summary {_fmt_secs(t['summary_s'])}")
        if bits:
            header += f"Processing: {' · '.join(bits)}\n"
        models = m.get("models") or {}
        if models.get("asr"):
            header += f"Transcribed by: {models['asr']}\n"
        if models.get("summary"):
            header += f"Summarized by: {models['summary']}\n"
    except Exception:
        pass
    with open(os.path.join(d, "summary.md"), "w") as f:
        f.write(f"{header}\n{md_body}\n")
    meta_path = os.path.join(d, "meta.json")
    try:
        with open(meta_path) as f:
            meta = json.load(f)
        meta["template"] = template
        meta["length"] = length
        with open(meta_path, "w") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _load_transcript_text(d):
    """Rebuild the transcript text from a session dir's meta.json segments."""
    try:
        with open(os.path.join(d, "meta.json")) as f:
            meta = json.load(f)
        return "\n".join(
            f"[{fmt_ts(s['start'])}] Speaker {s['speaker'] + 1}: {s['text']}"
            for s in meta.get("segments", []))
    except Exception:
        return ""


def _regenerate(d, template=None, length=None):
    """Re-run the summary for a session dir (current or history) with new settings."""
    if REGEN_LOCK.locked():
        return False
    with REGEN_LOCK:
        def work():
            try:
                sse_broadcast("status", {"msg": "regenerating summary…"})
                text = _load_transcript_text(d)
                if not text.strip():
                    raise RuntimeError("no transcript found for this session")
                try:
                    with open(os.path.join(d, "meta.json")) as f:
                        meta_lang = json.load(f).get("language")
                except Exception:
                    meta_lang = None
                from summarize import detect_dominant_language, normalize_lang
                lang = normalize_lang(meta_lang) or detect_dominant_language(text)
                t0 = time.time()
                md = summarize(text, language=lang, template=template or "default",
                               length=length or "medium")
                stamp = os.path.basename(d).replace("meeting-", "")
                try:
                    with open(os.path.join(d, "meta.json")) as f:
                        meta = json.load(f)
                    n_spk = meta.get("speakers", 1)
                    dur_s = meta.get("duration_s", 0.0)
                    timings = meta.setdefault("timings", {})
                    timings["summary_s"] = round(time.time() - t0, 1)
                    with open(os.path.join(d, "meta.json"), "w") as f:
                        json.dump(meta, f, ensure_ascii=False, indent=1)
                except Exception:
                    n_spk, dur_s = 1, 0.0
                _write_summary_file(d, md, lang, n_spk, dur_s, stamp,
                                    template or "default", length or "medium")
                # a successful regeneration clears any stale error state
                with session.lock:
                    if session.status == "error":
                        session.status = "done"
                sse_broadcast("status", {"msg": "summary regenerated"})
            except Exception as e:
                log.exception("regenerate failed")
                with session.lock:
                    session.status = "error"
                sse_broadcast("status", {"msg": f"regeneration failed: {e}"})

        threading.Thread(target=work, daemon=True).start()
    return True


REGEN_LOCK = threading.Lock()


# ---------------------------------------------------------------- routes

@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/state")
def api_state():
    with session.lock:
        dur = session.total_samples / SR
        tpl, ln = session.summary_template, session.summary_length
    return jsonify({"status": session.status, "duration": round(dur, 1),
                    "speakers": len(session.speakers), "template": tpl, "length": ln,
                    "models": {"asr": engine.description,
                               "summary": _summarize_mod.MODEL,
                               "summary_url": _summarize_mod.LMSTUDIO_URL}})


@app.route("/start", methods=["POST"])
def start():
    if session.status == "live":
        return jsonify({"error": "already live"}), 409
    session.__init__()
    session.status = "loading"
    sse_broadcast("status", {"msg": "starting…"})

    def boot():
        try:
            # the incremental worker does live subtitles AND pre-transcribes
            # blocks so the post-meeting wait shrinks to tail + diarize + summary
            if LIVE_MODE == "final" or INCREMENTAL:
                if engine.model is None:
                    sse_broadcast("status", {"msg": f"loading ASR ({engine.description})…"})
                    engine.load_final()
            # LIVE_MODE == None → no live subtitles, just buffer audio
            session.status = "live"
            sse_broadcast("status", {"msg": "listening — stream audio to /ingest"})
            if LIVE_MODE is not None or INCREMENTAL:
                session.worker_done.clear()
                threading.Thread(target=worker_loop, daemon=True).start()
        except Exception as e:
            log.exception("boot failed")
            session.status = "error"
            sse_broadcast("status", {"msg": f"startup error: {e}"})

    threading.Thread(target=boot, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/ingest", methods=["POST"])
def ingest():
    if session.status != "live":
        return jsonify({"error": f"not live (status={session.status})"}), 409
    pcm = request.get_data(cache=False)
    if not pcm:
        return jsonify({"error": "empty body"}), 400
    session.push(pcm)
    return "", 204


@app.route("/rootca")
def rootca():
    """Serve the local CA certificate (mkcert) so clients can trust this server.

    One-time per device: download this file and install it into your OS/browser
    trusted store — after that, https://<ip>:8766 shows no security warning.
    """
    cert_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")
    ca_path = os.path.expanduser("~/.local/share/mkcert/rootCA.pem")
    if not (os.path.exists(ca_path) and os.access(ca_path, os.R_OK)):
        return ("This server's certificate is self-signed; there is no local CA to install. "
                "Accept the browser warning once per device instead.", 404,
                {"Content-Type": "text/plain"})
    with open(ca_path, "rb") as f:
        data = f.read()
    return Response(data, mimetype="application/x-pem-file",
                    headers={"Content-Disposition": 'attachment; filename="seconded-rootCA.pem"'})


@app.route("/end", methods=["POST"])
def end():
    if session.status != "live":
        return jsonify({"error": f"not live (status={session.status})"}), 409
    session.status = "finalizing"
    sse_broadcast("status", {"msg": "meeting ended — processing…"})
    threading.Thread(target=final_pass, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/templates")
def api_templates():
    """Summary template + length options for the UI (hidden ones excluded)."""
    from summarize import TEMPLATES, LENGTHS
    return jsonify({
        "templates": [{"id": k, "label": v["label_en"]}
                      for k, v in TEMPLATES.items() if not v.get("hidden")],
        "lengths":   [{"id": k, "label": v["label_en"]} for k, v in LENGTHS.items()],
        "defaults":  {"template": "action",
                      "length": next((k for k, v in LENGTHS.items() if v.get("default")),
                                     "medium")},
    })


@app.route("/api/settings", methods=["POST"])
def api_settings():
    """Save summary template/length for the current session (applied on next run/regen)."""
    global session
    data = request.get_json(silent=True) or {}
    from summarize import TEMPLATES, LENGTHS
    with session.lock:
        if "template" in data and data["template"] in TEMPLATES:
            session.summary_template = data["template"]
        if "length" in data and data["length"] in LENGTHS:
            session.summary_length = data["length"]
        return jsonify({"ok": True, "template": session.summary_template,
                        "length": session.summary_length})


@app.route("/regenerate", methods=["POST"])
def regenerate_current():
    """Re-run the summary for the current session with (possibly new) settings."""
    data = request.get_json(silent=True) or {}
    from summarize import TEMPLATES, LENGTHS
    if "template" in data and data["template"] in TEMPLATES:
        session.summary_template = data["template"]
    if "length" in data and data["length"] in LENGTHS:
        session.summary_length = data["length"]
    d = session.meeting_dir
    if not d or not os.path.isdir(d):
        return jsonify({"ok": False, "error": "no finished session to regenerate"}), 409
    ok = _regenerate(d, template=session.summary_template, length=session.summary_length)
    return (jsonify({"ok": True}), 202 if ok else
            (jsonify({"ok": False, "error": "another regeneration is already running"}), 409))


@app.route("/reset", methods=["POST"])
def reset():
    """Abandon the current session and go back to idle (no final pass)."""
    if session.status == "finalizing":
        return jsonify({"error": "a final pass is running — wait for it or use End"}), 409
    if session.status in ("live", "loading"):
        # flip status first so ingest/worker_loop stop touching it, then clear
        session.status = "idle"
    session.__init__()
    sse_broadcast("status", {"msg": "reset — ready for a new session"})
    return jsonify({"ok": True})


@app.route("/cancel", methods=["POST"])
def cancel():
    """Force-stop the current processing (final pass / upload pipeline).

    The worker notices the flag at its next checkpoint and stops without
    writing artifacts. Safe to call from any state; no-op when idle.
    """
    if session.status in ("finalizing", "loading"):
        session.abort_event.set()
        sse_broadcast("status", {"msg": "cancelling — stopping processing…"})
        return jsonify({"ok": True})
    if session.status == "live":
        # end the capture and discard everything (no final pass)
        session.__init__()
        sse_broadcast("status", {"msg": "cancelled — nothing was processed"})
        return jsonify({"ok": True})
    return jsonify({"error": f"nothing to cancel (status={session.status})"}), 409


# ---------------------------------------------------------------- upload

def extract_audio(src_path: str) -> str:
    """Extract mono 16kHz WAV from any audio/video container via ffmpeg."""
    out = tempfile.mktemp(suffix=".wav", prefix="ma_upload_")
    cmd = ["ffmpeg", "-y", "-i", src_path, "-vn", "-ac", "1", "-ar", str(SR),
           "-c:a", "pcm_s16le", out]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"ffmpeg failed: {r.stderr.strip().splitlines()[-1] if r.stderr else 'unknown'}")
    return out


@app.route("/upload", methods=["POST"])
def upload():
    """Upload an audio/video file; run the full pipeline on it."""
    if session.status in ("live", "finalizing"):
        return jsonify({"error": f"busy (status={session.status})"}), 409
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "no file field"}), 400
    name = os.path.basename(f.filename)
    tmp_in = tempfile.mktemp(suffix=os.path.splitext(name)[1], prefix="ma_up_")
    f.save(tmp_in)

    def work():
        try:
            sse_broadcast("status", {"msg": f"upload: extracting audio from {name}…"})
            wav_path = extract_audio(tmp_in)
            if session.abort_event.is_set():
                _finish_cancelled()
                return
            # long files: sparse live lines (every 60s of audio) so we don't double the work
            run_file(wav_path, source_name=name, live_step_s=60)
        except Exception as e:
            log.exception("upload pipeline failed")
            session.status = "error"
            sse_broadcast("status", {"msg": f"upload failed: {e}"})
        finally:
            for p in (tmp_in,):
                try: os.unlink(p)
                except OSError: pass

    threading.Thread(target=work, daemon=True).start()
    return jsonify({"ok": True, "name": name})


# ---------------------------------------------------------------- history & downloads

def _load_session_dir(d):
    """Load a persisted session dir into (segments, meta). Returns None if unusable."""
    mp = os.path.join(d, "meta.json")
    if not os.path.exists(mp):
        return None
    try:
        with open(mp) as f:
            m = json.load(f)
        segs = [SpeakerSegment(s["start"], s["end"], s["text"]) for s in m.get("segments", [])]
        # re-attach speaker ids (stored per segment)
        for i, s in enumerate(segs):
            s.speaker = m["segments"][i].get("speaker", 0)
        meta = {"Language": m.get("language"), "Duration": fmt_ts(m.get("duration_s", 0)),
                "Speakers": m.get("speakers")}
        return segs, meta, m
    except Exception:
        log.exception("failed to load session %s", d)
        return None


def _list_history():
    """All completed sessions (dirs with meta.json), newest first."""
    out = []
    if not os.path.isdir(OUTPUT_ROOT):
        return out
    for entry in sorted(os.listdir(OUTPUT_ROOT), reverse=True):
        d = os.path.join(OUTPUT_ROOT, entry)
        mp = os.path.join(d, "meta.json")
        if not (os.path.isdir(d) and os.path.exists(mp)):
            continue
        try:
            with open(mp) as f:
                m = json.load(f)
        except Exception:
            continue
        out.append({
            "dir": entry,
            "name": m.get("name", entry),
            "created_at": m.get("created_at"),
            "language": m.get("language"),
            "duration_s": m.get("duration_s"),
            "speakers": m.get("speakers"),
            "template": m.get("template", "default"),
            "length": m.get("length", "medium"),
            "has_summary": os.path.exists(os.path.join(d, "summary.md")),
        })
    return out


@app.route("/api/history")
def api_history():
    hist = _list_history()
    # mark the live/current session if it's done and on disk
    for h in hist:
        h["current"] = (session.meeting_dir and os.path.basename(session.meeting_dir) == h["dir"])
    return jsonify({"sessions": hist})


@app.route("/api/session/name", methods=["POST"])
def api_set_name_current():
    """Rename the CURRENT (in-memory) session — body: {"name": ...}."""
    body = request.get_json(silent=True) or {}
    if "name" in body:
        session.name = str(body["name"]).strip()[:120] or None
    return jsonify({"ok": True, "name": session.name})


@app.route("/api/session/<name>", methods=["POST"])
def api_set_name(name):
    """Rename the current session (used by the UI before/after it finishes)."""
    body = request.get_json(silent=True) or {}
    if "name" in body:
        session.name = str(body["name"]).strip()[:120] or None
    return jsonify({"ok": True, "name": session.name})


@app.route("/history/<dir>/rename", methods=["POST"])
def history_rename(dir):
    """Rename a past session (stored in its meta.json)."""
    d = os.path.join(OUTPUT_ROOT, os.path.basename(dir))
    mp = os.path.join(d, "meta.json")
    if not os.path.isdir(d) or not os.path.exists(mp):
        return jsonify({"error": "session not found"}), 404
    body = request.get_json(silent=True) or {}
    new_name = str(body.get("name", "")).strip()[:120]
    if not new_name:
        return jsonify({"error": "empty name"}), 400
    try:
        with open(mp) as f:
            meta = json.load(f)
        meta["name"] = new_name
        with open(mp, "w") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "name": new_name})


@app.route("/history/<dir>", methods=["DELETE"])
def history_delete(dir):
    """Delete a past session and all its files (transcript, summary, audio)."""
    d = os.path.join(OUTPUT_ROOT, os.path.basename(dir))
    if not os.path.isdir(d) or not os.path.exists(os.path.join(d, "meta.json")):
        return jsonify({"error": "session not found"}), 404
    # never delete the session that is currently being processed/viewed as current
    if session.meeting_dir and os.path.realpath(session.meeting_dir) == os.path.realpath(d):
        return jsonify({"error": "this is the active session — end or cancel it first"}), 409
    shutil.rmtree(d, ignore_errors=True)
    log.info("deleted session %s", dir)
    return jsonify({"ok": True})


def _read_summary(d):
    p = os.path.join(d, "summary.md")
    if os.path.exists(p):
        with open(p) as f:
            return f.read()
    return ""


def _fmt_secs(s):
    """12.3 -> '12 s'; 75 -> '1 min 15 s'."""
    try:
        s = float(s)
    except (TypeError, ValueError):
        return None
    if s < 60:
        return f"{s:.0f} s" if s >= 10 else f"{s:.1f} s"
    m, r = divmod(int(round(s)), 60)
    return f"{m} min {r} s"


def _report_meta(d):
    """Display meta for reports: language/duration/speakers + processing time + models.

    Read fresh from the session's meta.json so timing/model notes are always
    current (summary_s is added after the transcript already exists).
    """
    try:
        with open(os.path.join(d, "meta.json")) as f:
            m = json.load(f)
    except Exception:
        return {}
    out = {
        "Language": m.get("language"),
        "Duration": fmt_ts(m.get("duration_s", 0)),
        "Speakers": m.get("speakers"),
    }
    t = m.get("timings") or {}
    bits = []
    if t.get("transcription_s"):
        bits.append(f"transcription {_fmt_secs(t['transcription_s'])}")
    if t.get("diarization_s"):
        bits.append(f"speaker separation {_fmt_secs(t['diarization_s'])}")
    if t.get("summary_s"):
        bits.append(f"summary {_fmt_secs(t['summary_s'])}")
    if bits:
        out["Processing"] = " · ".join(bits) + f"  (audio {out['Duration']})"
    models = m.get("models") or {}
    if models.get("asr"):
        out["Transcribed by"] = models["asr"]
    if models.get("summary"):
        out["Summarized by"] = models["summary"]
    return {k: v for k, v in out.items() if v}


@app.route("/download/<kind>")
def download(kind):
    """Download artifacts for the CURRENT session.

    kinds: transcript, summary, combined  (each with ?format=md|pdf)
           vtt, audio.wav                 (fixed formats)
    """
    if not session.meeting_dir or not session.final_segments:
        return jsonify({"error": "no completed meeting yet"}), 404
    d = session.meeting_dir
    segs = session.final_segments
    meta = _report_meta(d)
    summary = _read_summary(d)
    fmt = request.args.get("format", "md").lower()

    if kind in ("transcript", "summary", "combined"):
        return _render_doc(kind, segs, summary, meta, d, fmt, base_name="meeting")
    if kind == "vtt":
        p = os.path.join(d, "transcript.vtt")
        if not os.path.exists(p):
            return jsonify({"error": "vtt not ready"}), 404
        with open(p) as f:
            return Response(f.read(), mimetype="text/vtt; charset=utf-8",
                            headers={"Content-Disposition": 'attachment; filename="transcript.vtt"'})
    if kind == "audio.wav":
        p = os.path.join(d, "audio.wav")
        if not os.path.exists(p):
            return jsonify({"error": "audio not ready"}), 404
        with open(p, "rb") as f:
            data = f.read()
        return Response(data, mimetype="audio/wav",
                        headers={"Content-Disposition": 'attachment; filename="meeting-audio.wav"'})
    return jsonify({"error": f"unknown kind {kind}"}), 404


def _render_doc(kind, segs, summary, meta, d, fmt, base_name):
    """Render transcript/summary/combined as Markdown or PDF."""
    if fmt == "pdf":
        if kind == "transcript":
            html_str = report.transcript_html(segs, meta)
        elif kind == "summary":
            if not summary:
                return jsonify({"error": "summary not ready yet"}), 409
            html_str = report.summary_html(summary, meta)
        else:
            html_str = report.combined_html(segs, summary, meta)
        pdf_path = os.path.join(d, f"{kind}.pdf")
        try:
            report.render_pdf(html_str, pdf_path)
        except Exception as e:
            log.exception("PDF render failed (%s)", kind)
            return jsonify({"error": f"pdf render failed: {e}"}), 500
        with open(pdf_path, "rb") as f:
            data = f.read()
        fn = f"{base_name}-{kind}.pdf"
        return Response(data, mimetype="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{fn}"'})

    # markdown (default)
    if kind == "transcript":
        body = report.transcript_markdown(segs, meta)
        fn = f"{base_name}-transcript.md"
    elif kind == "summary":
        if not summary:
            return jsonify({"error": "summary not ready yet"}), 409
        body = summary
        fn = f"{base_name}-summary.md"
    else:
        body = report.combined_markdown(segs, summary, meta)
        fn = f"{base_name}-notes.md"
    return Response(body, mimetype="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@app.route("/history/<dir>/download/<kind>")
def history_download(dir, kind):
    """Download artifacts for a PAST session by its directory name.

    kinds: transcript, summary, combined (each with ?format=md|pdf), vtt, audio.wav
    """
    d = os.path.join(OUTPUT_ROOT, os.path.basename(dir))
    if not os.path.isdir(d) or not os.path.exists(os.path.join(d, "meta.json")):
        return jsonify({"error": "session not found"}), 404
    loaded = _load_session_dir(d)
    if not loaded:
        return jsonify({"error": "could not load session"}), 500
    segs, _, m = loaded
    meta = _report_meta(d)
    summary = _read_summary(d)
    fmt = request.args.get("format", "md").lower()
    base_name = (m.get("name") or "meeting").replace("/", "-")

    if kind in ("transcript", "summary", "combined"):
        return _render_doc(kind, segs, summary, meta, d, fmt, base_name=base_name)
    if kind == "vtt":
        p = os.path.join(d, "transcript.vtt")
        if not os.path.exists(p):
            return jsonify({"error": "no vtt"}), 404
        with open(p) as f:
            return Response(f.read(), mimetype="text/vtt; charset=utf-8",
                            headers={"Content-Disposition": 'attachment; filename="transcript.vtt"'})
    if kind == "audio.wav":
        p = os.path.join(d, "audio.wav")
        if not os.path.exists(p):
            return jsonify({"error": "no audio"}), 404
        with open(p, "rb") as f:
            data = f.read()
        return Response(data, mimetype="audio/wav",
                        headers={"Content-Disposition": 'attachment; filename="meeting-audio.wav"'})
    return jsonify({"error": f"unknown kind {kind}"}), 404


@app.route("/history/<dir>/regenerate", methods=["POST"])
def history_regenerate(dir):
    """Re-run the summary for a PAST session with (possibly new) settings."""
    data = request.get_json(silent=True) or {}
    from summarize import TEMPLATES, LENGTHS
    template = data.get("template") if data.get("template") in TEMPLATES else "default"
    length = data.get("length") if data.get("length") in LENGTHS else "medium"
    d = os.path.join(OUTPUT_ROOT, os.path.basename(dir))
    if not os.path.isdir(d) or not os.path.exists(os.path.join(d, "meta.json")):
        return jsonify({"error": "session not found"}), 404
    ok = _regenerate(d, template=template, length=length)
    return (jsonify({"ok": True}), 202 if ok else
            (jsonify({"ok": False, "error": "another regeneration is already running"}), 409))


@app.route("/history/<dir>/rediarize", methods=["POST"])
def history_rediarize(dir):
    """Re-run speaker diarization on a PAST session from its saved audio,
    then regenerate the summary so it sees the new speaker labels."""
    d = os.path.join(OUTPUT_ROOT, os.path.basename(dir))
    if not os.path.isdir(d) or not os.path.exists(os.path.join(d, "meta.json")):
        return jsonify({"error": "session not found"}), 404
    wav = os.path.join(d, "audio.wav")
    if not os.path.exists(wav):
        return jsonify({"error": "no saved audio for this session"}), 404
    if REGEN_LOCK.locked():
        return jsonify({"ok": False, "error": "another job is already running"}), 409
    with REGEN_LOCK:
        def work():
            try:
                sse_broadcast("status", {"msg": "re-running speaker detection…"})
                audio, _sr = sf.read(wav, dtype="float32")
                if audio.ndim > 1:
                    audio = audio.mean(axis=1)
                t0 = time.time()
                spk_segs = diarize(audio, SR, backend=DIAR_BACKEND)
                with open(os.path.join(d, "meta.json")) as f:
                    meta = json.load(f)
                segs = [SpeakerSegment(s["start"], s["end"], s["text"])
                        for s in meta.get("segments", [])]
                assign_speakers(spk_segs, segs)
                n_spk = max((s.speaker for s in segs), default=0) + 1
                meta["speakers"] = n_spk
                meta["segments"] = [{"start": s.start, "end": s.end, "text": s.text,
                                     "speaker": s.speaker} for s in segs]
                meta.setdefault("timings", {})["diarization_s"] = round(time.time() - t0, 1)
                with open(os.path.join(d, "meta.json"), "w") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=1)
                write_outputs(d, meta.get("language", ""), "", None, segs)
                sse_broadcast("status", {"msg": f"speaker detection done: {n_spk} speakers"})
            except Exception as e:
                log.exception("re-diarize failed")
                with session.lock:
                    session.status = "error"
                sse_broadcast("status", {"msg": f"speaker detection failed: {e}"})
                return
            # summary needs the new labels — regenerate it too
            _regenerate(d, template=meta.get("template"), length=meta.get("length"))
        threading.Thread(target=work, daemon=True).start()
    return jsonify({"ok": True}), 202


@app.route("/history/<dir>/view")
def history_view(dir):
    """Return a past session's transcript as JSON for the UI to render."""
    d = os.path.join(OUTPUT_ROOT, os.path.basename(dir))
    loaded = _load_session_dir(d) if os.path.isdir(d) else None
    if not loaded:
        return jsonify({"error": "session not found"}), 404
    segs, _, m = loaded
    summary = _read_summary(d)
    return jsonify({
        "name": m.get("name"), "created_at": m.get("created_at"),
        "language": m.get("language"), "duration_s": m.get("duration_s"),
        "speakers": m.get("speakers"),
        "template": m.get("template", "default"),
        "length": m.get("length", "medium"),
        "timings": m.get("timings") or {},
        "models": m.get("models") or {},
        "segments": [{"start": s.start, "end": s.end, "text": s.text,
                      "speaker": s.speaker} for s in segs],
        "summary": summary,
    })


@app.route("/events")
def events():
    def gen(q):
        with sse_lock:
            for p in session.events:  # replay history to late joiners
                yield f"event: {p['event']}\ndata: {json.dumps(p['data'])}\n\n"
        while True:
            try:
                p = q.get(timeout=15)
                yield f"event: {p['event']}\ndata: {json.dumps(p['data'])}\n\n"
            except Exception:
                yield ": keepalive\n\n"

    import queue
    q = queue.Queue()
    with sse_lock:
        sse_clients.append(q)

    def gen_guarded(q=q):
        try:
            yield from gen(q)
        finally:
            with sse_lock:
                if q in sse_clients:
                    sse_clients.remove(q)

    return Response(gen_guarded(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- file mode

def run_file(path: str, source_name: str = None, live_step_s: int = 8):
    """Full pipeline on a local audio file (test / no-Mac path).

    live_step_s: how often to emit simulated live lines while processing.
    Small for short demo files; large for long uploads so we don't double the work.
    """
    log.info("file mode: %s", path)
    data, sr = sf.read(path, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != SR:
        import scipy.signal as sig
        n_out = int(len(data) * SR / sr)
        data = sig.resample_poly(data, SR, sr).astype(np.float32)[:n_out]

    session.__init__()
    session.load_audio(data)
    # audio is fully in memory now; drop temp files (uploads) immediately
    if path.startswith(tempfile.gettempdir()):
        try: os.unlink(path)
        except OSError: pass
    label = source_name or os.path.basename(path)
    session.status = "live"
    sse_broadcast("status", {"msg": f"{label}: {len(data)/SR:.1f}s of audio loaded"})
    if (LIVE_MODE == "final" or INCREMENTAL) and engine.model is None:
        engine.load_final()
    if INCREMENTAL:
        # real incremental pass: every block is transcribed once, live events
        # come from the same pass, final_pass only waits for the last blocks
        session.worker_done.clear()
        threading.Thread(target=worker_loop, daemon=True).start()
    elif LIVE_MODE is not None:
        # simulate live subtitles for the demo, then final pass
        step = int(live_step_s * SR)
        for i in range(0, len(data), step):
            if session.abort_event.is_set():
                _finish_cancelled()
                return
            chunk = data[max(0, i - LIVE_WINDOW_S * SR):i + SR]
            lang, text = engine.transcribe(chunk, SR)
            sse_broadcast("live", {"text": " ".join(text.split()) or "(listening…)",
                                   "t": round(i / SR, 1)})
            time.sleep(0.2)
    session.status = "finalizing"
    final_pass()
    # wait for the summary thread to finish so `--file` runs to completion
    for _ in range(1200):  # up to ~30 min for a long meeting + slow LLM
        if session.status in ("done", "error"):
            break
        time.sleep(1.5)
    log.info("file mode finished: status=%s", session.status)


# ---------------------------------------------------------------- main

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="run pipeline on a local audio file instead of streaming")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--https-port", type=int, default=None,
                    help="also serve HTTPS on this port (self-signed cert; needed for "
                         "browser mic capture from other devices)")
    ap.add_argument("--no-live", action="store_true",
                    help="skip live subtitles entirely; only run the final pass at end")
    ap.add_argument("--no-incremental", action="store_true",
                    help="disable incremental (during-meeting) transcription; transcribe "
                         "everything after the meeting ends instead (slower overall)")
    ap.add_argument("--diar-backend", choices=["ecapa", "resemblyzer"],
                    default=os.environ.get("DIAR_BACKEND", "ecapa"),
                    help="speaker diarization backend (default: ecapa; env DIAR_BACKEND). "
                         "resemblyzer is lighter but English-centric and weak on other languages.")
    ap.add_argument("--asr-backend", choices=sorted(set(BACKENDS)),
                    default=os.environ.get("ASR_BACKEND", "qwen"),
                    help="ASR backend (default: qwen; env ASR_BACKEND)")
    ap.add_argument("--asr-model", default=os.environ.get("ASR_MODEL"),
                    help="ASR model id (HF repo or local dir; default per backend)")
    ap.add_argument("--asr-aligner", default=os.environ.get("ASR_ALIGNER"),
                    help="qwen backend forced aligner id, or 'none' to disable timestamps")
    ap.add_argument("--asr-device", default=os.environ.get("ASR_DEVICE", "cpu"),
                    help="ASR device: cpu (default) or cuda")
    ap.add_argument("--summary-url",
                    default=os.environ.get("SUMMARY_API_URL"),
                    help="OpenAI-compatible /v1/chat/completions URL for summaries")
    ap.add_argument("--summary-model",
                    default=os.environ.get("SUMMARY_MODEL"),
                    help="model name the summary backend expects")
    ap.add_argument("--auth-token",
                    default=os.environ.get("SECONDED_TOKEN") or None,
                    help="require this access token for every request (env SECONDED_TOKEN). "
                         "Unset = no authentication (default; fine on a trusted LAN).")
    args = ap.parse_args()

    DIAR_BACKEND = args.diar_backend  # noqa: PLW0603 — module-level main block

    if args.auth_token:
        AUTH_TOKEN = args.auth_token  # noqa: PLW0603 — module-level main block
        log.info("AUTH ENABLED — all requests need the access token "
                 "(login page at /, cookie persists 30 days)")
    else:
        log.info("auth disabled — anyone who can reach these ports has full access "
                 "(set --auth-token / SECONDED_TOKEN if others can reach this network)")

    if args.no_live:
        LIVE_MODE = None
        INCREMENTAL = False   # --no-live means: no CPU work during the meeting
    if args.no_incremental:
        INCREMENTAL = False

    engine.configure(backend=args.asr_backend, model=args.asr_model,
                     aligner=args.asr_aligner, device=args.asr_device)
    _summarize_mod.configure(url=args.summary_url, model=args.summary_model)
    log.info("ASR: %s", engine.description)
    log.info("Summary: %s @ %s", _summarize_mod.MODEL, _summarize_mod.LMSTUDIO_URL)

    if args.file:
        # run_file loads whichever models LIVE_MODE needs; UI watches progress via SSE
        threading.Thread(target=lambda: (run_file(args.file)), daemon=True).start()

    ssl_ctx = None
    if args.https_port:
        import shutil as _sh
        cert_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")
        os.makedirs(cert_dir, exist_ok=True)
        crt, key = os.path.join(cert_dir, "cert.pem"), os.path.join(cert_dir, "key.pem")
        if not (os.path.exists(crt) and os.path.exists(key)):
            host_ip = socket.gethostbyname(socket.gethostname())
            names = [host_ip, "localhost", "127.0.0.1"]
            # prefer mkcert: signed by a local CA clients can trust (no scary warnings)
            mkcert_bin = _sh.which("mkcert") or os.path.expanduser("~/.local/bin/mkcert")
            used_mkcert = False
            if os.path.exists(mkcert_bin):
                r = subprocess.run([mkcert_bin] + names, cwd=cert_dir,
                                   capture_output=True, text=True)
                if r.returncode == 0:
                    pems = [f for f in os.listdir(cert_dir)
                            if f.endswith(".pem") and not f.endswith("-key.pem")]
                    if pems:
                        newest = max(pems, key=lambda f: os.path.getmtime(os.path.join(cert_dir, f)))
                        cand_crt, cand_key = os.path.join(cert_dir, newest), \
                                             os.path.join(cert_dir, newest[:-4] + "-key.pem")
                        if os.path.exists(cand_key):
                            shutil.copyfile(cand_crt, crt)
                            shutil.copyfile(cand_key, key)
                            used_mkcert = True
            if not used_mkcert:
                r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                                    "-keyout", key, "-out", crt, "-days", "3650",
                                    "-subj", "/CN=seconded",
                                    "-addext", f"subjectAltName=DNS:localhost,IP:{host_ip},IP:127.0.0.1"],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    raise SystemExit(f"failed to generate self-signed cert: {r.stderr}")
            log.info("generated %s (valid 10 years)", "mkcert certificate (locally-trusted CA)"
                     if used_mkcert else "self-signed cert")
        ssl_ctx = (crt, key)
        log.info("HTTPS on port %d — clients can install the local CA via /rootca to skip warnings", args.https_port)

    if ssl_ctx:
        # serve BOTH plain HTTP and HTTPS concurrently
        from werkzeug.serving import make_server
        http_srv = make_server("0.0.0.0", args.port, app, threaded=True)
        https_srv = make_server("0.0.0.0", args.https_port, app, threaded=True, ssl_context=ssl_ctx)
        threading.Thread(target=http_srv.serve_forever, daemon=True).start()
        log.info("HTTP on port %d (uploads/downloads work here too)", args.port)
        https_srv.serve_forever()
    else:
        app.run(host="0.0.0.0", port=args.port, threaded=True)
