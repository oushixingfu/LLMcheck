from __future__ import annotations

from pathlib import Path
import hashlib
import http.client
import json
import os
import subprocess
import threading
import time
import urllib.error
import zipfile

import llmcheck.preprocess as preprocess
from llmcheck.batch import discover_batch_sources, run_batch
from llmcheck import gui_exe
from llmcheck.cli import _print_progress_event, main
from llmcheck.llm import LlmClient, LlmConfig
from llmcheck.model_compare import run_model_compare
from llmcheck.gui import JobStore, _read_mineru_segment_status, _save_uploaded_files, _with_live_progress, render_index_html
from llmcheck.pipeline import LlmCheckSettings, _apply_safe_review_patches, correct_text_concurrently, process_documents
from llmcheck.preflight import preflight_llm_models
from llmcheck.pipeline import split_text_chunks
from llmcheck.preprocess import (
    MinerUClient,
    MinerUTransientError,
    PreprocessSettings,
    SourceKind,
    _run_one_mineru_file,
    _write_mineru_segment_status,
    discover_source_files,
    page_segments,
    prepare_markdown_inputs,
    run_mineru_vlm,
    run_mineru_vlm_for_pdf_chunks,
    run_ppx,
)
from llmcheck.quality import clean_markdown_text, repair_acceptance_locally, quality_errors


class FakeClient:
    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.calls: list[str] = []

    def complete_json(self, prompt: str) -> dict[str, object]:
        self.calls.append(prompt)
        if '"corrected_text"' in prompt:
            return {
                "status": "draft_ready",
                "confidence": 0.9,
                "summary": "correction complete",
                "corrected_text": "# Case\n\nPatient has headache. Use guizhi decoction.\n",
                "changes": [],
                "unresolved_issues": [],
            }
        return {
            "status": "passed" if self.accept else "needs_revision",
            "confidence": 0.9,
            "summary": "deliverable" if self.accept else "needs repair",
            "blocking_issues": [] if self.accept else [{"category": "layout", "reason": "bad paragraph split"}],
            "non_blocking_notes": [],
        }


class ReviewClient:
    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.calls: list[str] = []

    def complete_json(self, prompt: str) -> dict[str, object]:
        self.calls.append(prompt)
        return {
            "status": "reviewed",
            "accepted": self.accept,
            "confidence": 0.9,
            "summary": "review passed" if self.accept else "blocking issue found",
            "issues": []
            if self.accept
            else [
                {
                    "id": "issue-001",
                    "category": "latex_artifact",
                    "severity": "blocking",
                    "location_hint": "body",
                    "excerpt": "$9\\mathrm{g}$",
                    "reason": "unit formula residue",
                    "suggested_action": "clean by deterministic rule or manual review",
                    "safe_fix_type": "rule_fix",
                }
            ],
            "manual_review_notes": [] if self.accept else ["review unit cleanup rule"],
        }


def test_process_documents_uses_fallback_model_when_preferred_unavailable(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "book.md"
    source.write_text("# 医案\n\n桂枝汤用于外感风寒。\n", encoding="utf-8")
    calls: list[tuple[str, str]] = []

    class FakeModelClient:
        def __init__(self, config: LlmConfig) -> None:
            self.config = config
            self._call_count = 0

        @property
        def call_count(self) -> int:
            return self._call_count

        def complete_json(self, prompt: str) -> dict[str, object]:
            self._call_count += 1
            calls.append((self.config.model, self.config.api_url))
            if self.config.model == "mimo-v2.5-pro":
                return {"status": "error", "error": "model temporarily unavailable"}
            return {
                "status": "reviewed",
                "accepted": True,
                "confidence": 0.9,
                "summary": "review passed",
                "issues": [],
                "manual_review_notes": [],
            }

    monkeypatch.setattr("llmcheck.pipeline.LlmClient", FakeModelClient)

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="key",
            llm_model="mimo-v2.5-pro",
            fallback_models="gpt-5.5",
        ),
    )

    row = report["documents"][0]
    call_rows = [json.loads(line) for line in Path(row["llm_calls_report_path"]).read_text(encoding="utf-8").splitlines()]
    assert report["status"] == "passed"
    assert calls[:2] == [
        ("mimo-v2.5-pro", "https://api.iaigc.fun/v1"),
        ("gpt-5.5", "http://127.0.0.1:3022"),
    ]
    assert call_rows[0]["model"] == "gpt-5.5"
    assert call_rows[0]["fallback_used"] is True
    assert call_rows[0]["attempted_models"] == ["mimo-v2.5-pro", "gpt-5.5"]


def test_preflight_llm_models_reports_fallback_availability_without_secrets() -> None:
    calls: list[tuple[str, str]] = []

    class FakeProbeClient:
        def __init__(self, config: LlmConfig) -> None:
            self.config = config

        def complete_json(self, prompt: str) -> dict[str, object]:
            calls.append((self.config.model, self.config.api_url))
            if self.config.model == "mimo-v2.5-pro":
                return {"status": "error", "error": "model unavailable"}
            return {"status": "ok"}

    report = preflight_llm_models(
        LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="secret-key",
            llm_model="mimo-v2.5-pro",
            fallback_models="gpt-5.5",
        ),
        client_factory=FakeProbeClient,
    )

    assert calls == [
        ("mimo-v2.5-pro", "https://api.iaigc.fun/v1"),
        ("gpt-5.5", "http://127.0.0.1:3022"),
    ]
    assert report["status"] == "passed"
    assert report["preferred_available_model"] == "gpt-5.5"
    assert [row["model"] for row in report["models"]] == ["mimo-v2.5-pro", "gpt-5.5"]
    assert [row["available"] for row in report["models"]] == [False, True]
    assert "secret-key" not in json.dumps(report, ensure_ascii=False)


class EchoChunkClient:
    def __init__(self) -> None:
        self.correction_calls = 0
        self.acceptance_calls = 0

    def complete_json(self, prompt: str) -> dict[str, object]:
        if '"corrected_text"' in prompt:
            self.correction_calls += 1
            text = prompt.split("<TEXT_BEGIN>\n", 1)[1].split("\n<TEXT_END>", 1)[0]
            return {
                "status": "draft_ready",
                "confidence": 0.8,
                "summary": "chunk corrected",
                "corrected_text": text.replace("typo", "fixed"),
                "changes": [],
                "unresolved_issues": [],
            }
        self.acceptance_calls += 1
        return {
            "status": "passed",
            "confidence": 0.8,
            "summary": "chunk accepted",
            "blocking_issues": [],
            "non_blocking_notes": [],
        }


class ExplodingClient:
    def complete_json(self, prompt: str) -> dict[str, object]:
        raise AssertionError("cached chunks should not call LLM")


class ReviewButCorrectedClient:
    def complete_json(self, prompt: str) -> dict[str, object]:
        if '"corrected_text"' in prompt:
            text = prompt.split("<TEXT_BEGIN>\n", 1)[1].split("\n<TEXT_END>", 1)[0]
            return {
                "status": "needs_manual_review",
                "confidence": 0.8,
                "summary": "review noted but full correction supplied",
                "corrected_text": text.replace("typo", "fixed"),
                "changes": [],
                "unresolved_issues": [{"location_hint": "chunk", "excerpt": "doubt", "reason": "keep for acceptance"}],
            }
        return {
            "status": "passed",
            "confidence": 0.8,
            "summary": "deliverable",
            "blocking_issues": [],
            "non_blocking_notes": [],
        }


class EmptyMarkupResidueClient:
    def complete_json(self, prompt: str) -> dict[str, object]:
        text = prompt.split("<TEXT_BEGIN>\n", 1)[1].split("\n<TEXT_END>", 1)[0]
        corrected = "" if text.strip() == "<td></td></tr></table>" else text.replace("typo", "fixed")
        return {
            "status": "draft_ready",
            "confidence": 0.9,
            "summary": "chunk cleaned",
            "corrected_text": corrected,
            "changes": [],
            "unresolved_issues": [],
        }

def test_llm_client_retries_invalid_json_message_content(monkeypatch) -> None:
    responses = [
        json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"status":"draft_ready","confidence":0.9,"summary":"bad JSON","corrected_text":"missing quote}'
                        }
                    }
                ]
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "status": "draft_ready",
                                    "confidence": 0.9,
                                    "summary": "second attempt succeeded",
                                    "corrected_text": "valid corrected text",
                                    "changes": [],
                                    "unresolved_issues": [],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
            ensure_ascii=False,
        ),
    ]
    calls = 0

    class FakeResponse:
        def __init__(self, body: str) -> None:
            self.body = body

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return self.body.encode("utf-8")

    def fake_urlopen(*args: object, **kwargs: object) -> FakeResponse:
        nonlocal calls
        body = responses[min(calls, len(responses) - 1)]
        calls += 1
        return FakeResponse(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("llmcheck.llm.time.sleep", lambda *_args, **_kwargs: None)

    client = LlmClient(LlmConfig(api_url="http://llm.test", api_key="key", model="model", timeout_seconds=1))
    result = client.complete_json("璇疯繑鍥?JSON")

    assert result["status"] == "draft_ready"
    assert result["corrected_text"] == "valid corrected text"
    assert calls == 2


class RepairingClient:
    def complete_json(self, prompt: str) -> dict[str, object]:
        if '"corrected_text"' in prompt:
            text = prompt.split("<TEXT_BEGIN>\n", 1)[1].split("\n<TEXT_END>", 1)[0]
            return {
                "status": "draft_ready",
                "confidence": 0.9,
                "summary": "draft kept for repair",
                "corrected_text": text,
                "changes": [],
                "unresolved_issues": [],
            }
        if '"repaired_text"' in prompt:
            text = prompt.split("<TEXT_BEGIN>\n", 1)[1].split("\n<TEXT_END>", 1)[0]
            return {
                "status": "repaired",
                "confidence": 0.9,
                "summary": "missing phrase repaired",
                "repaired_text": text.replace("one two four", "one two three four"),
                "changes": [{"location_hint": "paragraph", "before": "one two four", "after": "one two three four", "reason": "restore missing token"}],
                "unresolved_issues": [],
            }
        text = prompt.split("<TEXT_BEGIN>\n", 1)[1].split("\n<TEXT_END>", 1)[0]
        if "one two three four" in text:
            return {
                "status": "passed",
                "confidence": 0.9,
                "summary": "accepted",
                "blocking_issues": [],
                "non_blocking_notes": [],
            }
        return {
            "status": "needs_revision",
            "confidence": 0.9,
            "summary": "missing phrase remains",
            "blocking_issues": [{"category": "missing_text", "severity": "medium", "location_hint": "paragraph", "excerpt": "one two four", "reason": "missing token", "suggested_action": "repair text"}],
            "non_blocking_notes": [],
        }


class TwoRoundRepairClient:
    def __init__(self) -> None:
        self.repair_calls = 0

    def complete_json(self, prompt: str) -> dict[str, object]:
        text = prompt.split("<TEXT_BEGIN>\n", 1)[1].split("\n<TEXT_END>", 1)[0]
        if '"corrected_text"' in prompt:
            return {
                "status": "draft_ready",
                "confidence": 0.9,
                "summary": "draft ready",
                "corrected_text": text,
                "changes": [],
                "unresolved_issues": [],
            }
        if '"repaired_text"' in prompt:
            self.repair_calls += 1
            repaired_text = text.replace("alpha", "alpha beta") if self.repair_calls == 1 else text.replace("alpha beta", "alpha beta gamma")
            return {
                "status": "repaired",
                "confidence": 0.9,
                "summary": "repair pass complete",
                "repaired_text": repaired_text,
                "changes": [],
                "unresolved_issues": [],
            }
        if "alpha beta gamma" in text:
            return {
                "status": "passed",
                "confidence": 0.9,
                "summary": "accepted",
                "blocking_issues": [],
                "non_blocking_notes": [],
            }
        return {
            "status": "needs_revision",
            "confidence": 0.9,
            "summary": "still incomplete",
            "blocking_issues": [{"category": "missing_text", "severity": "medium", "location_hint": "body", "excerpt": text, "reason": "missing token", "suggested_action": "repair again"}],
            "non_blocking_notes": [],
        }

def test_clean_markdown_text_merges_forced_line_breaks() -> None:
    text = "鎮ｈ€呭ご鐥涘彂鐑剦娴甛n澶勬柟妗傛灊姹ゅ姞鍑忔不鐤椼€俓n"

    cleaned = clean_markdown_text(text)

    assert "bad_control_characters" not in quality_errors(cleaned)


def test_clean_markdown_text_removes_mineru_generated_details_noise() -> None:
    text = "Body\n<details>\n<summary>line</summary>\n\n| x | y |\n|---|---|\n| 0 | 1 |\n</details>\n\n<details>\n<summary>flowchart</summary>\nA --> B\n</details>\n\n<details>\n<summary>natural_image</summary>\nEnglish image description.\n</details>\n\n<details>\n<summary>radar</summary>\nNoise chart text.\n</details>\n"

    cleaned = clean_markdown_text(text)

    assert "<summary>line</summary>" not in cleaned
    assert "| x | y |" not in cleaned
    assert "<summary>flowchart</summary>" not in cleaned
    assert "<summary>natural_image</summary>" not in cleaned
    assert "<summary>radar</summary>" not in cleaned
    assert "English image description" not in cleaned


def test_clean_markdown_text_transcribes_table_image_flowchart_details() -> None:
    text = """Table image:
![table](images/table2.png)

<details>
<summary>flowchart</summary>

```mermaid
flowchart TD
    A["Root"] --> B["Left"]
    A --> C["Right"]
    D["Other"] --> E["Leaf"]
```

</details>
"""

    cleaned = clean_markdown_text(text)

    assert "![table](images/table2.png)" in cleaned
    assert "<summary>flowchart</summary>" not in cleaned
    assert "MinerU flowchart" in cleaned
    assert "| Root | Left |" in cleaned
    assert "| Other | Leaf |" in cleaned


def test_clean_markdown_text_converts_html_table_residue() -> None:
    text = "Before\n<tr><td>Name</td><td>Value</td><td>9g</td></tr>\n<tr><td>Other</td><td>More</td><td>3g</td></tr>\nAfter"

    cleaned = clean_markdown_text(text)

    assert "<td>" not in cleaned
    assert "</tr>" not in cleaned
    assert "| Name | Value | 9g |" in cleaned
    assert "| Other | More | 3g |" in cleaned


def test_clean_markdown_text_normalizes_broken_markdown_tables() -> None:
    text = "| A | B | C | D | E | F |\n\n| r1c1 | r1c2 | r1c3 | r1c4 | wrapped\ncell | r1c6 |\n| bad | row | has | too | many | cells | A | B | C | D | E | F |\n| r2c1 | r2c2 | r2c3 | r2c4 | r2c5 | r2c6 |\n"

    cleaned = clean_markdown_text(text)

    assert "| A | B | C | D | E | F |\n|---|---|---|---|---|---|" in cleaned
    assert "| r1c1 | r1c2 | r1c3 | r1c4 | wrapped cell | r1c6 |" in cleaned
    assert "| bad | row | has | too | many | cells | A | B | C | D | E | F |" not in cleaned
    assert "| r2c1 | r2c2 | r2c3 | r2c4 | r2c5 | r2c6 |" in cleaned

def test_clean_markdown_text_merges_table_continuation_rows() -> None:
    text = "| A | B | C | D | E |\n| one | two |  |  |  |\n| continued | value | 3g |\n"

    cleaned = clean_markdown_text(text)

    assert "| one | two | continued | value | 3g |" in cleaned
    assert "| continued | value | 3g |" not in cleaned.splitlines()


def test_clean_markdown_text_drops_short_table_fragments() -> None:
    text = "| A | B | C | D | E | F |\n| r1c1 | r1c2 | r1c3 | r1c4 | r1c5 | r1c6 |\n| fragment | two |\n"

    cleaned = clean_markdown_text(text)

    assert "| r1c1 | r1c2 | r1c3 | r1c4 | r1c5 | r1c6 |" in cleaned
    assert "| fragment | two |" not in cleaned.splitlines()


def test_clean_markdown_text_resets_table_state_between_sections() -> None:
    text = "| A | B | C | D | E | F |\n| r1c1 | r1c2 | r1c3 | r1c4 | r1c5 | r1c6 |\n\n# Next Section\n\n<table><tr><td>H1</td><td>H2</td><td>H3</td><td>H4</td><td>H5</td></tr><tr><td>a</td><td>b</td><td>c</td><td>d</td><td>e</td></tr></table>\n"

    cleaned = clean_markdown_text(text)

    assert "| H1 | H2 | H3 | H4 | H5 |" in cleaned
    assert "| a | b | c | d | e |" in cleaned


def test_clean_markdown_text_splits_local_structure_glue() -> None:
    text = "Header Shoulder: pain Elbow: sore Wrist: weak"

    cleaned = clean_markdown_text(text)

    assert "Shoulder:\npain" in cleaned
    assert "Elbow:\nsore" in cleaned
    assert "Wrist:\nweak" in cleaned


def test_clean_markdown_text_splits_bibliography_glue() -> None:
    text = "References: 1965 First title 1962 Second title"

    cleaned = clean_markdown_text(text)

    assert "1965\nFirst title" in cleaned
    assert "1962\nSecond title" in cleaned


def test_repair_acceptance_locally_targets_failed_layout_excerpt() -> None:
    text = "Header Shoulder: pain Elbow: sore Wrist: weak\n"
    acceptance = {
        "accepted": False,
        "chunks": [
            {
                "accepted": False,
                "llm_result": {
                    "blocking_issues": [
                        {
                            "category": "layout",
                            "location_hint": "body",
                            "excerpt": text.strip(),
                            "reason": "local structure glued together",
                        }
                    ]
                },
            }
        ],
    }

    repair = repair_acceptance_locally(text, acceptance)

    assert repair["repaired"] is True
    assert repair["issue_count"] == 1
    assert "Shoulder:\npain" in repair["repaired_text"]


def test_safe_review_patch_applies_excerpt_scoped_rule_fix() -> None:
    text = "# Case\n\n瓜蒌 $9\\mathrm{g}$，体温 $40.1^{\\circ} \\mathrm{C}$。\n"
    review = {
        "accepted": False,
        "issues": [
            {
                "id": "issue-001",
                "category": "latex_artifact",
                "severity": "blocking",
                "safe_fix_type": "rule_fix",
                "location_hint": "body",
                "excerpt": "$9\\mathrm{g}$",
                "reason": "unit formula residue",
            },
            {
                "id": "issue-002",
                "category": "latex_artifact",
                "severity": "major",
                "safe_fix_type": "rule_fix",
                "location_hint": "body",
                "excerpt": "$40.1^{\\circ} \\mathrm{C}$",
                "reason": "temperature formula residue",
            },
        ],
    }

    patch = _apply_safe_review_patches(text, review)

    assert patch["accepted"] is True
    assert patch["status"] == "patched"
    assert patch["applied_patch_count"] == 2
    assert patch["unresolved_issues"] == []
    assert "9g" in patch["patched_text"]
    assert "40.1℃" in patch["patched_text"]
    assert "\\mathrm" not in patch["patched_text"]

def test_process_documents_review_first_blocks_final_outputs_on_review_issue(tmp_path: Path) -> None:
    source = tmp_path / "book.md"
    source.write_text("# 鍖绘\n\n鐡滆拰 $9\\mathrm{g}$銆俓n", encoding="utf-8")

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        client=ReviewClient(accept=False),
    )

    row = report["documents"][0]
    review_report = json.loads(Path(row["review_report_path"]).read_text(encoding="utf-8"))
    assert row["llm_calls_report_path"]
    llm_call_rows = [
        json.loads(line)
        for line in Path(row["llm_calls_report_path"]).read_text(encoding="utf-8").splitlines()
    ]
    assert report["status"] == "review_required"
    assert row["status"] == "llm_review_failed"
    assert row["final_markdown_path"] == ""
    assert row["text_pdf_path"] == ""
    assert row["correction_report_path"] == ""
    assert row["acceptance_report_path"] == ""
    assert Path(row["draft_path"]).exists()
    assert review_report["accepted"] is False
    assert review_report["issues"][0]["category"] == "latex_artifact"
    assert llm_call_rows[0]["stage"] == "llm_review"
    assert llm_call_rows[0]["status"] == "ok"
    process_dir = tmp_path / "out" / "process"
    assert (process_dir / "run_events.jsonl").exists()
    assert (process_dir / "heartbeat.json").exists()
    assert (process_dir / "cost_report.json").exists()
    assert (process_dir / "stage_timings.json").exists()
    assert (process_dir / "llm_calls.jsonl").exists()
    heartbeat = json.loads((process_dir / "heartbeat.json").read_text(encoding="utf-8"))
    cost_report = json.loads((process_dir / "cost_report.json").read_text(encoding="utf-8"))
    assert heartbeat["stage"] == "finished"
    assert cost_report["model"] == "model"
    assert cost_report["llm_call_count"] == 1


