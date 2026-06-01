from __future__ import annotations

from pathlib import Path
from typing import Any

from llmcheck.llm import (
    acceptance_result_payload,
    build_acceptance_prompt,
    build_correction_prompt,
    build_repair_prompt,
    correction_result_payload,
    repair_result_payload,
)
from llmcheck.profiles import DEFAULT_PROFILE_ID, get_profile, list_profiles


class CapturingClient:
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.prompts: list[str] = []

    def complete_json(self, prompt: str) -> dict[str, Any]:
        self.prompts.append(prompt)
        return dict(self.result)


def test_default_profile_is_general_standard_document() -> None:
    profile = get_profile(DEFAULT_PROFILE_ID)

    assert profile.id == "general_standard_document"
    assert "通用" in profile.label
    assert "中医" not in profile.description


def test_profile_registry_contains_domain_presets() -> None:
    ids = [profile["id"] for profile in list_profiles()]

    assert ids[0] == "general_standard_document"
    assert "academic_paper" in ids
    assert "technical_manual" in ids
    assert "legal_contract" in ids
    assert "financial_report" in ids
    assert "medical_reference" in ids
    assert "chinese_medicine_reference" in ids


def test_unknown_profile_raises_clear_error() -> None:
    try:
        get_profile("not-a-profile")
    except ValueError as error:
        assert "未知文档 profile" in str(error)
        assert "general_standard_document" in str(error)
    else:
        raise AssertionError("unknown profile should fail")


def test_default_prompts_are_domain_neutral() -> None:
    prompt = build_correction_prompt(source_name="manual.md", text_path=Path("manual.md"), text="第一行\n第二行")
    acceptance = build_acceptance_prompt(source_name="manual.md", text_path=Path("manual.md"), text="第一行\n第二行")

    assert "文档清洗与结构规范化编辑" in prompt
    assert "最终验收员" in acceptance
    assert "中医 Markdown 文本" not in prompt
    assert "中医 Markdown 文本" not in acceptance
    assert "不得凭医学常识" not in prompt
    assert "医案、处方" not in acceptance
    assert "乱码" in acceptance
    assert "强制换行" in acceptance


def test_profile_specific_prompt_can_preserve_chinese_medicine_rules() -> None:
    profile = get_profile("chinese_medicine_reference")
    prompt = build_correction_prompt(source_name="book.md", text_path=Path("book.md"), text="患者头痛。", profile=profile)

    assert "中医参考资料" in prompt
    assert "不得凭中医知识补写" in prompt
    assert "医案、方剂、剂量" in prompt


def test_repair_prompt_accepts_profile_and_preserves_chinese_medicine_rules() -> None:
    profile = get_profile("chinese_medicine_reference")
    prompt = build_repair_prompt(
        source_name="book.md",
        text_path=Path("book.md"),
        text="患者头痛。",
        acceptance_issue={"category": "layout", "reason": "结构不清"},
        profile=profile,
    )

    assert "中医参考资料" in prompt
    assert "不得凭中医知识补写" in prompt
    assert "profile_id: chinese_medicine_reference" in prompt


def test_result_payloads_include_profile_id_and_pass_profile_to_prompt() -> None:
    profile = get_profile("technical_manual")
    correction_client = CapturingClient(
        {
            "status": "draft_ready",
            "corrected_text": "运行 `llmcheck`。",
            "changes": [],
            "unresolved_issues": [],
        }
    )
    acceptance_client = CapturingClient(
        {
            "status": "passed",
            "blocking_issues": [],
            "non_blocking_notes": [],
        }
    )
    repair_client = CapturingClient(
        {
            "status": "repaired",
            "repaired_text": "运行 `llmcheck`。",
            "changes": [],
            "unresolved_issues": [],
        }
    )

    correction = correction_result_payload(
        source_name="manual.md",
        text_path=Path("manual.md"),
        text="运行 llmcheck。",
        client=correction_client,
        model="test-model",
        profile=profile,
    )
    acceptance = acceptance_result_payload(
        source_name="manual.md",
        text_path=Path("manual.md"),
        text="运行 `llmcheck`。",
        client=acceptance_client,
        model="test-model",
        profile=profile,
    )
    repair = repair_result_payload(
        source_name="manual.md",
        text_path=Path("manual.md"),
        text="运行 llmcheck。",
        acceptance_issue={"category": "layout", "reason": "命令格式不清"},
        previous_text="",
        next_text="",
        audit_text="",
        client=repair_client,
        model="test-model",
        profile=profile,
    )

    assert correction["profile_id"] == "technical_manual"
    assert acceptance["profile_id"] == "technical_manual"
    assert repair["profile_id"] == "technical_manual"
    assert "profile_id: technical_manual" in correction_client.prompts[0]
    assert "profile_id: technical_manual" in acceptance_client.prompts[0]
    assert "profile_id: technical_manual" in repair_client.prompts[0]
