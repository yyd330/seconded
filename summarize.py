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

"""Meeting summarization via an OpenAI-compatible local backend.

Point SUMMARY_API_URL / SUMMARY_MODEL (or --summary-url / --summary-model) at any
OpenAI-compatible /v1/chat/completions endpoint — LM Studio, llama.cpp, vLLM,
Ollama's /v1, etc. The model must be resident on the backend; for LM Studio
(port 1234) the `lms` CLI is used to load it automatically.
"""
import json
import logging
import re
import shutil
import subprocess
import time
from urllib.parse import urlparse

import requests

log = logging.getLogger("summary")

import os

LMSTUDIO_URL = os.environ.get("SUMMARY_API_URL") or os.environ.get(
    "LM_STUDIO_URL", "http://127.0.0.1:8080/v1/chat/completions")
MODEL = os.environ.get("SUMMARY_MODEL") or os.environ.get(
    "LM_STUDIO_MODEL", "qwen3.8-flash-next-unsloth-ud-iq4_xs")
LMS_BIN = os.environ.get("LM_STUDIO_BIN") or (
    shutil.which("lms") or os.path.expanduser("~/.lmstudio/bin/lms"))
LMSTUDIO_TIMEOUT = int(os.environ.get("SUMMARY_TIMEOUT") or os.environ.get(
    "LM_STUDIO_TIMEOUT", "3600"))


def configure(url: str = None, model: str = None, timeout: int = None):
    """Override backend URL/model at runtime (used by server CLI flags)."""
    global LMSTUDIO_URL, MODEL, LMSTUDIO_TIMEOUT
    if url:
        LMSTUDIO_URL = url
    if model:
        MODEL = model
    if timeout:
        LMSTUDIO_TIMEOUT = int(timeout)


def _models_url() -> str:
    u = urlparse(LMSTUDIO_URL)
    return f"{u.scheme}://{u.netloc}/v1/models"


def _is_lm_studio() -> bool:
    return urlparse(LMSTUDIO_URL).port == 1234


def _model_resident() -> bool:
    """True if the backend lists MODEL as loaded/available."""
    try:
        r = requests.get(_models_url(), timeout=10)
        r.raise_for_status()
        for m in r.json().get("data", []):
            if m.get("id") == MODEL:
                status = m.get("status")
                # some backends report {"status": {"value": "loaded"}};
                # LM Studio and others omit status entirely (listing = available).
                if isinstance(status, dict):
                    return status.get("value") == "loaded"
                return True
    except Exception as e:
        log.warning("model check failed: %s", e)
    return False


def ensure_model_loaded():
    """Make sure the pinned model is resident on the backend."""
    if _model_resident():
        return True
    if _is_lm_studio():
        log.info("loading %s into LM Studio ...", MODEL)
        subprocess.run([LMS_BIN, "load", MODEL, "--identifier", MODEL,
                        "--gpu", "max", "--context-length", "97280"],
                       capture_output=True, timeout=300)
        time.sleep(5)
        return True
    raise RuntimeError(
        f"model {MODEL!r} is not loaded on the backend at {_models_url()}. "
        "Start the backend with the model resident, or point "
        "SUMMARY_API_URL/SUMMARY_MODEL at a loaded model.")