def test_process_documents_review_first_blocks_before_llm_on_pre_llm_quality_error(tmp_path: Path) -> None:
    source = tmp_path / "book.md"
    source.write_text("# 标题\n\n这是锟斤拷乱码文本，含有�替换符。\n", encoding="utf-8")

    class ExplodingReviewClient:
        def complete_json(self, prompt: str) -> dict[str, object]:
            raise AssertionError("pre-LLM quality gate should block before LLM review")

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        client=ExplodingReviewClient(),
    )

    row = report["documents"][0]
    final_acceptance = json.loads(Path(row["final_acceptance_report_path"]).read_text(encoding="utf-8"))
    quality = json.loads((tmp_path / "out" / "process" / "reports" / "book.quality.json").read_text(encoding="utf-8"))
    assert report["status"] == "review_required"
    assert row["status"] == "pre_llm_quality_failed"
    assert row["final_markdown_path"] == ""
    assert row["text_pdf_path"] == ""
    assert row["review_report_path"] == ""
    assert Path(row["draft_path"]).exists()
    assert final_acceptance["accepted"] is False
    assert "mojibake" in final_acceptance["blocking_errors"]
    # Replacement chars may be removed by deterministic cleaning before the gate;
    # mojibake residue is still a hard block.
    assert quality["pre_llm_gate"]["accepted"] is False


def test_process_documents_review_first_finalizes_structure_before_llm(tmp_path: Path) -> None:
    source = tmp_path / "book.md"
    source.write_text(
        "# 标题\n\n页眉重复行\n正文第一段。\n\n页眉重复行\n正文第二段。\n\n页眉重复行\n正文第三段。\n",
        encoding="utf-8",
    )

    class AssertingReviewClient:
        def __init__(self) -> None:
            self.calls = 0

        def complete_json(self, prompt: str) -> dict[str, object]:
            self.calls += 1
            assert "页眉重复行" not in prompt
            return {
                "status": "reviewed",
                "accepted": True,
                "confidence": 0.9,
                "summary": "review passed",
                "issues": [],
                "manual_review_notes": [],
            }

    client = AssertingReviewClient()

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        client=client,
    )

    row = report["documents"][0]
    draft_text = Path(row["draft_path"]).read_text(encoding="utf-8")
    final_text = Path(row["final_markdown_path"]).read_text(encoding="utf-8")
    assert report["status"] == "passed"
    assert client.calls == 1
    assert "页眉重复行" not in draft_text
    assert "页眉重复行" not in final_text


def test_process_documents_local_gate_delivers_without_llm_when_quality_gate_passes(tmp_path: Path) -> None:
    source = tmp_path / "book.md"
    source.write_text(
        "# 标题\n\n页眉重复行\n正文第一段。\n\n页眉重复行\n正文第二段。\n\n页眉重复行\n正文第三段。\n",
        encoding="utf-8",
    )

    class ExplodingClient:
        def complete_json(self, prompt: str) -> dict[str, object]:
            raise AssertionError("local-gate mode must not call LLM")

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(llm_api_url="", llm_api_key="", llm_model="model", llm_mode="local-gate"),
        client=ExplodingClient(),
    )

    row = report["documents"][0]
    final_text = Path(row["final_markdown_path"]).read_text(encoding="utf-8")
    quality = json.loads((tmp_path / "out" / "process" / "reports" / "book.quality.json").read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert row["status"] == "passed"
    assert row["review_report_path"] == ""
    assert row["correction_report_path"] == ""
    assert quality["pre_llm_gate"]["accepted"] is True
    assert "页眉重复行" not in final_text
    assert Path(row["text_pdf_path"]).read_bytes().startswith(b"%PDF-")


def test_process_documents_keeps_draft_but_not_final_when_acceptance_fails(tmp_path: Path) -> None:
    source = tmp_path / "book.md"
    source.write_text("body", encoding="utf-8")

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="key",
            llm_model="model",
            llm_mode="legacy-correction",
        ),
        client=FakeClient(accept=False),
    )

    row = report["documents"][0]
    assert report["status"] == "review_required"
    assert row["status"] == "acceptance_failed"
    assert Path(row["draft_path"]).exists()
    assert row["final_markdown_path"] == ""
    assert not list((tmp_path / "out" / "md").glob("*.md"))


def test_process_documents_uses_preprocessed_markdown_inputs(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    generated = tmp_path / "generated.md"
    generated.write_text("generated markdown body", encoding="utf-8")
    pdf_titles: list[str] = []

    def fake_write_text_pdf(path: Path, *, title: str, text: str) -> None:
        pdf_titles.append(title)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr("llmcheck.pipeline.write_text_pdf", fake_write_text_pdf)

    def fake_preprocess(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> list[Path]:
        assert input_path == source
        assert settings.mineru_model == "vlm"
        assert settings.pdf_page_chunk_size == 30
        assert settings.mineru_request_timeout_seconds == 60
        return [generated]

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="key",
            llm_model="model",
            llm_mode="legacy-correction",
            mineru_api_key="mineru-key",
        ),
        client=FakeClient(),
        preprocess_runner=fake_preprocess,
    )

    assert report["status"] == "passed"
    assert report["documents"][0]["source_path"] == str(generated)
    assert pdf_titles == ["source"]


def test_process_documents_chunks_llm_correction_and_acceptance(tmp_path: Path) -> None:
    paragraphs = [f"para {index}: " + ("typo needs punctuation and paragraph repair. " * 80) for index in range(1, 7)]
    source = tmp_path / "long.md"
    source.write_text("\n\n".join(paragraphs), encoding="utf-8")
    client = EchoChunkClient()

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="key",
            llm_model="model",
            llm_mode="legacy-correction",
            concurrency=3,
            llm_chunk_chars=2000,
        ),
        client=client,
    )

    row = report["documents"][0]
    final_text = Path(row["final_markdown_path"]).read_text(encoding="utf-8")
    correction = json.loads(Path(row["correction_report_path"]).read_text(encoding="utf-8"))
    acceptance = json.loads(Path(row["acceptance_report_path"]).read_text(encoding="utf-8"))
    correction_chunks = list((tmp_path / "out" / "process" / "reports" / "long.llm_correction_chunks").glob("*.json"))
    acceptance_chunks = list((tmp_path / "out" / "process" / "reports" / "long.llm_acceptance_chunks").glob("*.json"))
    assert report["status"] == "passed"
    assert client.correction_calls > 1
    assert client.acceptance_calls > 1
    assert "fixed" in final_text
    assert "typo" not in final_text
    assert correction["chunk_count"] == client.correction_calls
    assert acceptance["chunk_count"] == client.acceptance_calls
    assert len(correction_chunks) == client.correction_calls
    assert len(acceptance_chunks) == client.acceptance_calls


def test_process_documents_allows_review_correction_when_text_is_returned(tmp_path: Path) -> None:
    source = tmp_path / "book.md"
    source.write_text("typo", encoding="utf-8")

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model", llm_mode="legacy-correction"),
        client=ReviewButCorrectedClient(),
    )

    row = report["documents"][0]
    correction = json.loads(Path(row["correction_report_path"]).read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert correction["chunks"][0]["requires_review"] is True
    assert "fixed" in Path(row["final_markdown_path"]).read_text(encoding="utf-8")


def test_correct_text_concurrently_allows_empty_markup_residue_chunk(tmp_path: Path) -> None:
    source = tmp_path / "book.md"
    text = "\n\n".join(["before typo " * 400, "<td></td></tr></table>", "after typo " * 400])
    source.write_text(text, encoding="utf-8")

    correction = correct_text_concurrently(
        source_name=source.name,
        text_path=source,
        text=text,
        client=EmptyMarkupResidueClient(),
        model="model",
        concurrency=2,
        max_chars=40,
        chunk_report_dir=tmp_path / "chunks",
    )

    empty_chunk = next(chunk for chunk in correction["chunks"] if chunk["input_chars"] == len("<td></td></tr></table>"))
    assert correction["draft_ready"] is True
    assert correction["status"] == "draft_ready"
    assert empty_chunk["draft_ready"] is True
    assert empty_chunk["empty_corrected_text_allowed"] is True
    assert "<td>" not in correction["corrected_text"]
    assert "before fixed" in correction["corrected_text"]
    assert "after fixed" in correction["corrected_text"]


def test_process_documents_repairs_failed_acceptance_chunk_then_rechecks(tmp_path: Path) -> None:
    source = tmp_path / "book.md"
    source.write_text("one two four", encoding="utf-8")

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model", llm_mode="legacy-correction"),
        client=RepairingClient(),
    )

    row = report["documents"][0]
    final_text = Path(row["final_markdown_path"]).read_text(encoding="utf-8")
    repair = json.loads(Path(row["repair_report_path"]).read_text(encoding="utf-8"))
    reports_dir = tmp_path / "out" / "process" / "reports"
    assert report["status"] == "passed"
    assert "one two three four" in final_text
    assert repair["repaired"] is True
    assert (reports_dir / "book.llm_acceptance_chunks").exists()
    assert (reports_dir / "book.llm_acceptance_round_1_chunks").exists()


