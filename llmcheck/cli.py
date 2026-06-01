from __future__ import annotations

from pathlib import Path
import argparse
import dataclasses
import json
import os
import sys

from llmcheck.batch import run_batch
from llmcheck.diagnostics import summarize_book_output
from llmcheck.gui import create_app
from llmcheck.pipeline import (
    DEFAULT_LLM_CHUNK_CHARS,
    DEFAULT_LLM_CONCURRENCY,
    MIN_LLM_CHUNK_CHARS,
    LlmCheckError,
    LlmCheckSettings,
    process_documents,
)
from llmcheck.preprocess import (
    DEFAULT_MINERU_BATCH_SIZE,
    DEFAULT_MINERU_MAX_RETRIES,
    DEFAULT_MINERU_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_MINERU_RETRY_BACKOFF_SECONDS,
    DEFAULT_PDF_PAGE_CHUNK_SIZE,
)

try:
    from llmcheck.profiles import DEFAULT_PROFILE_ID, get_profile, list_profiles
except ModuleNotFoundError:
    DEFAULT_PROFILE_ID = "general_standard_document"

    class _FallbackProfile:
        def __init__(self, profile_id: str) -> None:
            self.id = profile_id

    def get_profile(profile_id: str | None = None) -> _FallbackProfile:
        normalized = (profile_id or DEFAULT_PROFILE_ID).strip() or DEFAULT_PROFILE_ID
        ids = {profile["id"] for profile in list_profiles()}
        if normalized not in ids:
            raise ValueError(f"未知文档 profile: {normalized}. 可用 profile: {', '.join(sorted(ids))}")
        return _FallbackProfile(normalized)

    def list_profiles() -> list[dict[str, object]]:
        return [
            {"id": "general_standard_document", "label": "通用标准文档", "description": "通用文档默认配置"},
            {"id": "technical_manual", "label": "技术手册", "description": "技术手册与工程规范"},
        ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="llmcheck", description="Markdown LLM correction and acceptance workflow.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    profile_choices = [str(profile["id"]) for profile in list_profiles()]

    run = subparsers.add_parser("run", help="Run LLMcheck on a Markdown file or directory.")
    run.add_argument("--input", required=True, type=Path, help="Supported file or directory: md/pdf/image/Office.")
    run.add_argument("--output-dir", required=True, type=Path)
    run.add_argument("--llm-api-url", required=True)
    run.add_argument("--llm-api-key", required=True)
    run.add_argument("--llm-model", required=True)
    run.add_argument("--concurrency", type=int, default=DEFAULT_LLM_CONCURRENCY)
    run.add_argument("--llm-chunk-chars", type=int, default=DEFAULT_LLM_CHUNK_CHARS)
    run.add_argument("--acceptance-repair-rounds", type=int, default=1)
    run.add_argument("--timeout-seconds", type=int, default=600)
    run.add_argument("--mineru-api-url", default="https://mineru.net")
    run.add_argument("--mineru-api-key", default="")
    run.add_argument("--mineru-concurrency", type=int, default=12)
    run.add_argument("--mineru-batch-size", type=int, default=DEFAULT_MINERU_BATCH_SIZE)
    run.add_argument("--mineru-timeout-seconds", type=int, default=3600)
    run.add_argument("--mineru-request-timeout-seconds", type=int, default=DEFAULT_MINERU_REQUEST_TIMEOUT_SECONDS)
    run.add_argument("--mineru-max-retries", type=int, default=DEFAULT_MINERU_MAX_RETRIES)
    run.add_argument("--mineru-retry-backoff-seconds", type=float, default=DEFAULT_MINERU_RETRY_BACKOFF_SECONDS)
    run.add_argument("--pdf-page-chunk-size", type=int, default=DEFAULT_PDF_PAGE_CHUNK_SIZE)
    run.add_argument("--ppx-command", default="/mnt/d/codex/memect-ppx/ppx")
    run.add_argument("--ppx-cwd", default="/mnt/d/codex/memect-ppx")
    run.add_argument("--ppx-timeout-seconds", type=int, default=3600)
    run.add_argument("--ppx-backend", default="default")
    run.add_argument("--ppx-ocr", default="auto")
    run.add_argument("--ppx-formula", default="no")
    run.add_argument("--profile", default=DEFAULT_PROFILE_ID, choices=profile_choices)

    batch = subparsers.add_parser("batch", help="Run LLMcheck one book at a time for a source directory.")
    batch.add_argument("--source-dir", required=True, type=Path)
    batch.add_argument("--output-dir", required=True, type=Path)
    batch.add_argument("--llm-api-url", required=True)
    batch.add_argument("--llm-api-key", required=True)
    batch.add_argument("--llm-model", required=True)
    batch.add_argument("--concurrency", type=int, default=DEFAULT_LLM_CONCURRENCY, help="LLM per-book concurrency.")
    batch.add_argument("--llm-chunk-chars", type=int, default=DEFAULT_LLM_CHUNK_CHARS)
    batch.add_argument("--acceptance-repair-rounds", type=int, default=1)
    batch.add_argument("--book-concurrency", type=int, default=1, help="Book-level supervisor concurrency. Use 1 for one-by-one.")
    batch.add_argument("--start-index", type=int, default=1)
    batch.add_argument("--limit", type=int, default=0)
    batch.add_argument("--timeout-seconds", type=int, default=600)
    batch.add_argument("--mineru-api-url", default="https://mineru.net")
    batch.add_argument("--mineru-api-key", default="")
    batch.add_argument("--mineru-concurrency", type=int, default=12)
    batch.add_argument("--mineru-batch-size", type=int, default=DEFAULT_MINERU_BATCH_SIZE)
    batch.add_argument("--mineru-timeout-seconds", type=int, default=3600)
    batch.add_argument("--mineru-request-timeout-seconds", type=int, default=DEFAULT_MINERU_REQUEST_TIMEOUT_SECONDS)
    batch.add_argument("--mineru-max-retries", type=int, default=DEFAULT_MINERU_MAX_RETRIES)
    batch.add_argument("--mineru-retry-backoff-seconds", type=float, default=DEFAULT_MINERU_RETRY_BACKOFF_SECONDS)
    batch.add_argument("--pdf-page-chunk-size", type=int, default=DEFAULT_PDF_PAGE_CHUNK_SIZE)
    batch.add_argument("--ppx-command", default="/mnt/d/codex/memect-ppx/ppx")
    batch.add_argument("--ppx-cwd", default="/mnt/d/codex/memect-ppx")
    batch.add_argument("--ppx-timeout-seconds", type=int, default=3600)
    batch.add_argument("--ppx-backend", default="default")
    batch.add_argument("--ppx-ocr", default="auto")
    batch.add_argument("--ppx-formula", default="no")
    batch.add_argument("--profile", default=DEFAULT_PROFILE_ID, choices=profile_choices)
    batch.add_argument("--force", action="store_true")

    gui = subparsers.add_parser("gui", help="Start the LLMcheck GUI server.")
    gui.add_argument("--host", default="127.0.0.1")
    gui.add_argument("--port", type=int, default=8766)

    mineru_status = subparsers.add_parser("mineru-status", help="Summarize MinerU segment progress for one book output directory.")
    mineru_status.add_argument("--book-output-dir", required=True, type=Path)
    mineru_status.add_argument("--book-name", default="")
    subparsers.add_parser("profiles", help="List built-in document profiles.")

    args = parser.parse_args(argv)
    if args.command == "profiles":
        print(json.dumps({"default_profile_id": DEFAULT_PROFILE_ID, "profiles": list_profiles()}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "mineru-status":
        print(json.dumps(summarize_book_output(args.book_output_dir, book_name=args.book_name), ensure_ascii=False, indent=2))
        return 0
    if args.command == "run":
        try:
            report = process_documents(
                input_path=args.input,
                output_dir=args.output_dir,
                settings=_make_settings(
                    llm_api_url=args.llm_api_url,
                    llm_api_key=args.llm_api_key,
                    llm_model=args.llm_model,
                    concurrency=max(1, args.concurrency),
                    llm_chunk_chars=max(MIN_LLM_CHUNK_CHARS, args.llm_chunk_chars),
                    acceptance_repair_rounds=max(0, args.acceptance_repair_rounds),
                    timeout_seconds=max(10, args.timeout_seconds),
                    mineru_api_url=args.mineru_api_url,
                    mineru_api_key=_mineru_api_key(args),
                    mineru_model="vlm",
                    mineru_concurrency=max(1, args.mineru_concurrency),
                    mineru_batch_size=max(1, args.mineru_batch_size),
                    mineru_timeout_seconds=max(10, args.mineru_timeout_seconds),
                    mineru_request_timeout_seconds=max(10, args.mineru_request_timeout_seconds),
                    mineru_max_retries=max(1, args.mineru_max_retries),
                    mineru_retry_backoff_seconds=max(0.0, args.mineru_retry_backoff_seconds),
                    pdf_page_chunk_size=max(1, args.pdf_page_chunk_size),
                    ppx_command=args.ppx_command,
                    ppx_cwd=args.ppx_cwd,
                    ppx_timeout_seconds=max(10, args.ppx_timeout_seconds),
                    ppx_backend=args.ppx_backend,
                    ppx_ocr=args.ppx_ocr,
                    ppx_formula=args.ppx_formula,
                    **_profile_settings_kwargs(args.profile),
                ),
            )
        except LlmCheckError as error:
            print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "passed" else 1
    if args.command == "batch":
        report = run_batch(
            source_dir=args.source_dir,
            output_dir=args.output_dir,
            settings=_settings_from_args(args),
            book_concurrency=max(1, args.book_concurrency),
            start_index=max(1, args.start_index),
            limit=max(0, args.limit),
            force=args.force,
            progress_callback=_print_progress_event,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "passed" else 1
    if args.command == "gui":
        import uvicorn

        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


def _settings_from_args(args: argparse.Namespace) -> LlmCheckSettings:
    return _make_settings(
        llm_api_url=args.llm_api_url,
        llm_api_key=args.llm_api_key,
        llm_model=args.llm_model,
        concurrency=max(1, args.concurrency),
        llm_chunk_chars=max(MIN_LLM_CHUNK_CHARS, args.llm_chunk_chars),
        acceptance_repair_rounds=max(0, args.acceptance_repair_rounds),
        timeout_seconds=max(10, args.timeout_seconds),
        mineru_api_url=args.mineru_api_url,
        mineru_api_key=_mineru_api_key(args),
        mineru_model="vlm",
        mineru_concurrency=max(1, args.mineru_concurrency),
        mineru_batch_size=max(1, args.mineru_batch_size),
        mineru_timeout_seconds=max(10, args.mineru_timeout_seconds),
        mineru_request_timeout_seconds=max(10, args.mineru_request_timeout_seconds),
        mineru_max_retries=max(1, args.mineru_max_retries),
        mineru_retry_backoff_seconds=max(0.0, args.mineru_retry_backoff_seconds),
        pdf_page_chunk_size=max(1, args.pdf_page_chunk_size),
        ppx_command=args.ppx_command,
        ppx_cwd=args.ppx_cwd,
        ppx_timeout_seconds=max(10, args.ppx_timeout_seconds),
        ppx_backend=args.ppx_backend,
        ppx_ocr=args.ppx_ocr,
        ppx_formula=args.ppx_formula,
        **_profile_settings_kwargs(args.profile),
    )


def _profile_settings_kwargs(profile_id: str | None) -> dict[str, str]:
    return {"profile_id": get_profile(profile_id).id}


def _make_settings(**kwargs: object) -> LlmCheckSettings:
    field_names = {field.name for field in dataclasses.fields(LlmCheckSettings)}
    profile_id = str(kwargs.pop("profile_id"))
    if "profile_id" in field_names:
        kwargs["profile_id"] = profile_id
        return LlmCheckSettings(**kwargs)  # type: ignore[arg-type]
    settings = LlmCheckSettings(**kwargs)  # type: ignore[arg-type]
    object.__setattr__(settings, "profile_id", profile_id)
    return settings


def _print_progress_event(event: dict[str, object]) -> None:
    print(json.dumps({"type": "progress", **event}, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)


def _mineru_api_key(args: argparse.Namespace) -> str:
    return str(
        args.mineru_api_key
        or os.environ.get("MINERU_CLOUD_API_TOKEN")
        or os.environ.get("MINERU_API_KEY")
        or os.environ.get("LLMCHECK_MINERU_API_KEY")
        or _dotenv_value("MINERU_CLOUD_API_TOKEN")
        or _dotenv_value("MINERU_API_KEY")
        or _dotenv_value("LLMCHECK_MINERU_API_KEY")
        or ""
    )


def _dotenv_value(name: str) -> str:
    dotenv_paths = [Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env"]
    seen: set[Path] = set()
    for dotenv_path in dotenv_paths:
        if dotenv_path in seen:
            continue
        seen.add(dotenv_path)
        try:
            lines = dotenv_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() != name:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            return value
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