def _extract_text(payload: dict) -> str:
    """Pull the answer out of a chat-completion response.

    Reasoning models (e.g. the current qwen3.8-flash backend) stream their
    thinking into `reasoning_content` and only fill `content` at the end —
    `content` can be null when the token budget runs out mid-thought.
    """
    msg = payload["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    if content:
        return content
    reasoning = (msg.get("reasoning_content") or "").strip()
    if reasoning:
        log.warning("empty content; falling back to reasoning_content "
                    "(raise max_tokens or disable reasoning if this repeats)")
        return reasoning
    raise RuntimeError("backend returned empty content "
                       f"(finish_reason={payload['choices'][0].get('finish_reason')}); "
                       "the reasoning budget may be too small")

SYSTEM_PROMPT = """You are an expert meeting assistant producing professional, accurate meeting notes.

Input: A meeting transcript with speaker labels and timestamps. Your job is to extract the substantive content and structure it for quick review.

CORE PRINCIPLES:
1. Be factual — base everything on the transcript. Do not invent content, attributes, or inferences.
2. Extract, don't just summarize — pull out specific names, dates, numbers, decisions, and action items.
3. Prioritize the actionable — readers want to know: what was decided, what happens next, who does what.
4. Be concise — no filler phrases, no "they discussed..." without the substance of what was discussed.
5. No duplicates — each fact, decision, or action item appears in exactly ONE section (action items
   once; do not list "decide X" and "revisit X" as two items). Skip procedural chatter (greetings,
   "let's get started", roll call) unless it is itself a decision.
6. Keep dates and deadlines exactly as stated in the transcript — never convert relative dates
   ("next Thursday", "end of November") into absolute calendar dates.

If a speaker label is uncertain, refer to them as "Speaker N".
If owners or deadlines are not explicit, infer from context or write "unassigned" / "no deadline".
If no section applies (e.g. no action items), write "(none)".

Follow EXACTLY the section structure given below."""


# ---------------------------------------------------------------- templates
# Each template defines its section structure in English and Simplified Chinese;
# the language rule picks which one to emit.

TEMPLATES = {
    # 'default' is a hidden alias of 'action' kept for old sessions' meta.json
    # and as a safe fallback; it is not offered in the UI.
    "default": {
        "label_en": "Standard (recommended)",
        "hidden": True,
        "structure_en": """## Summary
(2-3 sentences max: who met, what about, what the outcome was)

## Key Decisions
(Bullet list of every decision made. State the decision clearly, not "they agreed to discuss".)

## Action Items
(- [ ] **Action** — Owner: X — Deadline: Y (if mentioned, otherwise "not specified")
 If no deadline, write "no deadline". If owner unknown, write "unassigned".)

## Open Questions
(Bullet list of unresolved items; write "(none)" if all resolved)

## Critical Details
(Key numbers, dates, technical details, risks mentioned — only if not already in action items)""",
        "structure_zh": """## 摘要
（2-3句话：谁参加了，讨论什么，结果如何）

## 关键决定
（列出所有做出的决定，清晰陈述决定本身）

## 行动项
（- [ ] **行动** — 负责人：X — 截止日期：Y（如未提到则写"未指定"）
 如无法判断负责人写"未分配"，无截止日期写"无期限"。）

## 待解决问题
（未解决的议题列表；全部解决则写"(无)"）

## 关键细节
（重要数字、日期、技术细节、风险 — 仅当行动项中未包含时）""",
    },
    "action": {
        "label_en": "Action-focused (decisions & tasks)",
        "structure_en": """## Decisions Made
(Bullet list. Each decision stated as a complete sentence: "X will do Y" or "The team will Z by [date]". Include who decided if clear from transcript.)

## Action Items
(- [ ] **Specific action** — Owner: Name — Deadline: Date
 If owner unknown: write "unassigned" or "by next meeting" if implied.
 If deadline unknown: write "no deadline".
 List each action exactly once — never split one commitment into two items.)

## Blocked / Risks
(Items at risk or blocked, with reason and impact; or "none identified")

## Next Meeting / Check-in
(When the team should reconvene, if discussed; or "not specified")""",
        "structure_zh": """## 已做决定
（完整句子陈述每个决定，如"X将做Y"或"团队将于[日期]前Z"）

## 行动项
（- [ ] **具体行动** — 负责人：姓名 — 截止日期：日期
 如无法判断负责人写"未分配"，无截止日期写"无期限"或"下次会议前"。）

## 受阻 / 风险
（受阻或有风险的事项及原因、影响；未识别则写"无"）

## 下次会议 / 跟进
（如讨论了下次会议时间则列出；否则写"未指定"）""",
    },
    "executive": {
        "label_en": "Executive brief (for leadership)",
        "structure_en": """## Outcome
(1-2 sentences: bottom line. Did they make progress? What's the headline?)

## Key Results / Progress
(Bullet list of concrete outcomes only: completed items, milestones reached, numbers,
approvals given. No procedural notes like "the meeting opened" or "X introduced the topic".)

## Key Risks / Concerns
(Bullet list of risks raised or implied, with potential impact. Or "none raised.")

## Decisions Required from Leadership
(Decisions that need executive input; or "none at this time")

## Next Steps
(Immediate next steps, owners if known, and any upcoming key dates.)""",
        "structure_zh": """## 结果
（1-2句话：核心结论。有进展吗？重点是什么？）

## 关键成果 / 进展
（具体成果：已完成事项、达成的里程碑、数字。）

## 关键风险 / 顾虑
（提到的风险或隐含风险及潜在影响。未提到则写"无"。）

## 需要管理层决策的事项
（需要高管输入的决定；暂无则写"目前无"）

## 下一步
（接下来的步骤，能判断时注明负责人及重要日期。）""",
    },
    "detailed": {
        "label_en": "Detailed minutes (full record)",
        "structure_en": """## Overview
(What the meeting covered, attendees mentioned, and context in 2-3 sentences.)

## Topics Discussed
(For each topic: what was discussed, key arguments/points raised by specific people, and the outcome of that discussion.)

## Decisions & Rationale
(Each decision with WHY it was made and who made it. Be thorough.)

## Action Items
(- [ ] **Task** — Owner: X — Deadline: Y
 Include all commitments made, even implicit ones.)

## Open Questions / Parking Lot
(Everything left unresolved, deferred, or noted for future discussion. Be complete.)

## Notes
(Any other relevant details: announcements, context provided, items of significance that didn't fit above.)""",
        "structure_zh": """## 概述
（2-3句话说明会议内容、提到的参会者和背景。）

## 讨论议题
（每个议题：讨论了什么，谁提出了什么观点和论据，该议题的结果如何。）

## 决定及原因
（每个决定及其理由和决策人，务必完整。）

## 行动项
（- [ ] **任务** — 负责人：X — 截止日期：Y
 包含所有承诺，即使是隐式的。）

## 待解决问题 / 搁置事项
（所有未解决、推迟或计划未来讨论的事项，务必完整。）

## 备注
（其他相关信息：公告、背景说明、上述未涵盖但有意义的事项。）""",
    },
    "discussion_group": {
        "label_en": "Discussion group (book club, study group, round table)",
        "structure_en": """## Topic & Material
(What the group was discussing: the text, topic, or question explored, as stated in the meeting.)

## Key Points Raised
(Bullet list of the main ideas, interpretations, or observations that came out of the discussion,
 each grounded in what was actually said. Reference other material only if it was mentioned.)

## Questions Raised & Where They Landed
(Bullet list of questions people asked, and whether the group answered them, gave a partial
 answer, or left them open. Quote the question as closely as possible.)

## Notable Contributions
(Points or experiences shared by specific speakers that moved the discussion forward —
 "Speaker 2 noted that…". Skip routine or procedural remarks.)

## Takeaways & Agreements
(What the group converged on: shared conclusions, decisions, or personal commitments made.)

## Follow-ups
(Next steps: what will be discussed or read next, preparation asked of attendees, announcements.
 Write "(none mentioned)" if there were none.)""",
        "structure_zh": """## 主题与材料
（小组讨论的内容：经文、书籍、主题或问题，按会议中的表述记录。）

## 主要观点
（列出讨论中产生的主要观点、理解或观察，每条须基于实际发言。
 仅在发言提及时才引用其他材料。）

## 提出的问题及讨论结果
（列出大家提出的问题，以及小组是否解答、部分解答或留待后续。尽量保留问题的原始表述。）

## 有亮点的分享
（特定发言者分享的、真正推动讨论的观点或经历——"说话人2提到……"。
 跳过例行或流程性发言。）

## 共识与收获
（小组达成的共同结论、决定，或个人作出的承诺。）

## 后续安排
（下一步：下次讨论或阅读的内容、要求与会者做的准备、通知事项。
 没有则写"（未提及）"。）""",
    },
    "lecture": {
        "label_en": "Lecture / webinar / talk (mostly one speaker)",
        "structure_en": """## Topic & Speaker
(What the session was about, who presented, and the stated goal or framing, in 1-2 sentences.)

## Outline of Key Points
(The talk's structure as presented: main points in order, each with the essential explanation,
 example, or number the speaker used to support it. Keep the speaker's own emphasis and order.)

## Examples & Demonstrations
(Specific examples, demos, case studies, or slides the speaker walked through — enough detail
 that a reader recalls the point without the slides. Write "(none)" if there were none.)

## Q&A
(Questions from the audience and the answers given, condensed. Write "(none)" if there was no Q&A.)

## Resources & References Mentioned
(Books, tools, links, papers, or upcoming sessions the speaker pointed to. Write "(none mentioned)".)

## Action Items / Announcements
(Anything attendees were asked to do, sign up for, or remember. Write "(none)".)""",
        "structure_zh": """## 主题与讲者
（本次内容是什么、谁主讲、讲者陈述的目标或框架，1-2句话。）

## 要点提纲
（按讲者的结构记录：主要观点依序排列，每条附上讲者使用的关键解释、例子或数据。
 保留讲者自己的重点和顺序。）

## 案例与演示
（讲者讲解的具体例子、演示、案例或幻灯片——细节要足够让读者不看幻灯片也能回忆起要点。
 没有则写"（无）"。）

## 问答环节
（听众提问及讲者回答的精简记录。没有问答则写"（无）"。）

## 提及的资料与参考
（讲者推荐的书籍、工具、链接、论文或后续课程。未提及则写"（未提及）"。）

## 行动项 / 通知
（要求与会者完成、报名或记住的事项。没有则写"（无）"。）""",
    },
    "interview": {
        "label_en": "Interview (candidate, guest, user research)",
        "structure_en": """## Subject & Context
(Who was interviewed, by whom, for what purpose, in 1-2 sentences.)

## Topics Covered
(For each topic: the questions asked and a faithful condensation of the subject's answers —
 their words and positions, not the interviewers' interpretations.)

## Notable Quotes & Moments
(Distinctive statements worth keeping verbatim or near-verbatim, with brief context.)

## Signals & Observations
(Factual observations relevant to the interview's purpose: experience cited, concerns raised,
 enthusiasm or hesitation shown. Keep this descriptive, not a verdict.)

## Follow-ups
(Next steps agreed: references to check, tasks to complete, decision timeline, who contacts whom.)""",
        "structure_zh": """## 受访人与背景
（谁接受了谁的访谈、目的是什么，1-2句话。）

## 涉及话题
（每个话题：提出了哪些问题，以及受访人回答的忠实精简——记录其本人的表述和立场，
 而非访谈者的解读。）

## 值得记录的回答
（值得原样或近乎原样保留的表述，附简短背景。）

## 观察与信号
（与访谈目的相关的客观观察：提到的经验、表达的顾虑、显露的热情或犹豫。
 只做描述，不下结论。）

## 后续安排
（商定的下一步：需核实的推荐人、待完成事项、决定时间线、谁联系谁。）""",
    },
    "retrospective": {
        "label_en": "Retrospective / review (what went well & what didn't)",
        "structure_en": """## What Was Reviewed
(The period, project, or event looked back on, and any metrics or outcomes stated, in 1-2 sentences.)

## What Went Well
(Bullet list, each item specific enough to repeat — name the thing, not just "communication".)

## What Didn't Go Well
(Bullet list of problems, friction, or misses, with the cause or context given, if any.)

## Root Causes & Lessons
(Explanations the group offered for the problems, and lessons explicitly drawn. Write "(none stated)"
 if the group didn't go deeper than listing problems.)

## Action Items
(- [ ] **Improvement** — Owner: X — Deadline: Y — addresses: which problem it fixes
 Write "(none)" if the review produced no commitments.)""",
        "structure_zh": """## 回顾对象
（回顾的时间段、项目或事件，以及会上提到的指标或结果，1-2句话。）

## 做得好的
（逐条列出，具体到可复制——写清楚具体是什么，而不是笼统的"沟通不错"。）

## 不足之处
（列出问题、摩擦或失误，附上会上给出的原因或背景。）

## 根因与经验
（小组对问题给出的解释，以及明确总结的经验。若只列问题未深究，写"（未提及）"。）

## 行动项
（- [ ] **改进措施** — 负责人：X — 截止日期：Y — 对应解决：哪个问题
 没有承诺则写"（无）"。）""",
    },
}

LENGTHS = {
    "short": {
        "label_en": "Short (quick skim)",
        "rule_en": "LENGTH: Keep the ENTIRE summary under ~10 lines. Extremely concise — only the most important items.",
        "rule_zh": "长度：整个摘要控制在约10行以内，极其精炼——只保留最重要的内容。",
    },
    "medium": {
        "label_en": "Medium (default)",
        "default": True,
        "rule_en": "LENGTH: Medium — roughly 20-40 lines total.",
        "rule_zh": "长度：中等——总共约20-40行。",
    },
    "long": {
        "label_en": "Long & thorough",
        "rule_en": "LENGTH: Be thorough and complete — include all relevant context, numbers, names and follow-ups. No strict length limit.",
        "rule_zh": "长度：详尽完整——包含所有相关背景、数字、人名和后续事项，不设严格长度限制。",
    },
}


def build_prompt(language: str, template: str = "default", length: str = "medium") -> str:
    """Assemble the system prompt for a given language/template/length.

    `language` is a code from LANG_NAMES (e.g. 'en', 'zh', 'ja', 'ru').
    Chinese uses the hand-written Chinese section skeleton; every other
    language uses the English skeleton plus an explicit output-language rule
    (models follow English instructions while writing in the target language).
    """
    tpl = TEMPLATES.get(template) or TEMPLATES["default"]
    ln = LENGTHS.get(length) or LENGTHS["medium"]
    zh = (language == "zh")
    structure = tpl["structure_zh"] if zh else tpl["structure_en"]
    if zh:
        lang_rule = ("LANGUAGE RULE: Write the ENTIRE summary in Simplified Chinese (简体中文). "
                     "Keep proper nouns, product names and code in their original form.")
    else:
        name = LANG_NAMES.get(language, "the same language as the transcript")
        lang_rule = (f"LANGUAGE RULE: Write the ENTIRE summary in {name}. "
                     "Translate any part of the transcript that is in another language. "
                     "Keep proper nouns, product names and code in their original form. "
                     "The section headings given above are English instructions — output "
                     "equivalent headings in the target language.")
    return SYSTEM_PROMPT + "\n\n" + structure.strip() + \
           "\n\n" + ln["rule_zh" if zh else "rule_en"] + "\n\n" + lang_rule


def detect_dominant_language(text: str) -> str:
    """Heuristic dominant-language detection by script.

    Returns a language code ('zh', 'en', 'ja', 'ko', 'ru', 'ar', 'th', 'el',
    'he', 'hi'). Latin-script text returns 'en' as a placeholder — script
    can't tell Spanish from French; prefer the ASR-detected language when
    available (normalize_lang).
    """
    counts = {
        "zh": len(re.findall(r"[\u4e00-\u9fff]", text)),
        "ja": len(re.findall(r"[\u3040-\u30ff]", text)),   # kana ⇒ Japanese
        "ko": len(re.findall(r"[\uac00-\ud7af]", text)),
        "ru": len(re.findall(r"[\u0400-\u04ff]", text)),
        "ar": len(re.findall(r"[\u0600-\u06ff]", text)),
        "th": len(re.findall(r"[\u0e00-\u0e7f]", text)),
        "el": len(re.findall(r"[\u0370-\u03ff]", text)),
        "he": len(re.findall(r"[\u0590-\u05ff]", text)),
        "hi": len(re.findall(r"[\u0900-\u097f]", text)),
        "en": len(re.findall(r"[A-Za-z]+", text)),  # Latin words ≈ English count
    }
    if counts["ja"] > 0:
        # Kana present: Japanese unless kana is only sprinkled into mostly
        # Chinese text (loanwords etc.) — real Japanese runs 30%+ kana,
        # loanword noise is a handful of chars against hundreds of hanzi.
        if counts["ja"] * 3 >= counts["zh"]:
            return "ja"
        counts["zh"] += counts["ja"]
        counts["ja"] = 0
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] > 0 else "en"


