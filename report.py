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

"""Render meeting artifacts as Markdown and PDF.

PDF via weasyprint using system Noto Sans/Serif CJK fonts, so Chinese/Japanese
transcripts render correctly (not just English).
"""
import html
import os
import re


def _fmt_ts(sec: float) -> str:
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
    return f"{h:02d}:{m:02d}:{s:05.2f}"


def transcript_markdown(segments, meta=None) -> str:
    """segments: list of SpeakerSegment (start,end,text,speaker)."""
    out = []
    if meta:
        for k, v in meta.items():
            if v:
                out.append(f"**{k}:** {v}")
        out.append("")
    out.append("# Transcript\n")
    cur_spk = None
    for seg in segments:
        spk = f"Speaker {seg.speaker + 1}" if seg.speaker >= 0 else "Speaker ?"
        if spk != cur_spk:
            out.append(f"\n### {spk}\n")
            cur_spk = spk
        out.append(f"- `[{_fmt_ts(seg.start)}]` {seg.text}")
    return "\n".join(out) + "\n"


def combined_markdown(segments, summary_md: str, meta=None) -> str:
    parts = [transcript_markdown(segments, meta)]
    if summary_md and summary_md.strip():
        # strip the summarizer's own H1 header + its repeated meta lines
        body = re.sub(r"^#.*\n+", "", summary_md).strip()
        body = re.sub(r"^(Language|Duration|Speakers):\s.*(\n|$)", "", body, flags=re.M)
        parts.append("\n---\n\n# Summary\n\n" + body.strip() + "\n")
    return "\n".join(parts)


_CSS = """
@page { size: A4; margin: 18mm 16mm; }
body { font-family: "Noto Sans CJK SC", "Noto Sans", sans-serif;
       font-size: 10.5pt; line-height: 1.55; color: #1a2027; }
h1 { font-size: 16pt; border-bottom: 2px solid #4da3ff; padding-bottom: 4px; }
h2, h3 { font-size: 12pt; margin-top: 18px; color: #2b3d5e; }
.meta { color: #5a6675; font-size: 9.5pt; margin-bottom: 14px; }
ul.transcript { list-style: none; padding-left: 0; }
ul.transcript li { margin: 2px 0 8px 0; }
.ts { color: #6a7686; font-family: monospace; font-size: 9pt; margin-right: 6px; }
.spk { font-weight: bold; }
hr { border: none; border-top: 1px solid #cfd6df; margin: 22px 0; }
"""


def _segments_html(segments) -> str:
    rows = []
    cur_spk = None
    for seg in segments:
        spk = f"Speaker {seg.speaker + 1}" if seg.speaker >= 0 else "Speaker ?"
        color = ["#2b6cb0", "#c05621", "#2f855a", "#975a16", "#6b46c1"][
            (seg.speaker % 5) if seg.speaker >= 0 else 0]
        spk_html = f'<span class="spk" style="color:{color}">{html.escape(spk)}:</span> ' \
            if spk != cur_spk else ""
        rows.append(
            f'<li><span class="ts">[{_fmt_ts(seg.start)}]</span>{spk_html}'
            f'{html.escape(seg.text)}</li>')
        cur_spk = spk
    return "\n".join(rows)


def _md_to_html(md: str) -> str:
    """Minimal markdown → HTML (headings, bold, lists, paragraphs)."""
    lines = md.splitlines()
    out, in_ul = [], False
    for ln in lines:
        if re.match(r"^#{1,6}\s", ln):
            if in_ul:
                out.append("</ul>"); in_ul = False
            lvl = len(ln) - len(ln.lstrip("#"))
            txt = html.escape(ln.lstrip("# ").strip())
            out.append(f"<h{min(lvl, 4)}>{txt}</h{min(lvl, 4)}>")
        elif re.match(r"^\s*[-*]\s", ln):
            if not in_ul:
                out.append("<ul>"); in_ul = True
            txt = html.escape(re.sub(r"^\s*[-*]\s", "", ln))
            txt = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", txt)
            out.append(f"<li>{txt}</li>")
        elif ln.strip() == "":
            if in_ul:
                out.append("</ul>"); in_ul = False
        else:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            txt = html.escape(ln)
            txt = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", txt)
            out.append(f"<p>{txt}</p>")
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


def combined_html(segments, summary_md: str, meta=None) -> str:
    meta_html = ""
    if meta:
        bits = [f"{k}: {v}" for k, v in meta.items() if v]
        if bits:
            meta_html = f'<div class="meta">{html.escape(" · ".join(bits))}</div>'
    summary_html = _md_to_html(summary_md) if (summary_md or "").strip() else \
        '<p><i>Summary not available.</i></p>'
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{_CSS}</style></head><body>
<h1>Meeting Transcript</h1>{meta_html}
<ul class="transcript">{_segments_html(segments)}</ul>
<hr><h1>Summary</h1>{summary_html}
</body></html>"""


def _meta_html(meta) -> str:
    if not meta:
        return ""
    bits = [f"{k}: {v}" for k, v in meta.items() if v]
    return f'<div class="meta">{html.escape(" · ".join(bits))}</div>' if bits else ""


def transcript_html(segments, meta=None) -> str:
    """Transcript-only page (for a standalone PDF/HTML export)."""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{_CSS}</style></head><body>
<h1>Transcript</h1>{_meta_html(meta)}
<ul class="transcript">{_segments_html(segments)}</ul>
</body></html>"""


def summary_html(summary_md: str, meta=None) -> str:
    """Summary-only page (for a standalone PDF/HTML export)."""
    body = _md_to_html(summary_md) if (summary_md or "").strip() else \
        '<p><i>Summary not available.</i></p>'
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{_CSS}</style></head><body>
<h1>Meeting Summary</h1>{_meta_html(meta)}
{body}
</body></html>"""


def render_pdf(html_str: str, out_path: str) -> str:
    """Render HTML string to PDF at out_path. Returns out_path."""
    from weasyprint import HTML
    HTML(string=html_str).write_pdf(out_path)
    return out_path
