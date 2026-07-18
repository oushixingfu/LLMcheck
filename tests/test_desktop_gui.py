from __future__ import annotations

from pathlib import Path

from llmcheck import gui_exe
from llmcheck.desktop_gui import (
    DESKTOP_MENU_LABELS,
    DESKTOP_SECTION_LABELS,
    DesktopFormValues,
    build_settings_from_form,
    desktop_profile_options,
    should_run_batch,
)


def test_desktop_gui_exposes_latest_flow_menus_and_sections() -> None:
    assert DESKTOP_MENU_LABELS == ("文件", "运行", "Profile", "报告", "帮助")
    assert DESKTOP_SECTION_LABELS == ("任务", "Profile", "LLM", "高级设置", "批处理", "运行与报告")


def test_desktop_profile_options_include_builtin_profiles() -> None:
    options = desktop_profile_options()

    assert options[0][0] == "general_standard_document"
    assert ("technical_manual", "技术手册") in options
    assert ("chinese_medicine_reference", "中医参考资料") in options


def test_desktop_form_values_build_profile_aware_settings() -> None:
    values = DesktopFormValues(
        input_path=Path("/tmp/input.md"),
        output_dir=Path("/tmp/out"),
        profile_id="technical_manual",
        llm_api_url="http://llm.test",
        llm_api_key="key",
        llm_model="model",
        concurrency=7,
        llm_chunk_chars=3200,
        acceptance_repair_rounds=2,
        timeout_seconds=900,
        mineru_api_url="https://mineru.test",
        mineru_api_key="mineru-key",
        mineru_concurrency=5,
        mineru_batch_size=8,
        mineru_timeout_seconds=7200,
        mineru_request_timeout_seconds=90,
        mineru_max_retries=4,
        mineru_retry_backoff_seconds=2.5,
        pdf_page_chunk_size=25,
        ppx_command="/tool/ppx",
        ppx_cwd="/tool",
        ppx_timeout_seconds=1200,
        ppx_backend="pipeline",
        ppx_ocr="force",
        ppx_formula="yes",
        book_concurrency=3,
        start_index=2,
        limit=9,
        force=True,
    )

    settings = build_settings_from_form(values)

    assert settings.profile_id == "technical_manual"
    assert settings.llm_api_url == "http://llm.test"
    assert settings.concurrency == 7
    assert settings.mineru_batch_size == 8
    assert settings.ppx_formula == "yes"


def test_desktop_batch_detection_uses_input_directory(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_file = tmp_path / "source.md"
    source_file.write_text("# A\n", encoding="utf-8")

    assert should_run_batch(source_dir) is True
    assert should_run_batch(source_file) is False


def test_gui_exe_entry_starts_native_desktop(monkeypatch) -> None:
    captured: dict[str, bool] = {}

    def fake_run_desktop_gui() -> int:
        captured["called"] = True
        return 0

    monkeypatch.setattr("llmcheck.gui_exe.run_desktop_gui", fake_run_desktop_gui)

    assert gui_exe.main([]) == 0
    assert captured == {"called": True}
