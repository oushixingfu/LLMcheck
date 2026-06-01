from __future__ import annotations

from pathlib import Path
import hashlib
import json

from llmcheck.pipeline import LlmCheckSettings, correct_text_concurrently, process_documents, repair_failed_acceptance_chunks
from llmcheck.profiles import get_profile


class ProfilePipelineClient:
    def __init__(self, corrected_text: str = "# 标题\n\n正文第一段。\n") -> None:
        self.corrected_text = corrected_text
        self.prompts: list[str] = []

    def complete_json(self, prompt: str) -> dict[str, object]:
        self.prompts.append(prompt)
        if '"corrected_text"' in prompt:
            return {
                "status": "draft_ready",
                "confidence": 0.9,
                "summary": "已完成纠错",
                "corrected_text": self.corrected_text,
                "changes": [],
                "unresolved_issues": [],
            }
        return {
            "status": "passed",
            "confidence": 0.9,
            "summary": "片段可交付",
            "blocking_issues": [],
            "non_blocking_notes": [],
        }


class FreshCacheClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, prompt: str) -> dict[str, object]:
        self.calls += 1
        if '"repaired_text"' in prompt:
            text = prompt.split("<TEXT_BEGIN>\n", 1)[1].split("\n<TEXT_END>", 1)[0]
            return {
                "status": "repaired",
                "confidence": 0.9,
                "summary": "fresh repair",
                "repaired_text": text.replace("old", "fresh"),
                "changes": [],
                "unresolved_issues": [],
            }
        return {
            "status": "draft_ready",
            "confidence": 0.9,
            "summary": "fresh correction",
            "corrected_text": "fresh profile text.\n",
            "changes": [],
            "unresolved_issues": [],
        }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_process_documents_records_profile_and_final_reports(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "input"
    source.mkdir()
    (source / "manual.md").write_text("# 标题\n\n正文第一段。\n", encoding="utf-8")

    def fake_write_text_pdf(path: Path, *, title: str, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr("llmcheck.pipeline.write_text_pdf", fake_write_text_pdf)

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(
            llm_api_url="http://llm.test",
            llm_api_key="key",
            llm_model="model",
            profile_id="technical_manual",
        ),
        client=ProfilePipelineClient(),
    )

    row = report["documents"][0]
    assert report["profile_id"] == "technical_manual"
    assert row["profile_id"] == "technical_manual"
    assert Path(row["finalization_report_path"]).exists()
    assert Path(row["final_acceptance_report_path"]).exists()
    final_acceptance = json.loads(Path(row["final_acceptance_report_path"]).read_text(encoding="utf-8"))
    assert final_acceptance["accepted"] is True


def test_process_documents_blocks_final_output_when_final_acceptance_fails(tmp_path: Path) -> None:
    source = tmp_path / "input"
    source.mkdir()
    (source / "bad.md").write_text("这是锟斤拷文本。\n", encoding="utf-8")

    report = process_documents(
        input_path=source,
        output_dir=tmp_path / "out",
        settings=LlmCheckSettings(llm_api_url="http://llm.test", llm_api_key="key", llm_model="model"),
        client=ProfilePipelineClient(corrected_text="这是锟斤拷文本。\n"),
    )

    row = report["documents"][0]
    assert report["status"] == "review_required"
    assert row["status"] == "final_acceptance_failed"
    assert row["final_markdown_path"] == ""
    assert not list((tmp_path / "out" / "md").glob("*.md"))


def test_correct_text_concurrently_ignores_cached_chunk_from_other_profile(tmp_path: Path) -> None:
    chunk_dir = tmp_path / "chunks"
    chunk_dir.mkdir()
    text = "source profile text.\n"
    (chunk_dir / "correction_chunk_001.json").write_text(
        json.dumps(
            {
                "input_sha256": _sha256(text),
                "profile_id": "general_standard_document",
                "draft_ready": True,
                "llm_result": {"corrected_text": "cached wrong profile.\n"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = FreshCacheClient()

    result = correct_text_concurrently(
        source_name="manual.md",
        text_path=Path("manual.md"),
        text=text,
        client=client,
        model="model",
        concurrency=1,
        max_chars=1000,
        chunk_report_dir=chunk_dir,
        profile=get_profile("technical_manual"),
    )

    assert client.calls == 1
    assert result["profile_id"] == "technical_manual"
    assert result["corrected_text"] == "fresh profile text.\n"


def test_repair_failed_acceptance_chunks_ignores_cached_chunk_from_other_profile(tmp_path: Path) -> None:
    repair_dir = tmp_path / "repair"
    repair_dir.mkdir()
    text = "old repair text.\n"
    (repair_dir / "repair_chunk_001.json").write_text(
        json.dumps(
            {
                "input_sha256": _sha256(text),
                "profile_id": "general_standard_document",
                "repaired": True,
                "llm_result": {"repaired_text": "cached wrong profile.\n"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = FreshCacheClient()
    acceptance = {
        "accepted": False,
        "chunks": [{"chunk_index": 1, "accepted": False, "llm_result": {"blocking_issues": [{"category": "layout"}]}}],
    }

    result = repair_failed_acceptance_chunks(
        source_name="manual.md",
        text_path=Path("manual.md"),
        text=text,
        acceptance=acceptance,
        client=client,
        model="model",
        concurrency=1,
        max_chars=1000,
        repair_rounds=1,
        repair_report_dir=repair_dir,
        profile=get_profile("technical_manual"),
    )

    assert client.calls == 1
    assert result["repaired_text"] == "fresh repair text.\n"
