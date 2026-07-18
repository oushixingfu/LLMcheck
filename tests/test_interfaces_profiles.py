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


def test_cli_batch_accepts_explicit_process_and_output_md_dirs(tmp_path: Path, monkeypatch) -> None:
    source_dir = tmp_path / "output2" / "0"
    source_dir.mkdir(parents=True)
    seen: dict[str, object] = {}

    def fake_run_batch(**kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return {"status": "passed", "documents": []}

    monkeypatch.setattr("llmcheck.cli.run_batch", fake_run_batch)

    code = main(
        [
            "batch",
            "--input",
            str(source_dir),
            "--process-dir",
            str(tmp_path / "output2" / "process"),
            "--output-md-dir",
            str(tmp_path / "output2" / "md"),
            "--llm-api-url",
            "http://llm.test",
            "--llm-api-key",
            "key",
            "--llm-model",
            "mimo-v2.5-pro",
            "--fallback-models",
            "gpt-5.5",
        ]
    )

    assert code == 0
    assert seen["source_dir"] == source_dir
    assert seen["output_dir"] == tmp_path / "output2"
    assert seen["process_dir"] == tmp_path / "output2" / "process"
    assert seen["final_md_dir"] == tmp_path / "output2" / "md"


def test_cli_batch_accepts_prd_operation_flags(tmp_path: Path, monkeypatch) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    seen: dict[str, object] = {}

    def fake_run_batch(**kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return {"status": "dry_run", "documents": []}

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
            "mimo-v2.5-pro",
            "--fallback-models",
            "gpt-5.5",
            "--dry-run",
            "--preflight-only",
            "--skip-existing",
            "--mineru-fallback",
            "ppx",
            "--max-cleanup-loops",
            "3",
        ]
    )

    settings = seen["settings"]
    assert code == 0
    assert seen["dry_run"] is True
    assert seen["preflight_only"] is True
    assert seen["skip_existing"] is True
    assert isinstance(settings, LlmCheckSettings)
    assert settings.mineru_fallback == "ppx"
    assert settings.max_cleanup_loops == 3


def test_cli_batch_dry_run_does_not_require_llm_credentials(tmp_path: Path, monkeypatch) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    seen: dict[str, object] = {}

    def fake_run_batch(**kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return {"status": "dry_run", "documents": []}

    monkeypatch.delenv("LLMCHECK_LLM_API_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("LLMCHECK_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("llmcheck.cli.run_batch", fake_run_batch)

    code = main(
        [
            "batch",
            "--source-dir",
            str(source_dir),
            "--output-dir",
            str(tmp_path / "out"),
            "--dry-run",
        ]
    )

    settings = seen["settings"]
    assert code == 0
    assert seen["dry_run"] is True
    assert isinstance(settings, LlmCheckSettings)
    assert settings.llm_api_url == ""
    assert settings.llm_api_key == ""


def test_cli_batch_local_gate_does_not_require_llm_credentials_or_preflight(tmp_path: Path, monkeypatch) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    seen: dict[str, object] = {}

    def fake_run_batch(**kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return {"status": "passed", "documents": []}

    monkeypatch.delenv("LLMCHECK_LLM_API_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("LLMCHECK_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("llmcheck.cli.run_batch", fake_run_batch)

    code = main(
        [
            "batch",
            "--source-dir",
            str(source_dir),
            "--output-dir",
            str(tmp_path / "out"),
            "--llm-mode",
            "local-gate",
        ]
    )

    settings = seen["settings"]
    assert code == 0
    assert seen["llm_preflight"] is None
    assert isinstance(settings, LlmCheckSettings)
    assert settings.llm_mode == "local-gate"
    assert settings.llm_api_url == ""
    assert settings.llm_api_key == ""


def test_cli_model_compare_accepts_models_and_profile(tmp_path: Path, monkeypatch) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    seen: dict[str, object] = {}

    def fake_run_model_compare(**kwargs: object) -> dict[str, object]:
        settings = kwargs["settings"]
        assert isinstance(settings, LlmCheckSettings)
        seen["models"] = kwargs["models"]
        seen["preferred_model"] = kwargs["preferred_model"]
        seen["fallback_model"] = kwargs["fallback_model"]
        seen["limit"] = kwargs["limit"]
        seen["profile_id"] = settings.profile_id
        seen["llm_model_api_urls"] = settings.llm_model_api_urls
        return {
            "status": "passed",
            "recommendation": {"preferred_model": "mimo-v2.5-pro", "fallback_model": "gpt-5.5"},
        }

    monkeypatch.setattr("llmcheck.cli.run_model_compare", fake_run_model_compare)

    code = main(
        [
            "model-compare",
            "--source-dir",
            str(source_dir),
            "--output-dir",
            str(tmp_path / "out"),
            "--llm-api-url",
            "http://llm.test",
            "--llm-api-key",
            "key",
            "--models",
            "mimo-v2.5-pro,gpt-5.5",
            "--preferred-model",
            "mimo-v2.5-pro",
            "--fallback-model",
            "gpt-5.5",
            "--limit",
            "3",
            "--profile",
            "chinese_medicine_reference",
        ]
    )

    assert code == 0
    assert seen["models"] == ["mimo-v2.5-pro", "gpt-5.5"]
    assert seen["preferred_model"] == "mimo-v2.5-pro"
    assert seen["fallback_model"] == "gpt-5.5"
    assert seen["limit"] == 3
    assert seen["profile_id"] == "chinese_medicine_reference"
    assert "mimo-v2.5-pro=https://api.iaigc.fun/v1" in str(seen["llm_model_api_urls"])
    assert "gpt-5.5=http://127.0.0.1:3022" in str(seen["llm_model_api_urls"])


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
