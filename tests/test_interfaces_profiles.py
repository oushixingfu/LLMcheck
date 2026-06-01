from __future__ import annotations

from pathlib import Path
import asyncio
import json

from llmcheck.cli import main
from llmcheck.gui import JobStore, create_app, render_index_html
from llmcheck.pipeline import LlmCheckSettings


def test_cli_profiles_command_lists_builtin_profiles(capsys) -> None:
    code = main(["profiles"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["default_profile_id"] == "general_standard_document"
    assert any(profile["id"] == "technical_manual" for profile in payload["profiles"])


def test_cli_run_accepts_profile_argument(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "input.md"
    source.write_text("# 标题\n\n正文。\n", encoding="utf-8")
    seen: dict[str, str] = {}

    def fake_process_documents(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings):
        seen["profile_id"] = settings.profile_id
        return {"status": "passed", "documents": [], "profile_id": settings.profile_id}

    monkeypatch.setattr("llmcheck.cli.process_documents", fake_process_documents)

    code = main(
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
            "--profile",
            "technical_manual",
        ]
    )

    assert code == 0
    assert seen["profile_id"] == "technical_manual"


def test_cli_run_uses_default_profile(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "input.md"
    source.write_text("# 标题\n\n正文。\n", encoding="utf-8")
    seen: dict[str, str] = {}

    def fake_process_documents(*, input_path: Path, output_dir: Path, settings: LlmCheckSettings):
        seen["profile_id"] = settings.profile_id
        return {"status": "passed", "documents": [], "profile_id": settings.profile_id}

    monkeypatch.setattr("llmcheck.cli.process_documents", fake_process_documents)

    code = main(
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

    assert code == 0
    assert seen["profile_id"] == "general_standard_document"


def test_cli_batch_accepts_profile_argument(tmp_path: Path, monkeypatch) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    seen: dict[str, str] = {}

    def fake_run_batch(*, source_dir: Path, output_dir: Path, settings: LlmCheckSettings, **_kwargs: object):
        seen["profile_id"] = settings.profile_id
        return {"status": "passed", "documents": [], "profile_id": settings.profile_id}

    monkeypatch.setattr("llmcheck.cli.run_batch", fake_run_batch)

    code = main(
        [
            "batch",
            "--source-dir",
            str(source_dir),
            "--output-dir",
            str(tmp_path / "out"),
            "--llm-api-url",
            "http://llm.test",
            "--llm-api-key",
            "key",
            "--llm-model",
            "model",
            "--profile",
            "technical_manual",
        ]
    )

    assert code == 0
    assert seen["profile_id"] == "technical_manual"


def test_cli_batch_uses_default_profile(tmp_path: Path, monkeypatch) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    seen: dict[str, str] = {}

    def fake_run_batch(*, source_dir: Path, output_dir: Path, settings: LlmCheckSettings, **_kwargs: object):
        seen["profile_id"] = settings.profile_id
        return {"status": "passed", "documents": [], "profile_id": settings.profile_id}

    monkeypatch.setattr("llmcheck.cli.run_batch", fake_run_batch)

    code = main(
        [
            "batch",
            "--source-dir",
            str(source_dir),
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

    assert code == 0
    assert seen["profile_id"] == "general_standard_document"


def test_gui_html_contains_profile_selector() -> None:
    html = render_index_html()

    assert 'name="profile"' in html
    assert 'id="profile"' in html
    assert "general_standard_document" in html
    assert "technical_manual" in html


def test_gui_jobs_reads_profile_from_payload(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "input.md"
    source.write_text("# 标题\n\n正文。\n", encoding="utf-8")
    seen: dict[str, str] = {}

    def fake_start(
        self: JobStore,
        *,
        input_path: Path,
        output_dir: Path,
        settings: LlmCheckSettings,
        book_concurrency: int,
        start_index: int,
        limit: int,
        force: bool,
    ) -> dict[str, object]:
        seen["profile_id"] = settings.profile_id
        return {"job_id": "job-1", "status": "running", "progress_percent": 5}

    monkeypatch.setattr("llmcheck.gui.JobStore.start", fake_start)

    app = create_app()
    create_job = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/jobs")

    class FakeRequest:
        async def json(self) -> dict[str, object]:
            return {
            "input_path": str(source),
            "output_dir": str(tmp_path / "out"),
            "llm_api_url": "http://llm.test",
            "llm_api_key": "key",
            "llm_model": "model",
            "profile": "technical_manual",
            }

    response = asyncio.run(create_job(FakeRequest()))

    assert response.status_code == 200
    assert json.loads(response.body)["job_id"] == "job-1"
    assert seen["profile_id"] == "technical_manual"