def test_process_documents_applies_multiple_acceptance_repair_rounds(tmp_path: Path) -> None:
    source = tmp_path / "book.md"
    source.write_text("alpha", encoding="utf-8")
    client = TwoRoundRepairClient()

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="key",
            llm_model="model",
            llm_mode="legacy-correction",
            acceptance_repair_rounds=2,
        ),
        client=client,
    )

    row = report["documents"][0]
    final_text = Path(row["final_markdown_path"]).read_text(encoding="utf-8")
    repair = json.loads(Path(row["repair_report_path"]).read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert final_text.strip() == "alpha beta gamma"
    assert client.repair_calls == 2
    assert repair["round"] == 2


def test_process_documents_allows_finalization_for_layout_only_acceptance_failures(tmp_path: Path) -> None:
    class LayoutOnlyAcceptanceClient:
        def complete_json(self, prompt: str) -> dict[str, object]:
            text = prompt.split("<TEXT_BEGIN>\n", 1)[1].split("\n<TEXT_END>", 1)[0]
            if '"corrected_text"' in prompt:
                return {
                    "status": "draft_ready",
                    "confidence": 0.9,
                    "summary": "draft ready",
                    "corrected_text": text,
                    "changes": [],
                    "unresolved_issues": [],
                }
            return {
                "status": "needs_revision",
                "confidence": 0.9,
                "summary": "layout-only physical line break",
                "blocking_issues": [
                    {
                        "category": "layout",
                        "severity": "medium",
                        "location_hint": "body",
                        "excerpt": "This paragraph is clearly a continuous sentence,\nbut OCR split it",
                        "reason": "physical line break residue",
                        "suggested_action": "merge the wrapped line",
                    }
                ],
                "non_blocking_notes": [],
            }

    source = tmp_path / "book.md"
    source.write_text(
        "# Title\n\n"
        "This paragraph is clearly a continuous sentence,\n"
        "but OCR split it across a physical row.\n",
        encoding="utf-8",
    )

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="key",
            llm_model="model",
            llm_mode="legacy-correction",
            acceptance_repair_rounds=0,
        ),
        client=LayoutOnlyAcceptanceClient(),
    )

    row = report["documents"][0]
    final_text = Path(row["final_markdown_path"]).read_text(encoding="utf-8")
    final_acceptance = json.loads(Path(row["final_acceptance_report_path"]).read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert "sentence," in final_text
    assert "but OCR split" in final_text
    assert "sentence,\nbut OCR split" not in final_text
    assert final_acceptance["accepted"] is True


def test_process_documents_reuses_successful_chunk_reports(tmp_path: Path) -> None:
    source = tmp_path / "book.md"
    source.write_text("typo", encoding="utf-8")
    output = tmp_path / "out"
    first = process_documents(
        input_path=source,
        output_dir=output,
        settings=LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="key",
            llm_model="model",
            llm_mode="legacy-correction",
            llm_chunk_chars=2000,
        ),
        client=EchoChunkClient(),
    )
    second = process_documents(
        input_path=source,
        output_dir=output,
        settings=LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="key",
            llm_model="model",
            llm_mode="legacy-correction",
            llm_chunk_chars=2000,
        ),
        client=ExplodingClient(),
    )

    assert first["status"] == "passed"
    assert second["status"] == "passed"


