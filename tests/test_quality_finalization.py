from __future__ import annotations

from llmcheck.quality import final_acceptance_report, finalize_standard_document, quality_errors, quality_hints


def test_quality_errors_blocks_visible_encoding_artifacts() -> None:
    text = "这是锟斤拷文本，含有�替换符和零宽\u200b字符。\n"

    errors = quality_errors(text)

    assert "mojibake" in errors
    assert "replacement_characters" in errors
    assert "zero_width_characters" in errors


def test_quality_errors_blocks_abnormal_spacing_and_forced_line_breaks() -> None:
    text = "这是一个普通段落的第一部分\n第二部分仍然是同一个句子\n第三部分才结束。另有中  文异常空格。\n"

    errors = quality_errors(text)

    assert "abnormal_cjk_spaces" in errors
    assert "forced_line_breaks" in errors


def test_quality_errors_blocks_duplicate_repeated_lines() -> None:
    text = "扫描页眉\n正文第一段。\n\n扫描页眉\n正文第二段。\n\n扫描页眉\n正文第三段。\n"

    assert "duplicate_repeated_lines" in quality_errors(text)


def test_quality_hints_explain_new_blocking_errors() -> None:
    text = "扫描页眉\n这是锟斤拷\u200b\n\n扫描页眉\n这是�。\n\n扫描页眉\n中  文\n"

    hints = quality_hints(text)

    assert "mojibake" in hints["error_hints"]
    assert "replacement_characters" in hints["error_hints"]
    assert "zero_width_characters" in hints["error_hints"]
    assert "abnormal_cjk_spaces" in hints["error_hints"]
    assert "duplicate_repeated_lines" in hints["error_hints"]


def test_finalize_standard_document_removes_duplicate_running_headers_and_reports_changes() -> None:
    text = "# 标题\n正文第一段。\n\n扫描页眉\n正文第二段。\n\n扫描页眉\n正文第三段。\n"

    result = finalize_standard_document(text)

    assert result["status"] == "finalized"
    assert result["finalized"] is True
    assert result["text"].count("扫描页眉") == 0
    assert "# 标题\n\n正文第一段。" in result["text"]
    assert result["input_sha256"] != result["output_sha256"]
    assert any(change["kind"] == "removed_repeated_lines" for change in result["changes"])


def test_finalize_standard_document_reports_no_change_for_clean_text() -> None:
    text = "# 标题\n\n正文第一段。\n\n正文第二段。\n"

    result = finalize_standard_document(text)

    assert result["status"] == "finalized"
    assert result["finalized"] is False
    assert result["changes"] == []
    assert result["text"] == text


def test_final_acceptance_report_blocks_visible_artifacts() -> None:
    report = final_acceptance_report("# 标题\n\n这是锟斤拷，含有�和中  文异常空格。\n")

    assert report["status"] == "needs_revision"
    assert report["accepted"] is False
    assert "mojibake" in report["blocking_errors"]
    assert "replacement_characters" in report["blocking_errors"]
    assert "abnormal_cjk_spaces" in report["blocking_errors"]
    assert report["hints"]["error_hints"]["mojibake"]


def test_final_acceptance_report_blocks_hidden_break_and_duplicate_artifacts() -> None:
    report = final_acceptance_report(
        "# Title\n\n"
        "This paragraph is clearly a continuous sentence\n"
        "but OCR split it across a physical row.\n\n"
        "scan header\n"
        "正文第一段。\n\n"
        "scan header\n"
        "正文第二段。\n\n"
        "scan header\n"
        "正文第三段。\n"
        "\u200b"
    )

    assert report["accepted"] is False
    assert "zero_width_characters" in report["blocking_errors"]
    assert "forced_line_breaks" in report["blocking_errors"]
    assert "duplicate_repeated_lines" in report["blocking_errors"]


def test_final_acceptance_report_blocks_comma_boundary_physical_breaks() -> None:
    english_report = final_acceptance_report(
        "# Title\n\n"
        "This paragraph is clearly a continuous sentence,\n"
        "but OCR split it immediately after a comma.\n"
    )
    chinese_comma_report = final_acceptance_report("这是一个应当连续阅读的普通段落，\n下一行是 OCR 物理折行造成的残留。\n")
    enumeration_report = final_acceptance_report("这是一个包含枚举顿号的连续普通段落、\n下一行仍然是同一个段落的 OCR 折行。\n")

    assert english_report["accepted"] is False
    assert "forced_line_breaks" in english_report["blocking_errors"]
    assert chinese_comma_report["accepted"] is False
    assert "forced_line_breaks" in chinese_comma_report["blocking_errors"]
    assert enumeration_report["accepted"] is False
    assert "forced_line_breaks" in enumeration_report["blocking_errors"]


def test_final_acceptance_report_accepts_normal_short_lines() -> None:
    report = final_acceptance_report("Short one\nShort two\n")

    assert "forced_line_breaks" not in report["blocking_errors"]


def test_final_acceptance_report_accepts_clean_text_with_nonblocking_warnings() -> None:
    report = final_acceptance_report("# 标题\n\n正文第一段自然结束。\n")

    assert report["status"] == "passed"
    assert report["accepted"] is True
    assert report["blocking_errors"] == []
