#!/usr/bin/env python3
"""Unit tests for summarize.py language detection and prompt building.

Run with: python tests/test_summarize.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from summarize import detect_dominant_language, normalize_lang, build_prompt, TEMPLATES, LENGTHS

def test_language_detection_english():
    """Pure English transcript should detect 'en'."""
    print("Testing English language detection...")
    text = "Hello, this is a test meeting. We discussed the quarterly results. The revenue was up 15%."
    assert detect_dominant_language(text) == "en", "Failed to detect English"
    print("  OK")

def test_language_detection_chinese():
    """Pure Chinese transcript should detect 'zh'."""
    print("Testing Chinese language detection...")
    text = "你好，这是一个测试会议。我们讨论了季度结果。收入增长了百分之十五。"
    assert detect_dominant_language(text) == "zh", "Failed to detect Chinese"
    print("  OK")

def test_language_detection_mixed():
    """Mixed language should detect the dominant one."""
    print("Testing mixed language detection...")
    # More English words than Chinese characters
    mixed_en_dominant = "Hello world, how are you? 你好吗？We are meeting today to discuss the project."
    assert detect_dominant_language(mixed_en_dominant) == "en", "Failed to detect English dominant"

    # More Chinese characters than English words
    mixed_zh_dominant = "你好，我们今天要开会讨论项目。Hello, how are you?"
    assert detect_dominant_language(mixed_zh_dominant) == "zh", "Failed to detect Chinese dominant"
    print("  OK")

def test_template_structure():
    """All templates should have required fields."""
    print("Testing template structure...")
    for key, tpl in TEMPLATES.items():
        assert "label_en" in tpl, f"Template {key} missing label_en"
        assert "structure_en" in tpl, f"Template {key} missing structure_en"
        assert "structure_zh" in tpl, f"Template {key} missing structure_zh"
    print("  OK")

def test_length_structure():
    """All lengths should have required fields."""
    print("Testing length structure...")
    for key, ln in LENGTHS.items():
        assert "label_en" in ln, f"Length {key} missing label_en"
        assert "rule_en" in ln, f"Length {key} missing rule_en"
        assert "rule_zh" in ln, f"Length {key} missing rule_zh"
    print("  OK")

def test_build_prompt_en():
    """Building an English prompt should include English rules."""
    print("Testing English prompt building...")
    prompt = build_prompt("en", "default", "medium")
    assert "Simplified Chinese" not in prompt or "LANGUAGE RULE" in prompt
    assert "English" in prompt
    assert "Medium" in prompt or "medium" in prompt
    print("  OK")

def test_build_prompt_zh():
    """Building a Chinese prompt should include Chinese rules."""
    print("Testing Chinese prompt building...")
    prompt = build_prompt("zh", "default", "short")
    assert "Simplified Chinese" in prompt or "简体中文" in prompt
    assert "short" in prompt.lower() or "10行" in prompt
    print("  OK")

def test_language_detection_other_scripts():
    """Japanese/Korean/Russian/Arabic text should detect by script."""
    print("Testing other-script detection...")
    assert detect_dominant_language("今日は会議で新機能を議論しました") == "ja"
    assert detect_dominant_language("오늘 회의에서 새 기능을 논의했습니다") == "ko"
    assert detect_dominant_language("Сегодня мы обсудили новый релиз проекта") == "ru"
    assert detect_dominant_language("ناقشنا اليوم خطة الإطلاق الجديدة") == "ar"
    # Japanese kanji must not outvote real kana-heavy Japanese into 'zh'
    assert detect_dominant_language("今日は会議で新機能について議論しましたが、来週のリリースに向けてテストを続けます") == "ja"
    # Chinese text with a couple of Japanese loanwords stays Chinese
    assert detect_dominant_language("我们讨论了部署方案，决定下周上线新功能模块，请大家准备测试环境并确认发布清单") == "zh"
    print("  OK")

def test_normalize_lang():
    """ASR language labels normalize to codes."""
    print("Testing normalize_lang...")
    assert normalize_lang("Chinese") == "zh"
    assert normalize_lang("zh-CN") == "zh"
    assert normalize_lang("English") == "en"
    assert normalize_lang("Spanish") == "es"
    assert normalize_lang("Japanese") == "ja"
    assert normalize_lang("Klingon") is None
    assert normalize_lang(None) is None
    print("  OK")

def test_build_prompt_other_languages():
    """Non-zh languages get the English skeleton + target-language rule."""
    print("Testing prompts for other languages...")
    for code, name in [("ja", "Japanese"), ("ru", "Russian"), ("es", "Spanish")]:
        prompt = build_prompt(code, "action", "medium")
        assert f"in {name}" in prompt, f"{code}: missing target language rule"
        assert "LANGUAGE RULE" in prompt
    # unknown code falls back to 'same language as transcript'
    assert "same language as the transcript" in build_prompt("xx", "action", "medium")
    print("  OK")

if __name__ == "__main__":
    tests = [
        test_language_detection_english,
        test_language_detection_chinese,
        test_language_detection_mixed,
        test_language_detection_other_scripts,
        test_normalize_lang,
        test_template_structure,
        test_length_structure,
        test_build_prompt_en,
        test_build_prompt_zh,
        test_build_prompt_other_languages,
    ]

    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as e:
            print(f"  FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"  EXCEPTION: {e}")
            failed += 1

    if failed:
        print(f"\n{failed} test(s) failed")
        exit(1)
    print("\nAll tests passed")
