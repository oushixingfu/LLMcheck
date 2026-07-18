from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from llmcheck.agent_api import (
    SCHEMA_VERSION,
    get_final_markdown,
    get_job,
    list_profiles,
    normalize_job_report,
    submit_convert,
)
from llmcheck.pipeline import LlmCheckError, LlmCheckSettings


def test_list_profiles_envelope() -> None:
    payload = list_profiles()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["default_profile_id"]
    assert isinstance(payload["profiles"], list)
    assert any(row.get("id") == "general_standard_document" for row in payload["profiles"])


def test_normalize_report_schema_version(tmp_path: Path) -> None:
    raw = {
        "status": "review_required",
        "profile_id": "technical_manual",
        "documents": [
            {
                "document_id": "book-a",
                "status": "review_required",
                "final_markdown_path": "",
                "error": "needs review",
            }
        ],
        "output_dir": str(tmp_path),
    }
    report = normalize_job_report(raw, output_dir=tmp_path, job_id="job-1")
    assert report["schema_version"] == SCHEMA_VERSION
    assert report["job_id"] == "job-1"
    assert report["status"] == "review_required"
    assert report["profile_id"] == "technical_manual"
    assert report["artifacts"]["md_dir"] == str(tmp_path / "md")
    assert report["artifacts"]["process_dir"] == str(tmp_path / "process")
    assert report["documents"][0]["document_id"] == "book-a"
    assert report["documents"][0]["cross_report_path"] == ""


def test_get_final_markdown_denies_non_passed(tmp_path: Path) -> None:
    md_dir = tmp_path / "md"
    md_dir.mkdir(parents=True)
    leaked = md_dir / "draft-like.md"
    leaked.write_text("should not be returned\n", encoding="utf-8")
    process_reports = tmp_path / "process" / "reports"
    process_reports.mkdir(parents=True)
    report = {
        "schema_version": SCHEMA_VERSION,
        "job_id": "job-deny",
        "status": "review_required",
        "profile_id": "general_standard_document",
        "documents": [
            {
                "document_id": "book-a",
                "status": "review_required",
                "final_markdown_path": str(leaked),
                "final_markdown_sha256": "",
                "final_acceptance_report_path": "",
                "cross_report_path": "",
                "error": "gate failed",
            }
        ],
        "artifacts": {
            "md_dir": str(md_dir),
            "process_dir": str(tmp_path / "process"),
        },
        "output_dir": str(tmp_path),
    }
    (process_reports / "agent_job_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    payload = get_final_markdown(output_dir=tmp_path, document_id="book-a")
    assert payload["status"] == "review_required"
    assert payload["document_id"] == "book-a"
    assert "text" not in payload
    assert "error" in payload


def test_get_final_markdown_allows_passed(tmp_path: Path) -> None:
    md_dir = tmp_path / "md"
    md_dir.mkdir(parents=True)
    final = md_dir / "book-a.md"
    body = "# Title\n\nhello agent\n"
    final.write_text(body, encoding="utf-8")
    process_reports = tmp_path / "process" / "reports"
    process_reports.mkdir(parents=True)
    report = {
        "schema_version": SCHEMA_VERSION,
        "job_id": "job-pass",
        "status": "passed",
        "profile_id": "general_standard_document",
        "documents": [
            {
                "document_id": "book-a",
                "status": "passed",
                "final_markdown_path": str(final),
                "final_markdown_sha256": "abc",
                "final_acceptance_report_path": "",
                "cross_report_path": "",
                "error": "",
            }
        ],
        "artifacts": {
            "md_dir": str(md_dir),
            "process_dir": str(tmp_path / "process"),
        },
        "output_dir": str(tmp_path),
    }
    (process_reports / "agent_job_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    payload = get_final_markdown(output_dir=tmp_path, document_id="book-a")
    assert payload["status"] == "passed"
    assert payload["document_id"] == "book-a"
    assert payload["path"] == str(final.resolve())
    assert payload["text"] == body
    assert payload["char_count"] == len(body)
    assert payload["truncated"] is False

    truncated = get_final_markdown(output_dir=tmp_path, document_id="book-a", max_chars=5)
    assert truncated["truncated"] is True
    assert truncated["text"] == body[:5]


def test_get_job_falls_back_to_summary(tmp_path: Path) -> None:
    process_reports = tmp_path / "process" / "reports"
    process_reports.mkdir(parents=True)
    summary = {
        "status": "passed",
        "profile_id": "academic_paper",
        "documents": [
            {
                "document_id": "paper",
                "status": "passed",
                "final_markdown_path": str(tmp_path / "md" / "paper.md"),
                "final_markdown_sha256": "x",
            }
        ],
        "output_dir": str(tmp_path),
    }
    (process_reports / "llmcheck_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report = get_job(output_dir=tmp_path)
    assert report["schema_version"] == SCHEMA_VERSION
    assert report["status"] == "passed"
    assert report["profile_id"] == "academic_paper"
    assert report["job_id"]
    assert report["documents"][0]["document_id"] == "paper"


def test_submit_convert_normalizes_and_persists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "input.md"
    source.write_text("# hi\n", encoding="utf-8")
    output_dir = tmp_path / "out"

    def fake_process_documents(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["input_path"] == source
        assert kwargs["output_dir"] == output_dir
        assert isinstance(kwargs["settings"], LlmCheckSettings)
        return {
            "status": "passed",
            "profile_id": kwargs["settings"].profile_id,
            "output_dir": str(output_dir),
            "documents": [
                {
                    "document_id": "input",
                    "status": "passed",
                    "final_markdown_path": str(output_dir / "md" / "input.md"),
                    "final_markdown_sha256": "deadbeef",
                    "final_acceptance_report_path": "",
                    "error": "",
                }
            ],
        }

    report = submit_convert(
        input_path=source,
        output_dir=output_dir,
        process_runner=fake_process_documents,
        llm_mode="local-gate",
        profile_id="general_standard_document",
        job_id="fixed-job",
    )
    assert report["schema_version"] == SCHEMA_VERSION
    assert report["job_id"] == "fixed-job"
    assert report["status"] == "passed"
    assert report["documents"][0]["document_id"] == "input"

    persisted = output_dir / "process" / "reports" / "agent_job_report.json"
    assert persisted.is_file()
    loaded = json.loads(persisted.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == SCHEMA_VERSION
    assert loaded["job_id"] == "fixed-job"


def test_submit_convert_missing_input(tmp_path: Path) -> None:
    with pytest.raises(LlmCheckError):
        submit_convert(
            input_path=tmp_path / "missing.md",
            output_dir=tmp_path / "out",
            llm_mode="local-gate",
        )