# Language code -> display name used in prompts
LANG_NAMES = {
    "en": "English", "zh": "Simplified Chinese", "ja": "Japanese", "ko": "Korean",
    "ru": "Russian", "es": "Spanish", "fr": "French", "de": "German",
    "pt": "Portuguese", "it": "Italian", "ar": "Arabic", "hi": "Hindi",
    "th": "Thai", "vi": "Vietnamese", "id": "Indonesian", "tr": "Turkish",
    "pl": "Polish", "nl": "Dutch", "sv": "Swedish", "uk": "Ukrainian",
    "el": "Greek", "he": "Hebrew", "fa": "Persian",
}
_NAME_TO_CODE = {name.lower(): code for code, name in LANG_NAMES.items()}
_NAME_TO_CODE.update({
    "chinese": "zh", "mandarin": "zh", "cantonese": "zh", "spanish": "es",
    "french": "fr", "german": "de", "japanese": "ja", "korean": "ko",
    "russian": "ru", "portuguese": "pt", "italian": "it", "arabic": "ar",
    "hindi": "hi", "thai": "th", "vietnamese": "vi", "indonesian": "id",
    "turkish": "tr", "polish": "pl", "dutch": "nl", "swedish": "sv",
    "ukrainian": "uk", "greek": "el", "hebrew": "he", "persian": "fa",
    "english": "en",
})