def test_llm_client_retries_http_503(monkeypatch) -> None:
    calls = 0

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"choices":[{"message":{"content":"{\\"status\\":\\"passed\\"}"}}]}'

    def fake_urlopen(*args: object, **kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError("https://llm.test", 503, "Service Unavailable", {}, None)
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    client = LlmClient(LlmConfig(api_url="https://llm.test", api_key="key", model="model", timeout_seconds=1))

    assert client.complete_json("prompt")["status"] == "passed"
    assert calls == 2


def test_llm_client_retries_remote_disconnect(monkeypatch) -> None:
    calls = 0

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"choices":[{"message":{"content":"{\\"status\\":\\"passed\\"}"}}]}'

    def fake_urlopen(*args: object, **kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise http.client.RemoteDisconnected("Remote end closed connection without response")
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    client = LlmClient(LlmConfig(api_url="https://llm.test", api_key="key", model="model", timeout_seconds=1))

    assert client.complete_json("prompt")["status"] == "passed"
    assert calls == 2


def test_llm_client_curl_transport_enforces_timeout_and_retries(monkeypatch) -> None:
    calls = 0

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.TimeoutExpired(cmd="curl", timeout=1)
        return subprocess.CompletedProcess(
            args=["curl"],
            returncode=0,
            stdout='{"choices":[{"message":{"content":"{\\"status\\":\\"passed\\"}"}}]}\nHTTP_STATUS:200',
            stderr="",
        )

    monkeypatch.setenv("LLMCHECK_LLM_TRANSPORT", "curl")
    monkeypatch.setattr("llmcheck.llm.subprocess.run", fake_run)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    client = LlmClient(LlmConfig(api_url="https://llm.test", api_key="key", model="model", timeout_seconds=1))

    assert client.complete_json("prompt")["status"] == "passed"
    assert calls == 2


def test_llm_client_respects_configured_retry_count(monkeypatch) -> None:
    calls = 0

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        raise subprocess.TimeoutExpired(cmd="curl", timeout=1)

    monkeypatch.setenv("LLMCHECK_LLM_TRANSPORT", "curl")
    monkeypatch.setenv("LLMCHECK_LLM_RETRIES", "1")
    monkeypatch.setattr("llmcheck.llm.subprocess.run", fake_run)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    client = LlmClient(LlmConfig(api_url="https://llm.test", api_key="key", model="model", timeout_seconds=1))

    result = client.complete_json("prompt")

    assert result["status"] == "error"
    assert calls == 1


def test_llm_client_stops_when_call_budget_is_exceeded(monkeypatch) -> None:
    calls = 0

    def fake_complete(prompt: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"status": "passed"}

    client = LlmClient(LlmConfig(api_url="https://llm.test", api_key="key", model="model", max_calls_per_book=1))
    monkeypatch.setattr(client, "_complete_json_with_urllib", fake_complete)

    assert client.complete_json("first")["status"] == "passed"
    result = client.complete_json("second")

    assert result["status"] == "error"
    assert result["code"] == "llm_call_budget_exceeded"
    assert calls == 1
    assert client.call_count == 1


def test_discover_source_files_accepts_markdown_pdf_images_and_office(tmp_path: Path) -> None:
    for name in ["a.md", "b.pdf", "c.png", "d.docx", "e.xlsx", "skip.txt"]:
        (tmp_path / name).write_text("x", encoding="utf-8")

    rows = discover_source_files(tmp_path)

    assert [row.path.name for row in rows] == ["a.md", "b.pdf", "c.png", "d.docx", "e.xlsx"]
    assert [row.kind for row in rows] == [
        SourceKind.MARKDOWN,
        SourceKind.PDF,
        SourceKind.MINERU_ONLY,
        SourceKind.WORD,
        SourceKind.MINERU_ONLY,
    ]


def test_prepare_markdown_inputs_writes_existing_md_acquisition_manifest(tmp_path: Path) -> None:
    source = tmp_path / "100医案.md"
    source.write_text("# 医案\n\n原始 Markdown。\n", encoding="utf-8")
    output = tmp_path / "out"
    converter_calls: list[str] = []

    def fake_ppx(path: Path, *, output_dir: Path, settings: PreprocessSettings) -> Path:
        converter_calls.append("ppx")
        raise AssertionError("Markdown input must not call PPX")

    def fake_mineru(files: list[Path], *, output_dir: Path, settings: PreprocessSettings) -> Path:
        converter_calls.append("mineru")
        raise AssertionError("Markdown input must not call MinerU")

    rows = prepare_markdown_inputs(
        input_path=source,
        output_dir=output,
        settings=PreprocessSettings(mineru_api_key="token"),
        ppx_runner=fake_ppx,
        mineru_runner=fake_mineru,
    )

    manifest = json.loads((output / "preprocess" / "100医案" / "preprocess_manifest.json").read_text(encoding="utf-8"))
    formal = output / "preprocess" / "100医案" / "cross" / "initial.md"
    assert rows == [formal]
    assert converter_calls == []
    assert formal.exists()
    source_text = source.read_text(encoding="utf-8")
    formal_text = formal.read_text(encoding="utf-8")
    assert formal_text == source_text or formal_text == (source_text if source_text.endswith("\n") else source_text + "\n")
    assert manifest["kind"] == "markdown"
    assert manifest["acquisition_mode"] == "existing_md"
    assert Path(manifest["formal_markdown"]).name == "initial.md"
    assert "cross_report" in manifest
    assert manifest["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_page_segments_split_large_pdf_into_200_page_chunks() -> None:
    assert page_segments(401, max_pages=200) == [(1, 200), (201, 400), (401, 401)]
    assert page_segments(200, max_pages=200) == [(1, 200)]


def test_split_text_chunks_preserves_order() -> None:
    text = "\n\n".join([f"para {index} " + ("body " * 300) for index in range(1, 5)])

    chunks = split_text_chunks(text, max_chars=2000)

    assert len(chunks) > 1
    assert [chunk.index for chunk in chunks] == list(range(1, len(chunks) + 1))
    assert [chunk.total for chunk in chunks] == [len(chunks)] * len(chunks)
    assert "para 1" in chunks[0].text
    assert "para 4" in chunks[-1].text


def test_split_text_chunks_allows_one_thousand_char_chunks() -> None:
    chunks = split_text_chunks("x" * 1500, max_chars=1000)

    assert len(chunks) == 2
    assert [len(chunk.text) for chunk in chunks] == [1000, 500]


def test_split_text_chunks_keeps_details_blocks_intact() -> None:
    details = "<details>\n<summary>flowchart</summary>\n\n" + ("A --> B\n" * 400) + "\n</details>"
    text = "before\n" + details + "\nafter"

    chunks = split_text_chunks(text, max_chars=2000)

    details_chunks = [chunk for chunk in chunks if "<details>" in chunk.text or "</details>" in chunk.text]
    assert len(details_chunks) == 1
    assert "<details>" in details_chunks[0].text
    assert "</details>" in details_chunks[0].text


def test_prepare_markdown_inputs_runs_pdf_ppx_and_mineru_vlm(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    calls: list[tuple[str, object]] = []

    def fake_page_count(path: Path) -> int:
        assert path == pdf
        return 401

    def fake_split(path: Path, *, output_dir: Path, max_pages: int) -> list[Path]:
        calls.append(("split", max_pages))
        paths = [output_dir / "seg-001.pdf", output_dir / "seg-002.pdf", output_dir / "seg-003.pdf"]
        for item in paths:
            item.parent.mkdir(parents=True, exist_ok=True)
            item.write_bytes(b"%PDF-1.4\n")
        return paths

    def fake_ppx(path: Path, *, output_dir: Path, settings: PreprocessSettings) -> Path:
        calls.append(("ppx", path.name))
        out = output_dir / "ppx.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("ppx text", encoding="utf-8")
        return out

    def fake_mineru(files: list[Path], *, output_dir: Path, settings: PreprocessSettings) -> Path:
        calls.append(("mineru_model", settings.mineru_model))
        calls.append(("mineru_files", [path.name for path in files]))
        out = output_dir / "mineru_vlm.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("mineru text", encoding="utf-8")
        return out

    rows = prepare_markdown_inputs(
        input_path=pdf,
        output_dir=tmp_path / "out",
        settings=PreprocessSettings(mineru_api_key="token", enable_ppx=True, mineru_fallback="ppx"),
        page_count_reader=fake_page_count,
        pdf_splitter=fake_split,
        ppx_runner=fake_ppx,
        mineru_runner=fake_mineru,
    )

    assert rows == [tmp_path / "out" / "preprocess" / "book" / "cross" / "initial.md"]
    assert ("split", 30) in calls
    assert ("ppx", "book.pdf") in calls
    assert ("mineru_model", "vlm") in calls
    assert ("mineru_files", ["seg-001.pdf", "seg-002.pdf", "seg-003.pdf"]) in calls


def test_pdf_page_count_and_split_fallback_to_pypdf(tmp_path: Path, monkeypatch) -> None:
    from pypdf import PdfReader, PdfWriter

    source = tmp_path / "book.pdf"
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=72, height=72)
    with source.open("wb") as handle:
        writer.write(handle)

    def fake_run(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("missing pdf tool")

    monkeypatch.setattr(preprocess.subprocess, "run", fake_run)

    assert preprocess.pdf_page_count(source) == 3
    segments = preprocess.split_pdf_for_mineru(source, output_dir=tmp_path / "segments", max_pages=2)

    assert [preprocess.pdf_page_count(path) for path in segments] == [2, 1]
    assert [path.name for path in segments] == ["segment_0001_0002.pdf", "segment_0003_0003.pdf"]


def test_pdf_page_count_falls_back_on_pdfinfo_timeout(tmp_path: Path, monkeypatch) -> None:
    from pypdf import PdfWriter

    source = tmp_path / "slow.pdf"
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=72, height=72)
    with source.open("wb") as handle:
        writer.write(handle)

    def fake_run(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="pdfinfo", timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(preprocess.subprocess, "run", fake_run)

    assert preprocess.pdf_page_count(source) == 2


def test_prepare_markdown_inputs_uses_ppx_fallback_when_pdf_mineru_fails(tmp_path: Path) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    output = tmp_path / "out"
    calls: list[tuple[str, str]] = []

    def fake_pages(path: Path) -> int:
        return 12

    def fake_ppx(path: Path, *, output_dir: Path, settings: PreprocessSettings) -> Path:
        calls.append(("ppx", path.name))
        out = output_dir / "clean" / "ppx.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("ppx fallback text", encoding="utf-8")
        return out

    def fake_mineru(files: list[Path], *, output_dir: Path, settings: PreprocessSettings) -> Path:
        calls.append(("mineru", ",".join(path.name for path in files)))
        raise MinerUTransientError("model temporarily unavailable")

    rows = prepare_markdown_inputs(
        input_path=source,
        output_dir=output,
        settings=PreprocessSettings(mineru_api_key="token", enable_ppx=True, mineru_fallback="ppx"),
        page_count_reader=fake_pages,
        ppx_runner=fake_ppx,
        mineru_runner=fake_mineru,
    )

    assert rows == [output / "preprocess" / "book" / "cross" / "initial.md"]
    assert ("mineru", "book.pdf") in calls
    assert ("ppx", "book.pdf") in calls
    manifest = json.loads((output / "preprocess" / "book" / "preprocess_manifest.json").read_text(encoding="utf-8"))
    assert manifest["acquisition_mode"] == "ppx_fallback"
    assert Path(manifest["formal_markdown"]).name == "initial.md"
    assert Path(manifest["ppx_markdown"]).name == "ppx.md"
    assert "model temporarily unavailable" in manifest["mineru_error"]


def test_prepare_markdown_inputs_uses_ppx_fallback_when_image_mineru_fails(tmp_path: Path) -> None:
    source = tmp_path / "scan.png"
    source.write_bytes(b"png")
    output = tmp_path / "out"

    def fake_ppx(path: Path, *, output_dir: Path, settings: PreprocessSettings) -> Path:
        out = output_dir / "clean" / "ppx.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("ppx fallback image text", encoding="utf-8")
        return out

    def fake_mineru(files: list[Path], *, output_dir: Path, settings: PreprocessSettings) -> Path:
        raise MinerUTransientError("queue full")

    rows = prepare_markdown_inputs(
        input_path=source,
        output_dir=output,
        settings=PreprocessSettings(mineru_api_key="token", enable_ppx=True, mineru_fallback="ppx"),
        ppx_runner=fake_ppx,
        mineru_runner=fake_mineru,
    )

    assert rows == [output / "preprocess" / "scan" / "cross" / "initial.md"]
    manifest = json.loads((output / "preprocess" / "scan" / "preprocess_manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == SourceKind.MINERU_ONLY.value
    assert manifest["acquisition_mode"] == "ppx_fallback"
    assert Path(manifest["formal_markdown"]).name == "initial.md"


def test_prepare_markdown_inputs_sends_word_directly_to_mineru(tmp_path: Path) -> None:
    source = tmp_path / "book.docx"
    source.write_bytes(b"docx")
    output = tmp_path / "out"
    calls: list[tuple[str, object]] = []

    def fake_page_count(path: Path) -> int:
        calls.append(("page_count", path.name))
        raise AssertionError("Word inputs should be sent directly to MinerU")

    def fake_ppx(path: Path, *, output_dir: Path, settings: PreprocessSettings) -> Path:
        calls.append(("ppx", path.name))
        raise AssertionError("Word inputs should not need PPX when MinerU succeeds")

    def fake_mineru(files: list[Path], *, output_dir: Path, settings: PreprocessSettings) -> Path:
        calls.append(("mineru_files", [path.name for path in files]))
        out = output_dir / "mineru_vlm.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("mineru word text", encoding="utf-8")
        return out

    rows = prepare_markdown_inputs(
        input_path=source,
        output_dir=output,
        settings=PreprocessSettings(mineru_api_key="token"),
        page_count_reader=fake_page_count,
        ppx_runner=fake_ppx,
        mineru_runner=fake_mineru,
    )

    assert rows == [output / "preprocess" / "book" / "cross" / "initial.md"]
    # Preferred path may try Word→PDF first; if page-count/conversion fails, fall back to whole-file MinerU.
    assert ("mineru_files", ["book.docx"]) in calls
    assert not any(call[0] == "ppx" for call in calls)
    manifest = json.loads((output / "preprocess" / "book" / "preprocess_manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == SourceKind.WORD.value
    assert manifest["acquisition_mode"] in {"mineru_only", "selected_mineru", "mineru"}
    assert Path(manifest["formal_markdown"]).name == "initial.md"


def test_prepare_markdown_inputs_starts_pdf_ppx_and_mineru_together(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    ppx_started = threading.Event()
    mineru_started = threading.Event()

    def fake_page_count(path: Path) -> int:
        assert path == pdf
        return 1

    def fake_ppx(path: Path, *, output_dir: Path, settings: PreprocessSettings) -> Path:
        ppx_started.set()
        assert mineru_started.wait(timeout=1), "MinerU should start before PPX finishes"
        out = output_dir / "ppx.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("ppx text", encoding="utf-8")
        return out

    def fake_mineru(files: list[Path], *, output_dir: Path, settings: PreprocessSettings) -> Path:
        assert ppx_started.wait(timeout=1), "PPX should start before MinerU records work"
        mineru_started.set()
        out = output_dir / "mineru_vlm.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("mineru text", encoding="utf-8")
        return out

    rows = prepare_markdown_inputs(
        input_path=pdf,
        output_dir=tmp_path / "out",
        settings=PreprocessSettings(mineru_api_key="token", enable_ppx=True, mineru_fallback="ppx"),
        page_count_reader=fake_page_count,
        ppx_runner=fake_ppx,
        mineru_runner=fake_mineru,
    )

    assert rows == [tmp_path / "out" / "preprocess" / "book" / "cross" / "initial.md"]


def test_prepare_markdown_inputs_skips_ppx_by_default(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    ppx_calls = 0

    def fake_page_count(path: Path) -> int:
        return 1

    def fake_ppx(path: Path, *, output_dir: Path, settings: PreprocessSettings) -> Path:
        nonlocal ppx_calls
        ppx_calls += 1
        out = output_dir / "clean" / "ppx.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("ppx text", encoding="utf-8")
        return out

    def fake_mineru(files: list[Path], *, output_dir: Path, settings: PreprocessSettings) -> Path:
        out = output_dir / "mineru_vlm.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("mineru text", encoding="utf-8")
        return out

    rows = prepare_markdown_inputs(
        input_path=pdf,
        output_dir=tmp_path / "out",
        settings=PreprocessSettings(mineru_api_key="token"),
        page_count_reader=fake_page_count,
        ppx_runner=fake_ppx,
        mineru_runner=fake_mineru,
    )

    assert rows == [tmp_path / "out" / "preprocess" / "book" / "cross" / "initial.md"]
    assert ppx_calls == 0
    assert not (tmp_path / "out" / "preprocess" / "book" / "ppx").exists()
    manifest = json.loads((tmp_path / "out" / "preprocess" / "book" / "preprocess_manifest.json").read_text(encoding="utf-8"))
    assert manifest["acquisition_mode"] in {"mineru_only", "selected_mineru"}


def test_prepare_markdown_inputs_returns_after_mineru_without_waiting_for_ppx(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    ppx_started = threading.Event()
    release_ppx = threading.Event()
    ppx_finished = threading.Event()
    returned = threading.Event()
    errors: list[BaseException] = []
    rows: list[Path] = []

    def fake_page_count(path: Path) -> int:
        assert path == pdf
        return 1

    def fake_ppx(path: Path, *, output_dir: Path, settings: PreprocessSettings) -> Path:
        ppx_started.set()
        assert release_ppx.wait(timeout=2), "test should release PPX after prepare returns"
        out = output_dir / "clean" / "ppx.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("ppx text", encoding="utf-8")
        ppx_finished.set()
        return out

    def fake_mineru(files: list[Path], *, output_dir: Path, settings: PreprocessSettings) -> Path:
        assert ppx_started.wait(timeout=1), "PPX audit should start before MinerU returns"
        out = output_dir / "mineru_vlm.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("mineru text", encoding="utf-8")
        return out

    def run_prepare() -> None:
        try:
            rows.extend(
                prepare_markdown_inputs(
                    input_path=pdf,
                    output_dir=tmp_path / "out",
                    settings=PreprocessSettings(mineru_api_key="token", enable_ppx=True, mineru_fallback="ppx"),
                    page_count_reader=fake_page_count,
                    ppx_runner=fake_ppx,
                    mineru_runner=fake_mineru,
                )
            )
            returned.set()
        except BaseException as error:  # noqa: BLE001 - preserve assertion from worker thread for the main test.
            errors.append(error)
            returned.set()

    prepare_thread = threading.Thread(target=run_prepare)
    prepare_thread.start()
    try:
        assert returned.wait(timeout=1), "prepare should return after MinerU without waiting for PPX"
        assert not errors
        expected_ppx = tmp_path / "out" / "preprocess" / "book" / "ppx" / "clean" / "ppx.md"
        assert rows == [tmp_path / "out" / "preprocess" / "book" / "cross" / "initial.md"]
        assert not expected_ppx.exists()
        status = json.loads((tmp_path / "out" / "preprocess" / "book" / "ppx" / "ppx_audit_status.json").read_text(encoding="utf-8"))
        assert status["status"] == "running"
        assert status["pid"] == os.getpid()
        assert status["thread_name"].startswith("llmcheck-ppx-audit-book")
        assert status["updated_at"]
        release_ppx.set()
        assert ppx_finished.wait(timeout=1)
        assert expected_ppx.read_text(encoding="utf-8") == "ppx text"
        completed_status = {}
        for _ in range(20):
            try:
                completed_status = json.loads((tmp_path / "out" / "preprocess" / "book" / "ppx" / "ppx_audit_status.json").read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                time.sleep(0.05)
                continue
            if completed_status["status"] == "completed":
                break
            time.sleep(0.05)
        assert completed_status["status"] == "completed"
        assert completed_status["pid"] == os.getpid()
    finally:
        release_ppx.set()
        prepare_thread.join(timeout=1)


def test_run_ppx_reuses_existing_clean_markdown(tmp_path: Path) -> None:
    cached = tmp_path / "ppx" / "clean" / "ppx.md"
    cached.parent.mkdir(parents=True)
    cached.write_text("cached ppx text", encoding="utf-8")

    result = run_ppx(
        tmp_path / "book.pdf",
        output_dir=tmp_path / "ppx",
        settings=PreprocessSettings(ppx_command="/missing/ppx"),
    )

    assert result == cached
    assert result.read_text(encoding="utf-8") == "cached ppx text"


def test_run_ppx_uses_configured_local_ocr_flags(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF")
    ppx_command = tmp_path / "ppx"
    ppx_command.write_text("#!/bin/sh\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        raw_dir = Path(command[command.index("--out-dir") + 1])
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "book.md").write_text("ppx text", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("llmcheck.preprocess.subprocess.run", fake_run)

    result = run_ppx(
        source,
        output_dir=tmp_path / "ppx-out",
        settings=PreprocessSettings(
            ppx_command=str(ppx_command),
            ppx_backend="default",
            ppx_ocr="yes",
            ppx_formula="auto",
        ),
    )

    assert result.read_text(encoding="utf-8") == "ppx text"
    command = captured["command"]
    assert isinstance(command, list)
    assert command[command.index("--backend") + 1] == "default"
    assert command[command.index("--ocr") + 1] == "yes"
    assert command[command.index("--formula") + 1] == "auto"


def test_prepare_markdown_inputs_uses_ppx_fallback_for_pdf_without_mineru_key(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF")
    split_calls: list[int] = []
    mineru_calls: list[list[str]] = []
    ppx_calls = 0

    def fake_page_count(path: Path) -> int:
        assert path == pdf
        return 61

    def fake_split(path: Path, *, output_dir: Path, max_pages: int) -> list[Path]:
        split_calls.append(max_pages)
        raise AssertionError("missing MinerU key should avoid PDF splitting")

    def fake_ppx(path: Path, *, output_dir: Path, settings: PreprocessSettings) -> Path:
        nonlocal ppx_calls
        ppx_calls += 1
        assert path == pdf
        out = output_dir / "clean" / "ppx.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("ppx formal text", encoding="utf-8")
        return out

    def fail_mineru(files: list[Path], *, output_dir: Path, settings: PreprocessSettings) -> Path:
        mineru_calls.append([path.name for path in files])
        raise AssertionError("missing MinerU key should avoid MinerU conversion")

    rows = prepare_markdown_inputs(
        input_path=pdf,
        output_dir=tmp_path / "out",
        settings=PreprocessSettings(mineru_api_key="", pdf_page_chunk_size=30, enable_ppx=True, mineru_fallback="ppx"),
        page_count_reader=fake_page_count,
        pdf_splitter=fake_split,
        ppx_runner=fake_ppx,
        mineru_runner=fail_mineru,
    )

    assert split_calls == []
    assert mineru_calls == []
    assert ppx_calls == 1
    assert rows == [tmp_path / "out" / "preprocess" / "book" / "cross" / "initial.md"]
    manifest = json.loads((tmp_path / "out" / "preprocess" / "book" / "preprocess_manifest.json").read_text(encoding="utf-8"))
    assert manifest["acquisition_mode"] == "ppx_fallback"
    assert manifest["mineru_error"] == "missing MinerU API key"


def test_run_mineru_vlm_reuses_matching_manifest(tmp_path: Path) -> None:
    source = tmp_path / "seg.pdf"
    source.write_bytes(b"%PDF")
    output = tmp_path / "mineru"
    segment_md = output / "segment_001" / "full.md"
    segment_md.parent.mkdir(parents=True)
    segment_md.write_text("cached mineru", encoding="utf-8")
    merged = output / "mineru_vlm.md"
    merged.write_text("cached mineru", encoding="utf-8")
    (output / "mineru_manifest.json").write_text(
        json.dumps(
            {
                "status": "converted",
                "source_files": [str(source)],
                "segment_markdowns": [str(segment_md)],
                "merged_markdown": str(merged),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = run_mineru_vlm([source], output_dir=output, settings=PreprocessSettings(mineru_api_key="token"))

    assert result == merged
    assert result.read_text(encoding="utf-8") == "cached mineru"


def test_run_mineru_vlm_for_pdf_chunks_reuses_matching_manifest_without_api_key(tmp_path: Path) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF")
    segment_dir = tmp_path / "segments"
    output = tmp_path / "mineru"
    segment_path = segment_dir / "book__pages_0001_0002.pdf"
    segment_md = output / "segment_001" / "full.md"
    merged = output / "mineru_vlm.md"
    segment_path.parent.mkdir(parents=True)
    segment_path.write_bytes(b"%PDF")
    segment_md.parent.mkdir(parents=True)
    segment_md.write_text("cached chunk", encoding="utf-8")
    merged.write_text("cached chunk", encoding="utf-8")
    (output / "mineru_manifest.json").write_text(
        json.dumps(
            {
                "status": "converted",
                "source_files": [str(segment_path)],
                "segment_markdowns": [str(segment_md)],
                "merged_markdown": str(merged),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = run_mineru_vlm_for_pdf_chunks(
        source,
        page_count=2,
        segment_dir=segment_dir,
        output_dir=output,
        settings=PreprocessSettings(mineru_api_key="", pdf_page_chunk_size=30),
    )

    assert result == merged
    assert result.read_text(encoding="utf-8") == "cached chunk"


def test_run_mineru_vlm_submits_multiple_files_in_one_batch(tmp_path: Path, monkeypatch) -> None:
    files = [tmp_path / "seg1.pdf", tmp_path / "seg2.pdf"]
    for file in files:
        file.write_bytes(b"%PDF")
    output = tmp_path / "mineru"
    created_batches: list[list[str]] = []

    class FakeMinerUClient:
        def create_upload_batch(self, *, files: list[object], model_version: str) -> tuple[str, list[str]]:
            created_batches.append([getattr(file, "name") for file in files])
            return "batch-1", [f"https://upload.test/{index}" for index, _ in enumerate(files)]

        def upload_files(self, *, files: list[object], upload_urls: list[str]) -> None:
            assert [getattr(file, "name") for file in files] == ["seg1.pdf", "seg2.pdf"]
            assert len(upload_urls) == 2

        def poll_batch_results(
            self,
            *,
            batch_id: str,
            poll_interval_seconds: int,
            timeout_seconds: int,
            on_poll=None,
        ) -> list[dict[str, object]]:
            rows = [
                {"state": "done", "file_name": "seg1.pdf", "data_id": "llmcheck_seg1_001", "full_zip_url": "https://download.test/seg1.zip"},
                {"state": "done", "file_name": "seg2.pdf", "data_id": "llmcheck_seg2_002", "full_zip_url": "https://download.test/seg2.zip"},
            ]
            if on_poll is not None:
                on_poll(rows)
            return rows

        def download_result_zip(self, *, full_zip_url: str, output_path: Path) -> Path:
            with zipfile.ZipFile(output_path, "w") as archive:
                archive.writestr("full.md", f"markdown from {full_zip_url}")
            return output_path

    monkeypatch.setattr("llmcheck.preprocess._mineru_client", lambda settings: FakeMinerUClient())

    result = run_mineru_vlm(files, output_dir=output, settings=PreprocessSettings(mineru_api_key="token", mineru_batch_size=50))

    assert created_batches == [["seg1.pdf", "seg2.pdf"]]
    text = result.read_text(encoding="utf-8")
    assert "markdown from https://download.test/seg1.zip" in text
    assert "markdown from https://download.test/seg2.zip" in text


def test_run_mineru_vlm_resumes_multiple_files_from_one_batch(tmp_path: Path, monkeypatch) -> None:
    files = [tmp_path / "seg1.pdf", tmp_path / "seg2.pdf"]
    for file in files:
        file.write_bytes(b"%PDF")
    output = tmp_path / "mineru"
    for index, file in enumerate(files, start=1):
        segment_dir = output / f"segment_{index:03d}"
        segment_dir.mkdir(parents=True)
        (segment_dir / "status.json").write_text(
            json.dumps(
                {
                    "status": "polling",
                    "source": str(file),
                    "index": index,
                    "file_name": file.name,
                    "data_id": f"llmcheck_seg{index}_{index:03d}",
                    "batch_id": "batch-old",
                    "batch_size": 2,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    calls: list[str] = []

    class FakeMinerUClient:
        def create_upload_batch(self, *, files: list[object], model_version: str) -> tuple[str, list[str]]:
            raise AssertionError("should reuse existing MinerU batch")

        def upload_files(self, *, files: list[object], upload_urls: list[str]) -> None:
            raise AssertionError("should not upload again")

        def poll_batch_results(
            self,
            *,
            batch_id: str,
            poll_interval_seconds: int,
            timeout_seconds: int,
            on_poll=None,
        ) -> list[dict[str, object]]:
            calls.append(f"poll:{batch_id}")
            rows = [
                {"state": "done", "file_name": "seg2.pdf", "data_id": "llmcheck_seg2_002", "full_zip_url": "https://download.test/seg2.zip"},
                {"state": "done", "file_name": "seg1.pdf", "data_id": "llmcheck_seg1_001", "full_zip_url": "https://download.test/seg1.zip"},
            ]
            if on_poll is not None:
                on_poll(rows)
            return rows

        def download_result_zip(self, *, full_zip_url: str, output_path: Path) -> Path:
            calls.append(f"download:{full_zip_url}")
            with zipfile.ZipFile(output_path, "w") as archive:
                archive.writestr("full.md", f"markdown from {full_zip_url}")
            return output_path

    monkeypatch.setattr("llmcheck.preprocess._mineru_client", lambda settings: FakeMinerUClient())

    result = run_mineru_vlm(files, output_dir=output, settings=PreprocessSettings(mineru_api_key="token", mineru_batch_size=50))

    text = result.read_text(encoding="utf-8")
    assert calls == [
        "poll:batch-old",
        "download:https://download.test/seg1.zip",
        "download:https://download.test/seg2.zip",
    ]
    assert text.index("markdown from https://download.test/seg1.zip") < text.index("markdown from https://download.test/seg2.zip")
    assert json.loads((output / "segment_001" / "status.json").read_text(encoding="utf-8"))["status"] == "done"
    assert json.loads((output / "segment_002" / "status.json").read_text(encoding="utf-8"))["status"] == "done"


def test_run_one_mineru_file_writes_segment_status(tmp_path: Path) -> None:
    source = tmp_path / "seg.pdf"
    source.write_bytes(b"%PDF")
    output = tmp_path / "mineru" / "segment_001"
    calls: list[str] = []

    class FakeMinerUClient:
        def create_upload_batch(self, *, files: list[object], model_version: str) -> tuple[str, list[str]]:
            calls.append(f"create:{model_version}:{len(files)}")
            return "batch-1", ["https://upload.test/1"]

        def upload_files(self, *, files: list[object], upload_urls: list[str]) -> None:
            calls.append(f"upload:{len(files)}:{len(upload_urls)}")

        def poll_batch_results(
            self,
            *,
            batch_id: str,
            poll_interval_seconds: int,
            timeout_seconds: int,
            on_poll: object = None,
        ) -> list[dict[str, str]]:
            calls.append(f"poll:{batch_id}:{poll_interval_seconds}:{timeout_seconds}")
            return [{"state": "done", "full_zip_url": "https://download.test/result.zip"}]

        def download_result_zip(self, *, full_zip_url: str, output_path: Path) -> Path:
            calls.append(f"download:{full_zip_url}")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(output_path, "w") as archive:
                archive.writestr("full.md", "mineru text")
            return output_path

    result = _run_one_mineru_file(
        source,
        output_dir=output,
        settings=PreprocessSettings(mineru_api_key="token", mineru_poll_interval_seconds=7, mineru_timeout_seconds=99),
        client=FakeMinerUClient(),  # type: ignore[arg-type]
        index=1,
    )

    status = json.loads((output / "status.json").read_text(encoding="utf-8"))
    assert result == output / "full.md"
    assert result.read_text(encoding="utf-8") == "mineru text"
    assert calls == ["create:vlm:1", "upload:1:1", "poll:batch-1:7:99", "download:https://download.test/result.zip"]
    assert status["status"] == "done"
    assert status["batch_id"] == "batch-1"
    assert status["markdown"] == str(output / "full.md")
    assert status["source"] == str(source)


def test_run_one_mineru_file_resumes_existing_polling_batch(tmp_path: Path) -> None:
    source = tmp_path / "seg.pdf"
    source.write_bytes(b"%PDF")
    output = tmp_path / "mineru" / "segment_001"
    output.mkdir(parents=True)
    (output / "status.json").write_text(
        json.dumps({"status": "polling", "source": str(source), "batch_id": "batch-old"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    class FakeMinerUClient:
        def create_upload_batch(self, *, files: list[object], model_version: str) -> tuple[str, list[str]]:
            raise AssertionError("should reuse existing MinerU batch")

        def upload_files(self, *, files: list[object], upload_urls: list[str]) -> None:
            raise AssertionError("should not upload again")

        def poll_batch_results(
            self,
            *,
            batch_id: str,
            poll_interval_seconds: int,
            timeout_seconds: int,
            on_poll: object = None,
        ) -> list[dict[str, str]]:
            calls.append(f"poll:{batch_id}:{poll_interval_seconds}:{timeout_seconds}")
            return [{"state": "done", "full_zip_url": "https://download.test/result.zip"}]

        def download_result_zip(self, *, full_zip_url: str, output_path: Path) -> Path:
            calls.append(f"download:{full_zip_url}")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(output_path, "w") as archive:
                archive.writestr("full.md", "resumed mineru text")
            return output_path

    result = _run_one_mineru_file(
        source,
        output_dir=output,
        settings=PreprocessSettings(mineru_api_key="token", mineru_poll_interval_seconds=5, mineru_timeout_seconds=88),
        client=FakeMinerUClient(),  # type: ignore[arg-type]
        index=1,
    )

    status = json.loads((output / "status.json").read_text(encoding="utf-8"))
    assert result.read_text(encoding="utf-8") == "resumed mineru text"
    assert calls == ["poll:batch-old:5:88", "download:https://download.test/result.zip"]
    assert status["status"] == "done"
    assert status["batch_id"] == "batch-old"


def test_run_one_mineru_file_resumes_timeout_error_batch(tmp_path: Path) -> None:
    source = tmp_path / "seg.pdf"
    source.write_bytes(b"%PDF")
    output = tmp_path / "mineru" / "segment_001"
    output.mkdir(parents=True)
    (output / "status.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "source": str(source),
                "error": "MinerU batch 980a789c-b0ac-4bd5-8df6-01ef41269e94 瓒呮椂锛屾渶鍚庣姸鎬侊細pending",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    class FakeMinerUClient:
        def create_upload_batch(self, *, files: list[object], model_version: str) -> tuple[str, list[str]]:
            raise AssertionError("should recover batch id from failed timeout status")

        def upload_files(self, *, files: list[object], upload_urls: list[str]) -> None:
            raise AssertionError("should not upload again")

        def poll_batch_results(
            self,
            *,
            batch_id: str,
            poll_interval_seconds: int,
            timeout_seconds: int,
            on_poll: object = None,
        ) -> list[dict[str, str]]:
            calls.append(f"poll:{batch_id}")
            return [{"state": "done", "full_zip_url": "https://download.test/result.zip"}]

        def download_result_zip(self, *, full_zip_url: str, output_path: Path) -> Path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(output_path, "w") as archive:
                archive.writestr("full.md", "timeout resumed mineru text")
            return output_path

    result = _run_one_mineru_file(
        source,
        output_dir=output,
        settings=PreprocessSettings(mineru_api_key="token"),
        client=FakeMinerUClient(),  # type: ignore[arg-type]
        index=1,
    )

    status = json.loads((output / "status.json").read_text(encoding="utf-8"))
    assert result.read_text(encoding="utf-8") == "timeout resumed mineru text"
    assert calls == ["poll:980a789c-b0ac-4bd5-8df6-01ef41269e94"]
    assert status["batch_id"] == "980a789c-b0ac-4bd5-8df6-01ef41269e94"


def test_prepare_markdown_inputs_sends_images_and_office_to_mineru_without_ppx(tmp_path: Path) -> None:
    image = tmp_path / "page.png"
    image.write_bytes(b"png")
    calls: list[str] = []

    def fail_ppx(*args: object, **kwargs: object) -> Path:
        raise AssertionError("images and office files must not run PPX")

    def fake_mineru(files: list[Path], *, output_dir: Path, settings: PreprocessSettings) -> Path:
        calls.extend(path.name for path in files)
        out = output_dir / "page.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("mineru image text", encoding="utf-8")
        return out

    rows = prepare_markdown_inputs(
        input_path=image,
        output_dir=tmp_path / "out",
        settings=PreprocessSettings(mineru_api_key="token"),
        ppx_runner=fail_ppx,
        mineru_runner=fake_mineru,
    )

    assert rows[0].read_text(encoding="utf-8").strip() == "mineru image text"
    assert rows[0].name == "initial.md"
    assert calls == ["page.png"]


def test_prepare_markdown_inputs_converts_word_to_pdf_chunks_for_mineru(tmp_path: Path, monkeypatch) -> None:
    document = tmp_path / "book.docx"
    document.write_bytes(b"docx")
    calls: list[tuple[str, object]] = []

    def fake_convert(path: Path, *, output_dir: Path) -> Path:
        calls.append(("convert", path.name))
        pdf = output_dir / "book.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-1.4\n")
        return pdf

    def fake_page_count(path: Path) -> int:
        calls.append(("page_count", path.name))
        return 61

    def fake_pdf_chunks(
        path: Path,
        *,
        page_count: int,
        segment_dir: Path,
        output_dir: Path,
        settings: PreprocessSettings,
    ) -> Path:
        calls.append(("pdf_chunks", [path.name, page_count, settings.pdf_page_chunk_size, segment_dir.name]))
        out = output_dir / "mineru_vlm.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("word text", encoding="utf-8")
        return out

    def fail_mineru(files: list[Path], *, output_dir: Path, settings: PreprocessSettings) -> Path:
        raise AssertionError(f"word files must not be submitted whole: {files}")

    monkeypatch.setattr(preprocess, "_convert_word_to_pdf", fake_convert)
    monkeypatch.setattr(preprocess, "run_mineru_vlm_for_pdf_chunks", fake_pdf_chunks)

    rows = prepare_markdown_inputs(
        input_path=document,
        output_dir=tmp_path / "out",
        settings=PreprocessSettings(mineru_api_key="token", pdf_page_chunk_size=30),
        page_count_reader=fake_page_count,
        mineru_runner=fail_mineru,
    )

    assert rows[0].read_text(encoding="utf-8").strip() == "word text"
    assert rows[0].name == "initial.md"
    assert ("convert", "book.docx") in calls
    assert ("page_count", "book.pdf") in calls
    assert ("pdf_chunks", ["book.pdf", 61, 30, "mineru_segments"]) in calls


def test_mineru_client_retries_transient_request_errors(monkeypatch) -> None:
    calls = 0

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"code": 0, "data": {"ok": true}}'

    def fake_urlopen(*args: object, **kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("timed out")
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = MinerUClient(token="token", base_url="https://mineru.test", timeout_seconds=1, max_retries=2, retry_backoff_seconds=0)

    assert client._request_json("GET", "/status")["data"] == {"ok": True}
    assert calls == 2


def test_mineru_poll_continues_after_transient_status_error(monkeypatch) -> None:
    client = MinerUClient(token="token", base_url="https://mineru.test", timeout_seconds=1, max_retries=1, retry_backoff_seconds=0)
    calls = 0

    def fake_request_json(method: str, api_path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise MinerUTransientError("temporary timeout")
        return {"code": 0, "data": {"extract_result": {"state": "done", "full_zip_url": "https://example.test/result.zip"}}}

    monkeypatch.setattr(client, "_request_json", fake_request_json)

    rows = client.poll_batch_results(batch_id="batch", poll_interval_seconds=0, timeout_seconds=10)

    assert rows[0]["state"] == "done"
    assert calls == 2


def test_batch_discovery_excludes_output_and_generated_files(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    output = source / "output"
    output.mkdir(parents=True)
    for name in ["0涓尰姒傚康鍏ラ棬.pdf", "book.md", "0涓尰姒傚康鍏ラ棬__myocr_text.pdf", "notes.txt"]:
        (source / name).write_text("x", encoding="utf-8")
    (output / "generated.md").write_text("x", encoding="utf-8")

    rows = discover_batch_sources(source_dir=source, output_dir=output)

    assert [path.name for path in rows] == ["0涓尰姒傚康鍏ラ棬.pdf", "book.md"]


def test_batch_discovery_sorts_by_leading_filename_number(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    output = tmp_path / "output"
    for name in [
        "100中国百年百名中医临床家丛书—张子琳.md",
        "2中国百年百名中医临床家丛书—查玉明.md",
        "10中国百年百名中医临床家丛书—董建华.md",
        "1中国百年百名中医临床家丛书—蔡小荪.md",
    ]:
        (source / name).write_text("x", encoding="utf-8")

    rows = discover_batch_sources(source_dir=source, output_dir=output)

    assert [path.name for path in rows] == [
        "1中国百年百名中医临床家丛书—蔡小荪.md",
        "2中国百年百名中医临床家丛书—查玉明.md",
        "10中国百年百名中医临床家丛书—董建华.md",
        "100中国百年百名中医临床家丛书—张子琳.md",
    ]


def test_batch_discovery_prunes_output_subtree(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "pdf"
    output = source / "output"
    nested_output = output / "0001_book" / "reports"
    nested_source = source / "nested"
    nested_output.mkdir(parents=True)
    nested_source.mkdir(parents=True)
    (source / "book.md").write_text("book", encoding="utf-8")
    (nested_source / "chapter.md").write_text("chapter", encoding="utf-8")
    (nested_output / "generated.md").write_text("generated", encoding="utf-8")
    visited: list[Path] = []
    real_walk = os.walk

    def tracking_walk(*args: object, **kwargs: object):
        for dirpath, dirnames, filenames in real_walk(*args, **kwargs):
            visited.append(Path(dirpath).resolve())
            yield dirpath, dirnames, filenames

    monkeypatch.setattr("llmcheck.batch.os.walk", tracking_walk)

    rows = discover_batch_sources(source_dir=source, output_dir=output)

    assert [path.name for path in rows] == ["book.md", "chapter.md"]
    assert output.resolve() not in visited


def test_run_batch_writes_per_book_outputs_and_state(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    (source / "a.md").write_text("a", encoding="utf-8")
    (source / "b.md").write_text("b", encoding="utf-8")
    output = source / "output"

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        report_dir = output_dir / "process" / "reports"
        report_dir.mkdir(parents=True)
        (report_dir / "llmcheck_summary.json").write_text('{"status":"passed"}\n', encoding="utf-8")
        return {"status": "passed", "documents": [{"source_path": str(input_path)}]}

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        runner=fake_runner,
    )

    assert summary["status"] == "passed"
    assert summary["total"] == 2
    assert (output / "process" / "llmcheck_batch_state.jsonl").exists()
    assert (output / "process" / "llmcheck_batch_summary.json").exists()
    assert sorted(path.name for path in output.glob("0*_*.md")) == []
    assert len(list((output / "process" / "books").glob("0*/process/reports/llmcheck_summary.json"))) == 2


def test_cli_batch_accepts_acceptance_repair_rounds(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, LlmCheckSettings] = {}

    def fake_run_batch(**kwargs: object) -> dict[str, object]:
        captured["settings"] = kwargs["settings"]  # type: ignore[assignment]
        return {"status": "passed"}

    monkeypatch.setattr("llmcheck.cli.run_batch", fake_run_batch)

    status = main(
        [
            "batch",
            "--source-dir",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--llm-api-url",
            "http://llm.test",
            "--llm-api-key",
            "key",
            "--llm-model",
            "model",
            "--acceptance-repair-rounds",
            "3",
        ]
    )

    assert status == 0
    assert captured["settings"].acceptance_repair_rounds == 3


def test_run_batch_processes_pdf_through_mineru_ppx_llm_and_final_outputs(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    pdf = source / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    output = source / "output"
    calls: list[tuple[str, object]] = []
    ppx_finished = threading.Event()
    client = EchoChunkClient()

    def fake_page_count(path: Path) -> int:
        assert path == pdf
        return 61

    def fake_split(path: Path, *, output_dir: Path, max_pages: int) -> list[Path]:
        calls.append(("split", max_pages))
        paths = [output_dir / "seg-001.pdf", output_dir / "seg-002.pdf", output_dir / "seg-003.pdf"]
        for item in paths:
            item.parent.mkdir(parents=True, exist_ok=True)
            item.write_bytes(b"%PDF-1.4\n")
        return paths

    def fake_ppx(path: Path, *, output_dir: Path, settings: PreprocessSettings) -> Path:
        calls.append(("ppx", path.name))
        out = output_dir / "clean" / "ppx.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("PPX audit text", encoding="utf-8")
        ppx_finished.set()
        return out

    def fake_mineru(files: list[Path], *, output_dir: Path, settings: PreprocessSettings) -> Path:
        calls.append(("mineru_model", settings.mineru_model))
        calls.append(("mineru_files", [path.name for path in files]))
        out = output_dir / "mineru_vlm.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("# MinerU\n\nMinerU body typo.\n", encoding="utf-8")
        return out

    def preprocess_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> list[Path]:
        return prepare_markdown_inputs(
            input_path=input_path,
            output_dir=output_dir,
            settings=PreprocessSettings(
                mineru_api_url=settings.mineru_api_url,
                mineru_api_key=settings.mineru_api_key,
                mineru_model=settings.mineru_model,
                mineru_concurrency=settings.mineru_concurrency,
                mineru_timeout_seconds=settings.mineru_timeout_seconds,
                mineru_request_timeout_seconds=settings.mineru_request_timeout_seconds,
                mineru_max_retries=settings.mineru_max_retries,
                mineru_retry_backoff_seconds=settings.mineru_retry_backoff_seconds,
                enable_ppx=bool(getattr(settings, "enable_ppx", False)),
                mineru_fallback=settings.mineru_fallback if bool(getattr(settings, "enable_ppx", False)) else "none",
                pdf_page_chunk_size=settings.pdf_page_chunk_size,
                ppx_command=settings.ppx_command,
                ppx_cwd=settings.ppx_cwd,
                ppx_timeout_seconds=settings.ppx_timeout_seconds,
            ),
            page_count_reader=fake_page_count,
            pdf_splitter=fake_split,
            ppx_runner=fake_ppx,
            mineru_runner=fake_mineru,
        )

    def runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        return process_documents(
            input_path=input_path,
            output_dir=output_dir,
            settings=settings,
            client=client,
            preprocess_runner=preprocess_runner,
        )

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="key",
            llm_model="model",
            llm_mode="legacy-correction",
            concurrency=4,
            mineru_api_key="token",
            mineru_concurrency=3,
            pdf_page_chunk_size=30,
            enable_ppx=True,
            mineru_fallback="ppx",
        ),
        runner=runner,
    )

    book_output = output / "process" / "books" / "0001_book"
    book_summary = json.loads((book_output / "process" / "reports" / "llmcheck_summary.json").read_text(encoding="utf-8"))
    document = book_summary["documents"][0]
    final_md = Path(document["final_markdown_path"])
    final_pdf = Path(document["text_pdf_path"])
    manifest = json.loads((book_output / "process" / "preprocess" / "book" / "preprocess_manifest.json").read_text(encoding="utf-8"))
    ppx_status = json.loads(Path(manifest["ppx_audit_status"]).read_text(encoding="utf-8"))

    assert summary["status"] == "passed"
    assert summary["documents"][0]["output_dir"] == str(book_output)
    assert book_summary["status"] == "passed"
    assert document["status"] == "passed"
    assert final_md.exists()
    assert "MinerU body" in final_md.read_text(encoding="utf-8")
    assert final_pdf.read_bytes().startswith(b"%PDF-")
    assert Path(manifest["formal_markdown"]).name == "initial.md"
    assert Path(manifest["mineru_markdown"]).name == "mineru_vlm.md"
    assert "cross_report" in manifest
    assert ppx_finished.wait(timeout=1)
    assert ppx_status["status"] == "completed"
    assert ("split", 30) in calls
    assert ("mineru_model", "vlm") in calls
    assert ("mineru_files", ["seg-001.pdf", "seg-002.pdf", "seg-003.pdf"]) in calls
    assert client.correction_calls >= 1
    assert client.acceptance_calls >= 1


def test_run_batch_treats_skipped_passed_books_as_success(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    source_file = source / "a.md"
    source_file.write_text("a", encoding="utf-8")
    output = tmp_path / "output"
    book_output = output / "process" / "books" / "0001_a"
    report_dir = book_output / "process" / "reports"
    report_dir.mkdir(parents=True)
    final_md = book_output / "final_markdown" / "a.md"
    final_pdf = book_output / "text_pdfs" / "a.pdf"
    final_md.parent.mkdir(parents=True)
    final_pdf.parent.mkdir(parents=True)
    final_md.write_text("accepted", encoding="utf-8")
    final_pdf.write_bytes(b"%PDF-1.4\n")
    binding_report = report_dir / "a.artifact_binding.json"
    final_sha = hashlib.sha256("accepted".encode("utf-8")).hexdigest()
    pdf_sha = hashlib.sha256(b"%PDF-1.4\n").hexdigest()
    binding_report.write_text(
        json.dumps(
            {
                "accepted": True,
                "final_markdown_sha256": final_sha,
                "pdf_source_text_sha256": final_sha,
                "pdf_binary_sha256": pdf_sha,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (report_dir / "llmcheck_summary.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "input_path": str(source_file.resolve()),
                "documents": [
                    {
                        "status": "passed",
                        "final_markdown_path": str(final_md),
                        "text_pdf_path": str(final_pdf),
                        "artifact_binding_report_path": str(binding_report),
                        "artifact_binding_status": "passed",
                        "final_markdown_sha256": final_sha,
                        "pdf_source_sha256": final_sha,
                        "pdf_sha256": pdf_sha,
                        "sha256": final_sha,
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    calls = 0

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"status": "passed"}

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        runner=fake_runner,
    )

    assert calls == 0
    assert summary["status"] == "passed"
    assert summary["passed"] == 1
    assert summary["skipped"] == 1
    assert summary["documents"][0]["status"] == "skipped"


def test_run_batch_skips_when_final_markdown_already_exists(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    source_file = source / "1中国百年百名中医临床家丛书—蔡小荪.md"
    source_file.write_text("source", encoding="utf-8")
    output = tmp_path / "output"
    final_dir = output / "md"
    final_dir.mkdir(parents=True)
    (final_dir / f"{source_file.stem}.md").write_text("already delivered", encoding="utf-8")
    calls = 0

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"status": "passed"}

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        runner=fake_runner,
    )

    assert calls == 0
    assert summary["status"] == "passed"
    assert summary["skipped"] == 1
    assert summary["documents"][0]["status"] == "skipped"
    assert summary["documents"][0]["result_status"] == "already_delivered"


def test_run_batch_publishes_markdown_by_source_stem_and_delivery_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    source_file = source / "100医案.md"
    source_file.write_text("source", encoding="utf-8")
    output = tmp_path / "output"

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        final_md = output_dir / "final_markdown" / "internal.md"
        final_pdf = output_dir / "text_pdfs" / "internal.pdf"
        final_md.parent.mkdir(parents=True)
        final_pdf.parent.mkdir(parents=True)
        final_md.write_text("# accepted\n", encoding="utf-8")
        final_pdf.write_bytes(b"%PDF-1.4\n")
        return {
            "status": "passed",
            "documents": [
                {
                    "document_id": "doc-1",
                    "source_path": str(input_path),
                    "status": "passed",
                    "final_markdown_path": str(final_md),
                    "text_pdf_path": str(final_pdf),
                }
            ],
        }

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        runner=fake_runner,
    )

    delivery_path = output / "md" / f"{source_file.stem}.md"
    prefixed_delivery_path = output / "md" / f"0001_{source_file.stem}.md"
    manifest_path = output / "process" / "llmcheck_delivery_manifest.jsonl"
    manifest_rows = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines()]
    book_manifest = output / "process" / "books" / "0001_100医案" / "process" / "reports" / "100医案.delivery_manifest.json"

    assert summary["status"] == "passed"
    assert delivery_path.read_text(encoding="utf-8") == "# accepted\n"
    assert not prefixed_delivery_path.exists()
    assert summary["delivery_manifest_path"] == str(manifest_path)
    assert manifest_rows[-1]["accepted"] is True
    assert manifest_rows[-1]["source_path"] == str(source_file.resolve())
    assert manifest_rows[-1]["final_markdown_path"] == str(delivery_path.resolve())
    assert manifest_rows[-1]["acquisition_status"] == "delivered"
    assert json.loads(book_manifest.read_text(encoding="utf-8"))["final_markdown_path"] == str(delivery_path.resolve())


def test_run_batch_skips_when_delivery_manifest_maps_source_hash_to_output(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    source_file = source / "a.md"
    source_file.write_text("source", encoding="utf-8")
    output = tmp_path / "output"
    final_dir = output / "md"
    process_dir = output / "process"
    delivered = final_dir / "custom-delivered-name.md"
    delivered.parent.mkdir(parents=True)
    delivered.write_text("accepted", encoding="utf-8")
    manifest_path = process_dir / "llmcheck_delivery_manifest.jsonl"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "accepted": True,
                "acquisition_status": "delivered",
                "source_path": str(source_file.resolve()),
                "source_sha256": hashlib.sha256(source_file.read_bytes()).hexdigest(),
                "final_markdown_path": str(delivered.resolve()),
                "final_markdown_sha256": hashlib.sha256("accepted".encode("utf-8")).hexdigest(),
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    calls = 0

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"status": "passed", "documents": []}

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        runner=fake_runner,
    )

    assert calls == 0
    assert summary["status"] == "passed"
    assert summary["already_delivered"] == 1
    assert summary["documents"][0]["status"] == "skipped"
    assert summary["documents"][0]["result_status"] == "already_delivered"


def test_run_batch_allows_explicit_process_and_final_md_dirs_when_source_is_under_output_root(tmp_path: Path) -> None:
    output_root = tmp_path / "output2"
    source = output_root / "0"
    source.mkdir(parents=True)
    process_dir = output_root / "process"
    final_md_dir = output_root / "md"
    source_file = source / "100医案.md"
    source_file.write_text("source", encoding="utf-8")
    final_md_dir.mkdir(parents=True)
    (final_md_dir / f"{source_file.stem}.md").write_text("already done", encoding="utf-8")
    calls: list[Path] = []

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        calls.append(input_path)
        return {"status": "passed", "documents": []}

    summary = run_batch(
        source_dir=source,
        output_dir=output_root,
        process_dir=process_dir,
        final_md_dir=final_md_dir,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        runner=fake_runner,
    )

    assert calls == []
    assert summary["status"] == "passed"
    assert summary["discovered_total"] == 1
    assert summary["process_dir"] == str(process_dir.resolve())
    assert summary["final_md_dir"] == str(final_md_dir.resolve())
    assert summary["documents"][0]["result_status"] == "already_delivered"


def test_run_batch_dry_run_reports_selected_books_without_calling_runner(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name in ["1一.md", "2二.md"]:
        (source / name).write_text("source", encoding="utf-8")
    output = tmp_path / "output"
    calls = 0

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"status": "passed", "documents": []}

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        runner=fake_runner,
        dry_run=True,
    )

    assert calls == 0
    assert summary["status"] == "dry_run"
    assert summary["discovered_total"] == 2
    assert summary["selected_total"] == 2
    assert [row["result_status"] for row in summary["documents"]] == ["would_process", "would_process"]


def test_run_batch_preflight_only_includes_llm_preflight_report(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "1一.md").write_text("source", encoding="utf-8")
    output = tmp_path / "output"
    runner_calls = 0
    preflight_calls = 0

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        nonlocal runner_calls
        runner_calls += 1
        return {"status": "passed", "documents": []}

    def fake_preflight(settings: LlmCheckSettings) -> dict[str, object]:
        nonlocal preflight_calls
        preflight_calls += 1
        return {
            "status": "passed",
            "models": [{"model": settings.llm_model, "available": True}],
            "preferred_available_model": settings.llm_model,
        }

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="mimo-v2.5-pro"),
        runner=fake_runner,
        preflight_only=True,
        llm_preflight=fake_preflight,
    )

    assert runner_calls == 0
    assert preflight_calls == 1
    assert summary["status"] == "preflight_only"
    assert summary["llm_preflight"]["preferred_available_model"] == "mimo-v2.5-pro"


def test_run_batch_stops_before_books_when_llm_preflight_blocks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name in ["1一.md", "2二.md"]:
        (source / name).write_text("source", encoding="utf-8")
    output = tmp_path / "output"
    runner_calls = 0

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        nonlocal runner_calls
        runner_calls += 1
        return {"status": "passed", "documents": []}

    def fake_preflight(settings: LlmCheckSettings) -> dict[str, object]:
        return {
            "status": "blocked_needs_llm_service",
            "error": "all_review_models_unavailable",
            "models": [
                {"model": settings.llm_model, "available": False, "error": "model unavailable"},
                {"model": "gpt-5.5", "available": False, "error": "model unavailable"},
            ],
        }

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="key",
            llm_model="mimo-v2.5-pro",
            fallback_models="gpt-5.5",
        ),
        runner=fake_runner,
        llm_preflight=fake_preflight,
    )

    assert runner_calls == 0
    assert summary["status"] == "blocked_needs_llm_service"
    assert summary["error"] == "all_review_models_unavailable"
    assert summary["selected_total"] == 2
    assert summary["llm_preflight"]["status"] == "blocked_needs_llm_service"


def test_run_batch_stops_on_global_llm_service_error(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name in ["1一.md", "2二.md", "3三.md"]:
        (source / name).write_text("source", encoding="utf-8")
    output = tmp_path / "output"
    calls: list[str] = []

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        calls.append(input_path.name)
        return {
            "status": "review_required",
            "error": "All configured LLM reviewer models are unavailable; update LLM service information.",
            "documents": [
                {
                    "status": "llm_review_error",
                    "error": "all_review_models_unavailable",
                }
            ],
        }

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="mimo-v2.5-pro"),
        runner=fake_runner,
        stop_on_global_service_error=True,
    )

    assert calls == ["1一.md"]
    assert summary["status"] == "blocked_needs_llm_service"
    assert summary["error"] == "all_review_models_unavailable"
    assert summary["total"] == 1
    assert summary["documents"][0]["status"] == "failed"


def test_run_model_compare_runs_same_first_books_for_each_model(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name in ["1一.md", "2二.md", "3三.md", "4四.md"]:
        (source / name).write_text("x", encoding="utf-8")
    output = tmp_path / "compare"
    calls: list[tuple[str, str, int, str]] = []

    def fake_batch_runner(**kwargs: object) -> dict[str, object]:
        settings = kwargs["settings"]
        assert isinstance(settings, LlmCheckSettings)
        calls.append((settings.llm_model, settings.llm_api_url, int(kwargs["limit"]), Path(str(kwargs["output_dir"])).name))
        return {
            "status": "passed",
            "total": 3,
            "passed": 3,
            "failed": 0,
            "documents": [],
        }

    report = run_model_compare(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="mimo-v2.5-pro"),
        models=["mimo-v2.5-pro", "gpt-5.5"],
        preferred_model="mimo-v2.5-pro",
        fallback_model="gpt-5.5",
        limit=3,
        runner=fake_batch_runner,
    )

    assert calls == [
        ("mimo-v2.5-pro", "https://api.iaigc.fun/v1", 3, "mimo-v2.5-pro"),
        ("gpt-5.5", "http://127.0.0.1:3022", 3, "gpt-5.5"),
    ]
    assert [row["llm_api_url"] for row in report["models"]] == ["https://api.iaigc.fun/v1", "http://127.0.0.1:3022"]
    assert report["status"] == "passed"
    assert report["recommendation"]["preferred_model"] == "mimo-v2.5-pro"
    assert report["recommendation"]["fallback_model"] == "gpt-5.5"
    assert report["recommendation"]["quality_equivalent"] is True


def test_run_model_compare_disables_fallback_inside_each_model_run(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "1一.md").write_text("x", encoding="utf-8")
    output = tmp_path / "compare"

    def fake_batch_runner(**kwargs: object) -> dict[str, object]:
        settings = kwargs["settings"]
        assert isinstance(settings, LlmCheckSettings)
        assert settings.allow_fallback_models is False
        assert settings.fallback_models == ""
        return {"status": "passed", "total": 1, "passed": 1, "failed": 0, "documents": []}

    run_model_compare(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="key",
            llm_model="mimo-v2.5-pro",
            allow_fallback_models=True,
            fallback_models="gpt-5.5",
        ),
        models=["mimo-v2.5-pro", "gpt-5.5"],
        preferred_model="mimo-v2.5-pro",
        fallback_model="gpt-5.5",
        limit=1,
        runner=fake_batch_runner,
    )


def test_run_model_compare_does_not_prefer_mimo_when_both_models_fail(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "1一.md").write_text("x", encoding="utf-8")
    output = tmp_path / "compare"

    def fake_batch_runner(**kwargs: object) -> dict[str, object]:
        return {
            "status": "review_required",
            "total": 3,
            "passed": 0,
            "failed": 3,
            "documents": [
                {"status": "llm_review_error"},
                {"status": "llm_review_error"},
                {"status": "llm_review_error"},
            ],
        }

    report = run_model_compare(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="mimo-v2.5-pro"),
        models=["mimo-v2.5-pro", "gpt-5.5"],
        preferred_model="mimo-v2.5-pro",
        fallback_model="gpt-5.5",
        limit=3,
        runner=fake_batch_runner,
    )

    assert report["status"] == "review_required"
    assert report["recommendation"]["quality_equivalent"] is False
    assert report["recommendation"]["preferred_model"] == "gpt-5.5"
    assert report["recommendation"]["comparison_basis"] == "no_successful_model"


def test_run_batch_reruns_when_passed_summary_lacks_artifact_binding(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    source_file = source / "a.md"
    source_file.write_text("a", encoding="utf-8")
    output = tmp_path / "output"
    book_output = output / "process" / "books" / "0001_a"
    report_dir = book_output / "process" / "reports"
    report_dir.mkdir(parents=True)
    final_md = book_output / "final_markdown" / "a.md"
    final_pdf = book_output / "text_pdfs" / "a.pdf"
    final_md.parent.mkdir(parents=True)
    final_pdf.parent.mkdir(parents=True)
    final_md.write_text("accepted", encoding="utf-8")
    final_pdf.write_bytes(b"%PDF-1.4\n")
    (report_dir / "llmcheck_summary.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "input_path": str(source_file.resolve()),
                "documents": [{"status": "passed", "final_markdown_path": str(final_md), "text_pdf_path": str(final_pdf)}],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    calls = 0

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        nonlocal calls
        calls += 1
        new_report_dir = output_dir / "process" / "reports"
        new_report_dir.mkdir(parents=True, exist_ok=True)
        (new_report_dir / "llmcheck_summary.json").write_text('{"status":"passed"}\n', encoding="utf-8")
        return {"status": "passed"}

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        runner=fake_runner,
    )

    assert calls == 1
    assert summary["status"] == "passed"
    assert summary["documents"][0]["status"] == "passed"


def test_run_batch_reruns_when_passed_summary_lacks_final_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.md").write_text("a", encoding="utf-8")
    output = tmp_path / "output"
    report_dir = output / "process" / "books" / "0001_a" / "process" / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "llmcheck_summary.json").write_text(
        json.dumps({"status": "passed", "input_path": str((source / "a.md").resolve()), "documents": [{"status": "passed"}]}) + "\n",
        encoding="utf-8",
    )
    calls = 0

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        nonlocal calls
        calls += 1
        new_report_dir = output_dir / "process" / "reports"
        new_report_dir.mkdir(parents=True, exist_ok=True)
        (new_report_dir / "llmcheck_summary.json").write_text('{"status":"passed"}\n', encoding="utf-8")
        return {"status": "passed"}

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        runner=fake_runner,
    )

    assert calls == 1
    assert summary["status"] == "passed"
    assert summary["documents"][0]["status"] == "passed"


def test_run_batch_preserves_original_index_with_start_index(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    (source / "a.md").write_text("a", encoding="utf-8")
    (source / "b.md").write_text("b", encoding="utf-8")
    output = source / "output"

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        report_dir = output_dir / "process" / "reports"
        report_dir.mkdir(parents=True)
        (report_dir / "llmcheck_summary.json").write_text('{"status":"passed"}\n', encoding="utf-8")
        return {"status": "passed"}

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        start_index=2,
        limit=1,
        runner=fake_runner,
    )

    assert summary["status"] == "passed"
    assert summary["documents"][0]["index"] == 2
    assert Path(summary["documents"][0]["output_dir"]).name == "0002_b"
    assert (output / "process" / "books" / "0002_b" / "process" / "reports" / "llmcheck_summary.json").exists()


def test_run_batch_empty_selection_requires_review(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    output = source / "output"

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
    )

    assert summary["status"] == "review_required"
    assert summary["total"] == 0
    assert "未发现可处理输入" in summary["error"]


def test_run_batch_summary_merges_incremental_state(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    (source / "a.md").write_text("a", encoding="utf-8")
    (source / "b.md").write_text("b", encoding="utf-8")
    output = source / "output"

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        report_dir = output_dir / "process" / "reports"
        report_dir.mkdir(parents=True)
        (report_dir / "llmcheck_summary.json").write_text('{"status":"passed"}\n', encoding="utf-8")
        return {"status": "passed"}

    run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        start_index=1,
        limit=1,
        runner=fake_runner,
    )
    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        start_index=2,
        limit=1,
        runner=fake_runner,
    )

    assert summary["status"] == "passed"
    assert summary["selected_total"] == 1
    assert summary["total"] == 2
    assert [Path(row["output_dir"]).name for row in summary["documents"]] == ["0001_a", "0002_b"]


def test_run_batch_current_window_ignores_future_historical_state(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    (source / "a.md").write_text("a", encoding="utf-8")
    (source / "b.md").write_text("b", encoding="utf-8")
    (source / "c.md").write_text("c", encoding="utf-8")
    output = source / "output"
    process_dir = output / "process"
    state_path = process_dir / "llmcheck_batch_state.jsonl"
    process_dir.mkdir(parents=True)
    stale_rows = [
        {
            "index": 2,
            "total": 3,
            "source_path": str((source / "b.md").resolve()),
            "output_dir": str(process_dir / "books" / "0002_b"),
            "status": "failed",
            "started_at": "2026-01-01T00:00:00",
            "finished_at": "2026-01-01T00:00:01",
            "result_status": "llm_review_failed",
            "error": "stale failure",
        },
        {
            "index": 3,
            "total": 3,
            "source_path": str((source / "c.md").resolve()),
            "output_dir": str(process_dir / "books" / "0003_c"),
            "status": "in_progress",
            "started_at": "2026-01-01T00:00:02",
            "finished_at": "",
            "result_status": "",
            "error": "",
        },
    ]
    state_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in stale_rows) + "\n", encoding="utf-8")

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        report_dir = output_dir / "process" / "reports"
        report_dir.mkdir(parents=True)
        (report_dir / "llmcheck_summary.json").write_text('{"status":"passed"}\n', encoding="utf-8")
        return {"status": "passed"}

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        start_index=1,
        limit=1,
        force=True,
        runner=fake_runner,
    )

    assert summary["status"] == "passed"
    assert summary["total"] == 1
    assert [Path(row["source_path"]).name for row in summary["documents"]] == ["a.md"]


def test_run_batch_out_of_range_ignores_historical_state(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    (source / "a.md").write_text("a", encoding="utf-8")
    output = source / "output"

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        report_dir = output_dir / "reports"
        report_dir.mkdir(parents=True)
        (report_dir / "llmcheck_summary.json").write_text('{"status":"passed"}\n', encoding="utf-8")
        return {"status": "passed"}

    run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        start_index=1,
        limit=1,
        runner=fake_runner,
    )
    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        start_index=99,
        limit=1,
        runner=fake_runner,
    )

    assert summary["status"] == "review_required"
    assert summary["selected_total"] == 0
    assert summary["total"] == 0
    assert summary["documents"] == []


def test_run_batch_emits_progress_events(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    (source / "a.md").write_text("a", encoding="utf-8")
    output = source / "output"
    events: list[dict[str, object]] = []

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        report_dir = output_dir / "reports"
        report_dir.mkdir(parents=True)
        (report_dir / "llmcheck_summary.json").write_text('{"status":"passed"}\n', encoding="utf-8")
        return {"status": "passed"}

    run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        runner=fake_runner,
        progress_callback=events.append,
    )

    assert [event["event"] for event in events] == ["batch_started", "book_started", "book_finished"]
    assert events[1]["book_name"] == "a.md"
    assert events[2]["status"] == "passed"


def test_run_batch_progress_callback_error_does_not_fail_book(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    (source / "a.md").write_text("a", encoding="utf-8")
    output = source / "output"

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        report_dir = output_dir / "reports"
        report_dir.mkdir(parents=True)
        (report_dir / "llmcheck_summary.json").write_text('{"status":"passed"}\n', encoding="utf-8")
        return {"status": "passed"}

    def broken_progress(event: dict[str, object]) -> None:
        raise RuntimeError(f"progress sink failed: {event['event']}")

    summary = run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        runner=fake_runner,
        progress_callback=broken_progress,
    )

    assert summary["status"] == "passed"
    assert summary["documents"][0]["status"] == "passed"


def test_run_batch_writes_in_progress_state_before_runner_finishes(tmp_path: Path) -> None:
    source = tmp_path / "pdf"
    source.mkdir()
    (source / "a.md").write_text("a", encoding="utf-8")
    output = source / "output"
    observed: dict[str, str] = {}

    def fake_runner(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        report_dir = output_dir / "reports"
        report_dir.mkdir(parents=True)
        (report_dir / "llmcheck_summary.json").write_text('{"status":"passed"}\n', encoding="utf-8")
        return {"status": "passed"}

    def on_progress(event: dict[str, object]) -> None:
        if event["event"] != "book_started":
            return
        state_rows = (output / "process" / "llmcheck_batch_state.jsonl").read_text(encoding="utf-8").splitlines()
        summary = json.loads((output / "process" / "llmcheck_batch_summary.json").read_text(encoding="utf-8"))
        observed["state_status"] = str(json.loads(state_rows[-1])["status"])
        observed["summary_status"] = str(summary["documents"][0]["status"])

    run_batch(
        source_dir=source,
        output_dir=output,
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        runner=fake_runner,
        progress_callback=on_progress,
    )

    assert observed == {"state_status": "in_progress", "summary_status": "in_progress"}


def test_cli_progress_event_prints_jsonl_to_stderr(capsys) -> None:
    _print_progress_event({"event": "book_started", "index": 6, "total": 24, "book_name": "14鏈崏澶囪璁茶В.pdf"})

    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["type"] == "progress"
    assert payload["event"] == "book_started"
    assert payload["book_name"] == "14鏈崏澶囪璁茶В.pdf"


def test_cli_mineru_status_summarizes_book_output(tmp_path: Path, capsys) -> None:
    mineru_dir = tmp_path / "process" / "preprocess" / "14鏈崏澶囪璁茶В" / "mineru"
    (mineru_dir / "segment_001").mkdir(parents=True)
    (mineru_dir / "segment_002").mkdir(parents=True)
    (mineru_dir / "segment_001" / "status.json").write_text('{"status":"cached"}\n', encoding="utf-8")
    (mineru_dir / "segment_002" / "status.json").write_text(
        '{"status":"polling","state_counts":{"pending":1}}\n',
        encoding="utf-8",
    )
    ppx_markdown = tmp_path / "process" / "preprocess" / "14鏈崏澶囪璁茶В" / "ppx" / "clean" / "ppx.md"
    ppx_markdown.parent.mkdir(parents=True)
    ppx_markdown.write_text("ppx audit text", encoding="utf-8")

    exit_code = main(["mineru-status", "--book-output-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["book_name"] == "14鏈崏澶囪璁茶В"
    assert payload["stage"] == "mineru_pending"
    assert payload["mineru_segments"]["total"] == 2
    assert payload["mineru_segments"]["status_counts"] == {"cached": 1, "polling": 1}
    assert payload["mineru_segments"]["cloud_state_counts"] == {"pending": 1}
    assert payload["mineru_segments"]["updated_age_seconds"] >= 0
    assert payload["artifacts"]["ppx_audit_size"] == len("ppx audit text".encode())
    assert payload["artifacts"]["llm_correction_report_count"] == 0
    assert payload["artifacts"]["llm_acceptance_report_count"] == 0


def test_llmcheck_settings_and_cli_default_to_ten_llm_workers(monkeypatch, tmp_path: Path) -> None:
    assert LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model").concurrency == 10
    captured: dict[str, object] = {}

    def fake_process_documents(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        captured["settings"] = settings
        return {
            "status": "passed",
            "input_path": str(input_path),
            "output_dir": str(output_dir),
            "document_count": 0,
            "documents": [],
        }

    source = tmp_path / "source.md"
    source.write_text("text", encoding="utf-8")
    monkeypatch.setattr("llmcheck.cli.process_documents", fake_process_documents)

    exit_code = main(
        [
            "run",
            "--input",
            str(source),
            "--output-dir",
            str(tmp_path / "out"),
            "--llm-api-url",
            "http://llm.test",
            "--llm-api-key",
            "key",
            "--llm-model",
            "model",
        ]
    )

    assert exit_code == 0
    settings = captured["settings"]
    assert isinstance(settings, LlmCheckSettings)
    assert settings.concurrency == 10


def test_cli_run_defaults_deepseek_and_accepts_llm_guard_args(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_process_documents(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        captured["settings"] = settings
        return {
            "status": "passed",
            "input_path": str(input_path),
            "output_dir": str(output_dir),
            "document_count": 0,
            "documents": [],
        }

    source = tmp_path / "source.md"
    source.write_text("text", encoding="utf-8")
    monkeypatch.setattr("llmcheck.cli.process_documents", fake_process_documents)

    exit_code = main(
        [
            "run",
            "--input",
            str(source),
            "--output-dir",
            str(tmp_path / "out"),
            "--llm-api-url",
            "http://llm.test",
            "--llm-api-key",
            "key",
            "--review-concurrency",
            "4",
            "--patch-concurrency",
            "2",
            "--llm-call-timeout-seconds",
            "120",
            "--llm-stage-timeout-seconds",
            "1800",
            "--llm-idle-timeout-seconds",
            "300",
            "--llm-max-calls-per-book",
            "123",
            "--llm-max-cost-per-book",
            "1.5",
            "--allow-fallback-models",
            "--fallback-models",
            "gpt-5",
        ]
    )

    assert exit_code == 0
    settings = captured["settings"]
    assert isinstance(settings, LlmCheckSettings)
    assert settings.llm_model == "deepseek-v4-pro"
    assert settings.llm_mode == "review-first"
    assert settings.review_concurrency == 4
    assert settings.patch_concurrency == 2
    assert settings.llm_call_timeout_seconds == 120
    assert settings.llm_stage_timeout_seconds == 1800
    assert settings.llm_idle_timeout_seconds == 300
    assert settings.llm_max_calls_per_book == 123
    assert settings.llm_max_cost_per_book == 1.5
    assert settings.allow_fallback_models is True
    assert settings.fallback_models == "gpt-5"


def test_cli_run_accepts_llm_mode(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_process_documents(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        captured["settings"] = settings
        return {
            "status": "passed",
            "input_path": str(input_path),
            "output_dir": str(output_dir),
            "document_count": 0,
            "documents": [],
        }

    source = tmp_path / "source.md"
    source.write_text("text", encoding="utf-8")
    monkeypatch.setattr("llmcheck.cli.process_documents", fake_process_documents)

    exit_code = main(
        [
            "run",
            "--input",
            str(source),
            "--output-dir",
            str(tmp_path / "out"),
            "--llm-api-url",
            "http://llm.test",
            "--llm-api-key",
            "key",
            "--llm-mode",
            "legacy-correction",
        ]
    )

    assert exit_code == 0
    settings = captured["settings"]
    assert isinstance(settings, LlmCheckSettings)
    assert settings.llm_mode == "legacy-correction"


def test_cli_allows_one_thousand_char_llm_chunks(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_process_documents(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        captured["settings"] = settings
        return {
            "status": "passed",
            "input_path": str(input_path),
            "output_dir": str(output_dir),
            "document_count": 0,
            "documents": [],
        }

    source = tmp_path / "source.md"
    source.write_text("text", encoding="utf-8")
    monkeypatch.setattr("llmcheck.cli.process_documents", fake_process_documents)

    exit_code = main(
        [
            "run",
            "--input",
            str(source),
            "--output-dir",
            str(tmp_path / "out"),
            "--llm-api-url",
            "http://llm.test",
            "--llm-api-key",
            "key",
            "--llm-model",
            "model",
            "--llm-chunk-chars",
            "1000",
        ]
    )

    assert exit_code == 0
    settings = captured["settings"]
    assert isinstance(settings, LlmCheckSettings)
    assert settings.llm_chunk_chars == 1000


def test_cli_reads_mineru_api_key_from_dotenv(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_process_documents(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings) -> dict[str, object]:
        captured["settings"] = settings
        return {
            "status": "passed",
            "input_path": str(input_path),
            "output_dir": str(output_dir),
            "document_count": 0,
            "documents": [],
        }

    source = tmp_path / "source.md"
    source.write_text("text", encoding="utf-8")
    (tmp_path / ".env").write_text('MINERU_CLOUD_API_TOKEN="dotenv-token"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINERU_CLOUD_API_TOKEN", raising=False)
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    monkeypatch.delenv("LLMCHECK_MINERU_API_KEY", raising=False)
    monkeypatch.setattr("llmcheck.cli.process_documents", fake_process_documents)

    exit_code = main(
        [
            "run",
            "--input",
            str(source),
            "--output-dir",
            str(tmp_path / "out"),
            "--llm-api-url",
            "http://llm.test",
            "--llm-api-key",
            "key",
            "--llm-model",
            "model",
        ]
    )

    assert exit_code == 0
    settings = captured["settings"]
    assert isinstance(settings, LlmCheckSettings)
    assert settings.mineru_api_key == "dotenv-token"


def test_gui_reads_mineru_segment_status(tmp_path: Path) -> None:
    mineru_dir = tmp_path / "preprocess" / "14鏈崏澶囪璁茶В" / "mineru"
    (mineru_dir / "segment_001").mkdir(parents=True)
    (mineru_dir / "segment_002").mkdir(parents=True)
    (mineru_dir / "segment_001" / "status.json").write_text('{"status":"cached"}\n', encoding="utf-8")
    (mineru_dir / "segment_002" / "status.json").write_text(
        '{"status":"polling","state_counts":{"pending":1}}\n',
        encoding="utf-8",
    )

    status = _read_mineru_segment_status(tmp_path, book_name="14鏈崏澶囪璁茶В.pdf")

    assert status["total"] == 2
    assert status["status_counts"] == {"cached": 1, "polling": 1}
    assert status["cloud_state_counts"] == {"pending": 1}
    assert status["updated_age_seconds"] >= 0


def test_gui_live_progress_includes_book_diagnostics(tmp_path: Path) -> None:
    mineru_dir = tmp_path / "preprocess" / "14鏈崏澶囪璁茶В" / "mineru"
    (mineru_dir / "segment_001").mkdir(parents=True)
    (mineru_dir / "segment_001" / "status.json").write_text(
        '{"status":"polling","state_counts":{"pending":1}}\n',
        encoding="utf-8",
    )
    ppx_markdown = tmp_path / "preprocess" / "14鏈崏澶囪璁茶В" / "ppx" / "clean" / "ppx.md"
    ppx_markdown.parent.mkdir(parents=True)
    ppx_markdown.write_text("ppx audit text", encoding="utf-8")
    job = {"current_output_dir": str(tmp_path), "current_book": "14鏈崏澶囪璁茶В.pdf"}

    result = _with_live_progress(job)

    assert result["mineru_segments"]["total"] == 1
    assert result["diagnostics"]["artifacts"]["ppx_audit_size"] == len("ppx audit text".encode())


def test_write_mineru_segment_status_replaces_existing_json(tmp_path: Path) -> None:
    status_path = tmp_path / "segment_001" / "status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text('{"status":"old"}\n', encoding="utf-8")

    _write_mineru_segment_status(status_path, status="polling", state_counts={"pending": 1})

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["status"] == "polling"
    assert payload["state_counts"] == {"pending": 1}
    assert payload["updated_at"]
    assert not (status_path.parent / "status.json.tmp").exists()


def test_job_store_get_returns_copy() -> None:
    store = JobStore()
    job = {"job_id": "job", "steps": []}
    store._jobs["job"] = job  # noqa: SLF001 - verifies thread-safe public read behavior.

    returned = store.get("job")

    assert returned is not None
    returned["steps"].append({"label": "mutated"})
    assert store.get("job") == {"job_id": "job", "steps": []}


def test_save_uploaded_files_returns_directory_outside_output_for_batch(tmp_path: Path) -> None:
    output = tmp_path / "out"
    payload = {
        "uploaded_files": [
            {"name": "books/a.md", "data_base64": "YQ=="},
            {"name": "books/b.md", "data_base64": "Yg=="},
        ]
    }

    saved = _save_uploaded_files(payload, output_dir=output)

    assert saved is not None
    assert saved.is_dir()
    assert output not in saved.parents
    assert (saved / "books" / "a.md").read_text(encoding="utf-8") == "a"
    assert (saved / "books" / "b.md").read_text(encoding="utf-8") == "b"


def test_gui_html_exposes_directory_output_llm_and_concurrency_controls() -> None:
    html = render_index_html()

    assert 'id="uploaded_files"' in html
    assert 'id="uploaded_directory"' in html
    assert "webkitdirectory" in html
    assert ".png,.jpg,.jpeg,.jp2,.webp,.gif,.bmp,.doc,.docx,.ppt,.pptx,.xls,.xlsx" in html
    assert "supportedSuffixes" in html
    assert "endsWith('.md')" not in html
    assert 'id="input_path"' in html
    assert 'id="output_dir"' in html
    assert 'id="llm_api_url"' in html
    assert 'id="llm_api_key"' in html
    assert 'id="llm_model"' in html
    assert 'id="concurrency"' in html
    assert 'id="book_concurrency"' in html
    assert 'id="start_index"' in html
    assert 'id="limit"' in html
    assert 'id="force"' in html
    assert 'id="llm_chunk_chars"' in html
    assert 'id="mineru_api_key"' in html
    assert 'id="mineru_concurrency"' in html
    assert 'id="mineru_batch_size"' in html
    assert 'id="mineru_timeout_seconds"' in html
    assert 'id="mineru_request_timeout_seconds"' in html
    assert 'id="mineru_max_retries"' in html
    assert 'id="mineru_retry_backoff_seconds"' in html
    assert 'id="ppx_cwd"' in html
    assert 'id="ppx_timeout_seconds"' in html
    assert 'id="ppx_backend"' in html
    assert 'id="ppx_ocr"' in html
    assert 'id="ppx_formula"' in html
    assert "mineru_batch_size: document.getElementById('mineru_batch_size').value" in html
    assert "ppx_backend: document.getElementById('ppx_backend').value" in html
    assert "ppx_ocr: document.getElementById('ppx_ocr').value" in html
    assert "ppx_formula: document.getElementById('ppx_formula').value" in html


def test_gui_exe_launcher_starts_native_desktop(monkeypatch) -> None:
    captured: dict[str, bool] = {}

    def fake_run_desktop_gui() -> int:
        captured["called"] = True
        return 0

    monkeypatch.setattr("llmcheck.gui_exe.run_desktop_gui", fake_run_desktop_gui)

    assert gui_exe.main([]) == 0
    assert captured == {"called": True}


def test_run_guard_rejects_active_lock(tmp_path: Path) -> None:
    from llmcheck.run_guard import RunAlreadyActiveError, acquire_run_lock

    process_dir = tmp_path / "process"
    first = acquire_run_lock(process_dir, run_id="first")
    first.heartbeat(status="running", stage="llm")

    try:
        acquire_run_lock(process_dir, run_id="second")
    except RunAlreadyActiveError as error:
        assert "already active" in str(error)
    else:
        raise AssertionError("active lock should reject second run")


def test_process_documents_writes_finished_run_lock(tmp_path: Path) -> None:
    source = tmp_path / "book.md"
    source.write_text("# Title\n\nBody", encoding="utf-8")

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        client=ReviewClient(accept=False),
    )

    lock = json.loads((tmp_path / "out" / "process" / "llmcheck_run.lock").read_text(encoding="utf-8"))
    assert report["status"] == "review_required"
    assert lock["status"] == "review_required"
    assert lock["stage"] == "finished"
    assert lock["finished_at"]


def test_run_batch_rejects_active_output_lock(tmp_path: Path) -> None:
    from llmcheck.run_guard import RunAlreadyActiveError, acquire_run_lock

    source = tmp_path / "source"
    source.mkdir()
    (source / "a.md").write_text("a", encoding="utf-8")
    output = tmp_path / "output"
    lock = acquire_run_lock(output / "process", run_id="active")
    lock.heartbeat(status="running", stage="books")

    try:
        run_batch(
            source_dir=source,
            output_dir=output,
            settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
            runner=lambda **_kwargs: {"status": "passed"},
        )
    except RunAlreadyActiveError as error:
        assert "already active" in str(error)
    else:
        raise AssertionError("active batch lock should reject second run")


def test_cli_inspect_summarizes_output(tmp_path: Path, capsys) -> None:
    output = tmp_path / "out"
    reports = output / "process" / "reports"
    reports.mkdir(parents=True)
    (reports / "llmcheck_summary.json").write_text(json.dumps({"status": "review_required", "document_count": 1}) + "\n", encoding="utf-8")
    (reports / "llmcheck_manifest.jsonl").write_text(
        json.dumps({"document_id": "doc", "status": "llm_review_error", "error": "HTTP Error 401", "final_markdown_path": "", "text_pdf_path": ""}) + "\n",
        encoding="utf-8",
    )
    (output / "process" / "heartbeat.json").write_text(json.dumps({"status": "review_required", "stage": "finished"}) + "\n", encoding="utf-8")
    (output / "process" / "llm_calls.jsonl").write_text(json.dumps({"status": "error", "stage": "llm_review", "error": "HTTP Error 401"}) + "\n", encoding="utf-8")

    exit_code = main(["inspect", "--book-output-dir", str(output)])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["summary_status"] == "review_required"
    assert payload["documents"][0]["status"] == "llm_review_error"
    assert payload["llm_calls"]["status_counts"] == {"error": 1}
    assert payload["llm_calls"]["latest_error"] == "HTTP Error 401"


def test_cli_review_cost_and_quality_reports_summarize_output(tmp_path: Path, capsys) -> None:
    output = tmp_path / "out"
    process = output / "process"
    reports = process / "reports"
    clean = process / "clean"
    reports.mkdir(parents=True)
    clean.mkdir(parents=True)
    (reports / "llmcheck_manifest.jsonl").write_text(
        json.dumps({"document_id": "doc", "status": "llm_review_failed"}) + "\n",
        encoding="utf-8",
    )
    (reports / "doc.llm_review.json").write_text(
        json.dumps(
            {
                "status": "needs_revision",
                "accepted": False,
                "issues": [
                    {
                        "id": "i1",
                        "category": "latex_artifact",
                        "severity": "major",
                        "safe_fix_type": "rule_fix",
                        "excerpt": "$9\\mathrm{g}$",
                        "reason": "unit residue",
                    }
                ],
                "manual_review_notes": ["check dosage"],
            }
        ) + "\n",
        encoding="utf-8",
    )
    (reports / "doc.safe_patch.json").write_text(
        json.dumps({"status": "manual_review_required", "applied_patch_count": 0, "skipped_patch_count": 1, "unresolved_issues": [{"id": "i1"}]}) + "\n",
        encoding="utf-8",
    )
    (process / "cost_report.json").write_text(json.dumps({"model": "deepseek-v4-pro", "llm_call_count": 1, "estimated_cost": None, "currency": "unknown", "pricing_available": False}) + "\n", encoding="utf-8")
    (process / "llm_calls.jsonl").write_text(json.dumps({"status": "ok", "stage": "llm_review", "model": "deepseek-v4-pro", "duration_seconds": 1.25}) + "\n", encoding="utf-8")
    (reports / "doc.quality.json").write_text(json.dumps({"profile_id": "general_standard_document", "errors": ["latex_unit_residue"], "hints": ["short_document"]}) + "\n", encoding="utf-8")
    (clean / "doc.cleaning_report.json").write_text(json.dumps({"rule_changes": [{"rule_id": "latex.unit_math_to_text"}]}) + "\n", encoding="utf-8")
    (reports / "doc.final_acceptance.json").write_text(json.dumps({"accepted": False, "blocking_errors": ["latex_unit_residue"]}) + "\n", encoding="utf-8")

    assert main(["review-report", "--book-output-dir", str(output)]) == 0
    review_payload = json.loads(capsys.readouterr().out)
    assert review_payload["issue_counts"] == {"latex_artifact": 1}
    assert review_payload["documents"][0]["safe_patch_status"] == "manual_review_required"

    assert main(["cost-summary", "--book-output-dir", str(output)]) == 0
    cost_payload = json.loads(capsys.readouterr().out)
    assert cost_payload["model"] == "deepseek-v4-pro"
    assert cost_payload["stage_counts"] == {"llm_review": 1}
    assert cost_payload["status_counts"] == {"ok": 1}

    assert main(["quality-report", "--book-output-dir", str(output)]) == 0
    quality_payload = json.loads(capsys.readouterr().out)
    assert quality_payload["error_counts"] == {"latex_unit_residue": 1}
    assert quality_payload["rule_change_counts"] == {"latex.unit_math_to_text": 1}
    assert quality_payload["final_acceptance_status_counts"] == {"needs_revision": 1}