def normalize_lang(label) -> "str | None":
    """Normalize an ASR language label ('Chinese', 'zh', 'zh-CN', 'en'...)
    to a LANG_NAMES code; None if unrecognized."""
    if not label:
        return None
    s = str(label).strip().lower().replace("_", "-")
    if s in LANG_NAMES:
        return s
    code = _NAME_TO_CODE.get(s) or _NAME_TO_CODE.get(s.split("-")[0])
    if code:
        return code
    base = s.split("-")[0]
    return base if base in LANG_NAMES else None


def summarize(transcript_text: str, language=None, template="default", length="medium") -> str:
    """transcript_text: full meeting transcript with [HH:MM:SS] Speaker N: lines.

    language: 'en' | 'zh' (or None → auto-detect from the transcript).
    template: one of TEMPLATES keys ('default', 'action', 'executive', 'detailed').
    length:   one of LENGTHS keys ('short', 'medium', 'long').
    The summary is written in that language; for mixed-language meetings it
    follows the dominant one.
    """
    if not language:
        language = detect_dominant_language(transcript_text)
    system_prompt = build_prompt(language, template, length)

    ensure_model_loaded()
    # chunk only if huge; backend has 262K context so most meetings fit in
    # a single pass (override with SUMMARIZE_MAX_CHARS)
    max_chars = int(os.environ.get("SUMMARIZE_MAX_CHARS", "150000"))
    chunks = []
    while len(transcript_text) > max_chars:
        cut = transcript_text.rfind("\n", 0, max_chars)
        if cut < max_chars // 2:
            cut = max_chars
        chunks.append(transcript_text[:cut])
        transcript_text = transcript_text[cut:]
    chunks.append(transcript_text)

    parts = []
    for i, chunk in enumerate(chunks):
        msgs = [{"role": "system", "content": system_prompt}]
        if len(chunks) > 1:
            msgs[-1]["content"] += (f"\n\nThis is part {i+1} of {len(chunks)} of a longer "
                                    f"transcript. Summarize only this part; other parts "
                                    f"will be merged later.")
        msgs.append({"role": "user", "content": chunk})

        t0 = time.time()
        log.info("summarizing chunk %d/%d (%d chars) ...", i + 1, len(chunks), len(chunk))
        timeout = int(os.environ.get("SUMMARIZE_TIMEOUT", "3600"))
        max_tokens = int(os.environ.get("SUMMARIZE_MAX_TOKENS", "16384"))
        try:
            r = requests.post(LMSTUDIO_URL, json={
                "model": MODEL,
                "messages": msgs,
                "temperature": 0.2,
                "max_tokens": max_tokens,
            }, timeout=timeout)
            r.raise_for_status()
        except requests.exceptions.Timeout:
            raise RuntimeError(f"summary backend timed out after {timeout}s")
        except requests.exceptions.ConnectionError:
            raise RuntimeError(f"Cannot connect to summary backend at {LMSTUDIO_URL}")
        text = _extract_text(r.json())
        log.info("chunk %d done in %.1fs", i + 1, time.time() - t0)
        parts.append(text)

    if len(parts) == 1:
        return parts[0]

    # merge partial summaries with one more LLM call
    merged_prompt = ("Merge these partial meeting summaries into ONE final summary "
                     "(same section structure, dedupe; keep the SAME language as the "
                     "partials):\n\n" + "\n\n---\n\n".join(parts))
    timeout = int(os.environ.get("SUMMARIZE_TIMEOUT", "3600"))
    try:
        r = requests.post(LMSTUDIO_URL, json={
            "model": MODEL,
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": merged_prompt}],
            "temperature": 0.2,
            "max_tokens": int(os.environ.get("SUMMARIZE_MAX_TOKENS", "16384")),
        }, timeout=timeout)
        r.raise_for_status()
    except requests.exceptions.Timeout:
        raise RuntimeError(f"summary backend timed out after {timeout}s during merge")
    except requests.exceptions.ConnectionError:
        raise RuntimeError(f"Cannot connect to summary backend at {LMSTUDIO_URL}")
    return _extract_text(r.json())
