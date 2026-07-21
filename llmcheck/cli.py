from __future__ import annotations

from pathlib import Path
import argparse
import dataclasses
import json
import os
import sys

from llmcheck.batch import run_batch
from llmcheck.diagnostics import cost_summary_report, inspect_output, quality_report, review_report, summarize_book_output
from llmcheck.gui import create_app
from llmcheck.model_compare import run_model_compare
from llmcheck.preflight import preflight_llm_models
from llmcheck.run_guard import RunAlreadyActiveError
from llmcheck.pipeline import (
    DEFAULT_LLM_CHUNK_CHARS,
    DEFAULT_LLM_CONCURRENCY,
    DEFAULT_LLM_MAX_CALLS_PER_BOOK,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_MODEL_API_URLS,
    MIN_LLM_CHUNK_CHARS,
    LlmCheckError,
    LlmCheckSettings,
    model_api_url_from_overrides,
    process_documents,
)
from llmcheck.preprocess import (
    DEFAULT_MINERU_BATCH_SIZE,
    DEFAULT_MINERU_MAX_RETRIES,
    DEFAULT_MINERU_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_MINERU_RETRY_BACKOFF_SECONDS,
    DEFAULT_PDF_PAGE_CHUNK_SIZE,
)

LLM_PROCESSING_MODES = ("review-first", "legacy-correction", "local-gate")

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
    run.add_argument("--llm-api-url", default="")
    run.add_argument("--llm-api-key", default="")
    run.add_argument("--llm-model", default=DEFAULT_LLM_MODEL)
    run.add_argument("--llm-mode", choices=LLM_PROCESSING_MODES, default="review-first")
    run.add_argument("--concurrency", type=int, default=DEFAULT_LLM_CONCURRENCY)
    run.add_argument("--review-concurrency", type=int, default=0)
    run.add_argument("--patch-concurrency", type=int, default=0)
    run.add_argument("--llm-chunk-chars", type=int, default=DEFAULT_LLM_CHUNK_CHARS)
    run.add_argument("--acceptance-repair-rounds", type=int, default=1)
    run.add_argument("--timeout-seconds", type=int, default=600)
    run.add_argument("--llm-call-timeout-seconds", type=int, default=0)
    run.add_argument("--llm-stage-timeout-seconds", type=int, default=0)
    run.add_argument("--llm-idle-timeout-seconds", type=int, default=0)
    run.add_argument("--llm-max-calls-per-book", type=int, default=DEFAULT_LLM_MAX_CALLS_PER_BOOK)
    run.add_argument("--llm-max-cost-per-book", type=float, default=0.0)
    run.add_argument("--allow-fallback-models", action="store_true")
    run.add_argument("--fallback-models", default="")
    run.add_argument("--llm-model-api-urls", default="")
    run.add_argument("--mineru-api-url", default="https://mineru.net")
    run.add_argument("--mineru-api-key", default="")
    run.add_argument("--mineru-concurrency", type=int, default=12)
    run.add_argument("--mineru-batch-size", type=int, default=DEFAULT_MINERU_BATCH_SIZE)
    run.add_argument("--mineru-timeout-seconds", type=int, default=3600)
    run.add_argument("--mineru-request-timeout-seconds", type=int, default=DEFAULT_MINERU_REQUEST_TIMEOUT_SECONDS)
    run.add_argument("--mineru-max-retries", type=int, default=DEFAULT_MINERU_MAX_RETRIES)
    run.add_argument("--mineru-retry-backoff-seconds", type=float, default=DEFAULT_MINERU_RETRY_BACKOFF_SECONDS)
    run.add_argument("--pdf-page-chunk-size", type=int, default=DEFAULT_PDF_PAGE_CHUNK_SIZE)
    run.add_argument("--enable-ppx", action="store_true", help="Opt-in local PPX audit/fallback (default off; can freeze the machine).")
    run.add_argument("--mineru-fallback", choices=("ppx", "none"), default="none", help="Only used with --enable-ppx. Default none.")
    run.add_argument("--ppx-command", default="/mnt/d/codex/memect-ppx/ppx")
    run.add_argument("--ppx-cwd", default="/mnt/d/codex/memect-ppx")
    run.add_argument("--ppx-timeout-seconds", type=int, default=3600)
    run.add_argument("--ppx-backend", default="default")
    run.add_argument("--ppx-ocr", default="auto")
    run.add_argument("--ppx-formula", default="no")
    run.add_argument("--profile", default=DEFAULT_PROFILE_ID, choices=profile_choices)

    batch = subparsers.add_parser("batch", help="Run LLMcheck one book at a time for a source directory.")
    batch.add_argument("--source-dir", "--input", dest="source_dir", required=True, type=Path)
    batch.add_argument("--output-dir", type=Path)
    batch.add_argument("--output-md-dir", type=Path)
    batch.add_argument("--process-dir", type=Path)
    batch.add_argument("--llm-api-url", default="")
    batch.add_argument("--llm-api-key", default="")
    batch.add_argument("--llm-model", default=DEFAULT_LLM_MODEL)
    batch.add_argument("--llm-mode", choices=LLM_PROCESSING_MODES, default="review-first")
    batch.add_argument("--concurrency", type=int, default=DEFAULT_LLM_CONCURRENCY, help="LLM per-book concurrency.")
    batch.add_argument("--review-concurrency", type=int, default=0)
    batch.add_argument("--patch-concurrency", type=int, default=0)
    batch.add_argument("--llm-chunk-chars", type=int, default=DEFAULT_LLM_CHUNK_CHARS)
    batch.add_argument("--acceptance-repair-rounds", type=int, default=1)
    batch.add_argument("--book-concurrency", type=int, default=1, help="Book-level supervisor concurrency. Use 1 for one-by-one.")
    batch.add_argument("--start-index", type=int, default=1)
    batch.add_argument("--limit", type=int, default=0)
    batch.add_argument("--dry-run", action="store_true")
    batch.add_argument("--preflight-only", action="store_true")
    batch.add_argument("--skip-existing", action="store_true", default=True)
    batch.add_argument("--no-skip-existing", dest="skip_existing", action="store_false")
    batch.add_argument("--enable-ppx", action="store_true", help="Opt-in local PPX audit/fallback (default off).")
    batch.add_argument("--mineru-fallback", choices=("ppx", "none"), default="none", help="Only used with --enable-ppx. Default none.")
    batch.add_argument("--max-cleanup-loops", type=int, default=3)
    batch.add_argument("--stop-on-global-service-error", action="store_true", default=True)
    batch.add_argument("--no-stop-on-global-service-error", dest="stop_on_global_service_error", action="store_false")
    batch.add_argument("--timeout-seconds", type=int, default=600)
    batch.add_argument("--llm-call-timeout-seconds", type=int, default=0)
    batch.add_argument("--llm-stage-timeout-seconds", type=int, default=0)
    batch.add_argument("--llm-idle-timeout-seconds", type=int, default=0)
    batch.add_argument("--llm-max-calls-per-book", type=int, default=DEFAULT_LLM_MAX_CALLS_PER_BOOK)
    batch.add_argument("--llm-max-cost-per-book", type=float, default=0.0)
    batch.add_argument("--allow-fallback-models", action="store_true")
    batch.add_argument("--fallback-models", default="")
    batch.add_argument("--llm-model-api-urls", default="")
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

    model_compare = subparsers.add_parser("model-compare", help="Compare reviewer models on the same first N books.")
    model_compare.add_argument("--source-dir", required=True, type=Path)
    model_compare.add_argument("--output-dir", required=True, type=Path)
    model_compare.add_argument("--models", default="mimo-v2.5-pro,gpt-5.5")
    model_compare.add_argument("--preferred-model", default="mimo-v2.5-pro")
    model_compare.add_argument("--fallback-model", default="gpt-5.5")
    model_compare.add_argument("--llm-api-url", default="")
    model_compare.add_argument("--llm-api-key", default="")
    model_compare.add_argument("--llm-model", default="mimo-v2.5-pro")
    model_compare.add_argument("--llm-mode", choices=("review-first", "legacy-correction"), default="review-first")
    model_compare.add_argument("--concurrency", type=int, default=DEFAULT_LLM_CONCURRENCY)
    model_compare.add_argument("--review-concurrency", type=int, default=0)
    model_compare.add_argument("--patch-concurrency", type=int, default=0)
    model_compare.add_argument("--llm-chunk-chars", type=int, default=DEFAULT_LLM_CHUNK_CHARS)
    model_compare.add_argument("--acceptance-repair-rounds", type=int, default=1)
    model_compare.add_argument("--book-concurrency", type=int, default=1)
    model_compare.add_argument("--start-index", type=int, default=1)
    model_compare.add_argument("--limit", type=int, default=3)
    model_compare.add_argument("--timeout-seconds", type=int, default=600)
    model_compare.add_argument("--llm-call-timeout-seconds", type=int, default=0)
    model_compare.add_argument("--llm-stage-timeout-seconds", type=int, default=0)
    model_compare.add_argument("--llm-idle-timeout-seconds", type=int, default=0)
    model_compare.add_argument("--llm-max-calls-per-book", type=int, default=DEFAULT_LLM_MAX_CALLS_PER_BOOK)
    model_compare.add_argument("--llm-max-cost-per-book", type=float, default=0.0)
    model_compare.add_argument("--allow-fallback-models", action="store_true")
    model_compare.add_argument("--fallback-models", default="")
    model_compare.add_argument("--llm-model-api-urls", default="")
    model_compare.add_argument("--mineru-api-url", default="https://mineru.net")
    model_compare.add_argument("--mineru-api-key", default="")
    model_compare.add_argument("--mineru-concurrency", type=int, default=12)
    model_compare.add_argument("--mineru-batch-size", type=int, default=DEFAULT_MINERU_BATCH_SIZE)
    model_compare.add_argument("--mineru-timeout-seconds", type=int, default=3600)
    model_compare.add_argument("--mineru-request-timeout-seconds", type=int, default=DEFAULT_MINERU_REQUEST_TIMEOUT_SECONDS)
    model_compare.add_argument("--mineru-max-retries", type=int, default=DEFAULT_MINERU_MAX_RETRIES)
    model_compare.add_argument("--mineru-retry-backoff-seconds", type=float, default=DEFAULT_MINERU_RETRY_BACKOFF_SECONDS)
    model_compare.add_argument("--pdf-page-chunk-size", type=int, default=DEFAULT_PDF_PAGE_CHUNK_SIZE)
    model_compare.add_argument("--enable-ppx", action="store_true", help="Opt-in local PPX audit/fallback (default off).")
    model_compare.add_argument("--mineru-fallback", choices=("ppx", "none"), default="none", help="Only used with --enable-ppx. Default none.")
    model_compare.add_argument("--ppx-command", default="/mnt/d/codex/memect-ppx/ppx")
    model_compare.add_argument("--ppx-cwd", default="/mnt/d/codex/memect-ppx")
    model_compare.add_argument("--ppx-timeout-seconds", type=int, default=3600)
    model_compare.add_argument("--ppx-backend", default="default")
    model_compare.add_argument("--ppx-ocr", default="auto")
    model_compare.add_argument("--ppx-formula", default="no")
    model_compare.add_argument("--profile", default=DEFAULT_PROFILE_ID, choices=profile_choices)
    model_compare.add_argument("--force", action="store_true")

    gui = subparsers.add_parser("gui", help="Start the LLMcheck GUI server.")
    gui.add_argument("--host", default="127.0.0.1")
    gui.add_argument("--port", type=int, default=8766)

    mineru_status = subparsers.add_parser("mineru-status", help="Summarize MinerU segment progress for one book output directory.")
    mineru_status.add_argument("--book-output-dir", required=True, type=Path)
    mineru_status.add_argument("--book-name", default="")
    inspect = subparsers.add_parser("inspect", help="Inspect one book output directory: status, heartbeat, calls, artifacts.")
    inspect.add_argument("--book-output-dir", required=True, type=Path)
    inspect.add_argument("--book-name", default="")
    review = subparsers.add_parser("review-report", help="Summarize LLM review issues and safe patch decisions for one book output.")
    review.add_argument("--book-output-dir", required=True, type=Path)
    review.add_argument("--book-name", default="")
    cost = subparsers.add_parser("cost-summary", help="Summarize LLM call counts, stages, models, errors and estimated cost for one book output.")
    cost.add_argument("--book-output-dir", required=True, type=Path)
    cost.add_argument("--book-name", default="")
    quality = subparsers.add_parser("quality-report", help="Summarize deterministic cleaning, quality and final gate reports for one book output.")
    quality.add_argument("--book-output-dir", required=True, type=Path)
    quality.add_argument("--book-name", default="")
    subparsers.add_parser("profiles", help="List built-in document profiles.")

    agent = subparsers.add_parser("agent", help="Agent-callable JSON surface for convert/status/get-md/profiles.")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_convert = agent_sub.add_parser("convert", help="Submit a sync convert job and print JobReport JSON.")
    agent_convert.add_argument("--input", required=True, type=Path, help="Supported file or directory: md/pdf/image/Office.")
    agent_convert.add_argument("--output-dir", required=True, type=Path)
    agent_convert.add_argument("--llm-api-url", default="")
    agent_convert.add_argument("--llm-api-key", default="")
    agent_convert.add_argument("--llm-model", default=DEFAULT_LLM_MODEL)
    agent_convert.add_argument("--llm-mode", choices=LLM_PROCESSING_MODES, default="local-gate")
    agent_convert.add_argument("--concurrency", type=int, default=DEFAULT_LLM_CONCURRENCY)
    agent_convert.add_argument("--profile", default=DEFAULT_PROFILE_ID, choices=profile_choices)
    agent_convert.add_argument("--mineru-api-key", default="")
    agent_convert.add_argument("--enable-ppx", action="store_true", help="Opt-in local PPX (default off; only when the task explicitly requires PPX).")
    agent_convert.add_argument("--mineru-fallback", choices=("ppx", "none"), default="none", help="Only used with --enable-ppx.")
    agent_convert.add_argument("--pdf-page-chunk-size", type=int, default=50, help="PDF split page size for MinerU (<=50 recommended)")
    agent_convert.add_argument("--mineru-concurrency", type=int, default=2, help="Parallel MinerU segment uploads")
    agent_convert.add_argument("--mineru-batch-size", type=int, default=8, help="MinerU files per batch request")
    agent_status = agent_sub.add_parser("status", help="Read JobReport for an output directory.")
    agent_status.add_argument("--output-dir", required=True, type=Path)
    agent_get_md = agent_sub.add_parser("get-md", help="Return final Markdown only when document status is passed.")
    agent_get_md.add_argument("--output-dir", required=True, type=Path)
    agent_get_md.add_argument("--document-id", default="")
    agent_get_md.add_argument("--max-chars", type=int, default=None)
    agent_sub.add_parser("profiles", help="List built-in profiles as agent JSON.")

    args = parser.parse_args(argv)
    if args.command == "profiles":
        print(json.dumps({"default_profile_id": DEFAULT_PROFILE_ID, "profiles": list_profiles()}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "agent":
        return _run_agent_command(args)
    if args.command == "mineru-status":
        print(json.dumps(summarize_book_output(args.book_output_dir, book_name=args.book_name), ensure_ascii=False, indent=2))
        return 0
    if args.command == "inspect":
        print(json.dumps(inspect_output(args.book_output_dir, book_name=args.book_name), ensure_ascii=False, indent=2))
        return 0
    if args.command == "review-report":
        print(json.dumps(review_report(args.book_output_dir, book_name=args.book_name), ensure_ascii=False, indent=2))
        return 0
    if args.command == "cost-summary":
        print(json.dumps(cost_summary_report(args.book_output_dir, book_name=args.book_name), ensure_ascii=False, indent=2))
        return 0
    if args.command == "quality-report":
        print(json.dumps(quality_report(args.book_output_dir, book_name=args.book_name), ensure_ascii=False, indent=2))
        return 0
    if args.command == "run":
        try:
            report = process_documents(
                input_path=args.input,
                output_dir=args.output_dir,
                settings=_settings_from_args(args),
            )
        except (LlmCheckError, RunAlreadyActiveError) as error:
            print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] in {"passed", "dry_run", "preflight_only"} else 1
    if args.command == "batch":
        try:
            settings = _settings_from_args(args)
            report = run_batch(
                source_dir=args.source_dir,
                output_dir=_batch_output_dir(args),
                final_md_dir=args.output_md_dir,
                process_dir=args.process_dir,
                settings=settings,
                book_concurrency=max(1, args.book_concurrency),
                start_index=max(1, args.start_index),
                limit=max(0, args.limit),
                force=args.force,
                skip_existing=bool(args.skip_existing),
                dry_run=bool(args.dry_run),
                preflight_only=bool(args.preflight_only),
                llm_preflight=None if settings.llm_mode == "local-gate" else preflight_llm_models,
                stop_on_global_service_error=bool(args.stop_on_global_service_error),
                progress_callback=_print_progress_event,
            )
        except (LlmCheckError, RunAlreadyActiveError) as error:
            print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] in {"passed", "dry_run", "preflight_only"} else 1
    if args.command == "model-compare":
        try:
            report = run_model_compare(
                source_dir=args.source_dir,
                output_dir=args.output_dir,
                settings=_settings_from_args(args),
                models=_parse_model_list(args.models),
                preferred_model=args.preferred_model,
                fallback_model=args.fallback_model,
                book_concurrency=max(1, args.book_concurrency),
                start_index=max(1, args.start_index),
                limit=max(1, args.limit),
                force=args.force,
            )
        except (LlmCheckError, RunAlreadyActiveError) as error:
            print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] in {"passed", "dry_run", "preflight_only"} else 1
    if args.command == "gui":
        import uvicorn

        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


def _run_agent_command(args: argparse.Namespace) -> int:
    import llmcheck.agent_api as agent_api

    try:
        if args.agent_command == "profiles":
            print(json.dumps(agent_api.list_profiles(), ensure_ascii=False, indent=2))
            return 0
        if args.agent_command == "status":
            report = agent_api.get_job(output_dir=args.output_dir)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("status") == "passed" else 1
        if args.agent_command == "get-md":
            payload = agent_api.get_final_markdown(
                output_dir=args.output_dir,
                document_id=args.document_id or None,
                max_chars=args.max_chars,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0 if payload.get("status") == "passed" else 1
        if args.agent_command == "convert":
            enable_ppx = bool(getattr(args, "enable_ppx", False))
            report = agent_api.submit_convert(
                input_path=args.input,
                output_dir=args.output_dir,
                llm_api_url=args.llm_api_url or None,
                llm_api_key=args.llm_api_key or None,
                llm_model=args.llm_model,
                llm_mode=args.llm_mode,
                profile_id=args.profile,
                mineru_api_key=args.mineru_api_key or None,
                concurrency=args.concurrency,
                enable_ppx=enable_ppx,
                mineru_fallback=(getattr(args, "mineru_fallback", "none") if enable_ppx else "none"),
                pdf_page_chunk_size=int(getattr(args, "pdf_page_chunk_size", 50) or 50),
                mineru_concurrency=int(getattr(args, "mineru_concurrency", 2) or 2),
                mineru_batch_size=int(getattr(args, "mineru_batch_size", 8) or 8),
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("status") == "passed" else 1
    except LlmCheckError as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False, indent=2))
        message = str(error)
        if any(token in message for token in ("缺少", "不存在", "未知文档 profile", "需要", "invalid", "配置")):
            return 2
        return 1
    except (ValueError, FileNotFoundError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False, indent=2))
        return 2
    raise AssertionError(f"unhandled agent command: {args.agent_command}")


def _settings_from_args(args: argparse.Namespace) -> LlmCheckSettings:
    llm_mode = str(getattr(args, "llm_mode", ""))
    llm_required = not (
        llm_mode == "local-gate"
        or (
            getattr(args, "command", "") == "batch"
            and bool(getattr(args, "dry_run", False))
            and not bool(getattr(args, "preflight_only", False))
        )
    )
    return _make_settings(
        llm_api_url=_llm_api_url(args, required=llm_required),
        llm_api_key=_llm_api_key(args, required=llm_required),
        llm_model=args.llm_model,
        llm_mode=args.llm_mode,
        concurrency=max(1, args.concurrency),
        review_concurrency=max(0, args.review_concurrency),
        patch_concurrency=max(0, args.patch_concurrency),
        llm_chunk_chars=max(MIN_LLM_CHUNK_CHARS, args.llm_chunk_chars),
        acceptance_repair_rounds=max(0, args.acceptance_repair_rounds),
        timeout_seconds=max(10, args.timeout_seconds),
        llm_call_timeout_seconds=max(0, args.llm_call_timeout_seconds),
        llm_stage_timeout_seconds=max(0, args.llm_stage_timeout_seconds),
        llm_idle_timeout_seconds=max(0, args.llm_idle_timeout_seconds),
        llm_max_calls_per_book=max(0, args.llm_max_calls_per_book),
        llm_max_cost_per_book=max(0.0, args.llm_max_cost_per_book),
        allow_fallback_models=bool(args.allow_fallback_models),
        fallback_models=args.fallback_models,
        llm_model_api_urls=_llm_model_api_urls(args),
        mineru_api_url=args.mineru_api_url,
        mineru_api_key=_mineru_api_key(args),
        mineru_model="vlm",
        mineru_concurrency=max(1, args.mineru_concurrency),
        mineru_batch_size=max(1, args.mineru_batch_size),
        mineru_timeout_seconds=max(10, args.mineru_timeout_seconds),
        mineru_request_timeout_seconds=max(10, args.mineru_request_timeout_seconds),
        mineru_max_retries=max(1, args.mineru_max_retries),
        mineru_retry_backoff_seconds=max(0.0, args.mineru_retry_backoff_seconds),
        enable_ppx=bool(getattr(args, "enable_ppx", False)),
        mineru_fallback=(
            str(getattr(args, "mineru_fallback", "none") or "none")
            if bool(getattr(args, "enable_ppx", False))
            else "none"
        ),
        pdf_page_chunk_size=max(1, args.pdf_page_chunk_size),
        ppx_command=args.ppx_command,
        ppx_cwd=args.ppx_cwd,
        ppx_timeout_seconds=max(10, args.ppx_timeout_seconds),
        ppx_backend=args.ppx_backend,
        ppx_ocr=args.ppx_ocr,
        ppx_formula=args.ppx_formula,
        max_cleanup_loops=max(1, int(getattr(args, "max_cleanup_loops", 3))),
        **_profile_settings_kwargs(args.profile),
    )


def _batch_output_dir(args: argparse.Namespace) -> Path:
    if args.output_dir:
        return args.output_dir
    if getattr(args, "process_dir", None):
        return args.process_dir.parent
    if getattr(args, "output_md_dir", None):
        return args.output_md_dir.parent
    raise LlmCheckError("batch 需要 --output-dir，或同时使用显式 --process-dir/--output-md-dir 工作流")


def _parse_model_list(value: str) -> list[str]:
    models: list[str] = []
    seen: set[str] = set()
    for item in value.split(","):
        model = item.strip()
        if not model or model in seen:
            continue
        seen.add(model)
        models.append(model)
    if not models:
        raise LlmCheckError("至少需要提供一个模型用于对照")
    return models


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

def _llm_api_url(args: argparse.Namespace, *, required: bool = True) -> str:
    value = str(
        args.llm_api_url
        or os.environ.get("LLMCHECK_LLM_API_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or _dotenv_value("LLMCHECK_LLM_API_URL")
        or _dotenv_value("OPENAI_BASE_URL")
        or ""
    )
    if not value:
        value = model_api_url_from_overrides(_llm_model_api_urls(args), str(args.llm_model))
    if not value and required:
        raise LlmCheckError("缺少 LLM API URL：请传 --llm-api-url 或设置 LLMCHECK_LLM_API_URL/OPENAI_BASE_URL")
    return value


def _llm_model_api_urls(args: argparse.Namespace) -> str:
    return str(
        getattr(args, "llm_model_api_urls", "")
        or os.environ.get("LLMCHECK_LLM_MODEL_API_URLS")
        or _dotenv_value("LLMCHECK_LLM_MODEL_API_URLS")
        or DEFAULT_LLM_MODEL_API_URLS
    )


def _llm_api_key(args: argparse.Namespace, *, required: bool = True) -> str:
    value = str(
        args.llm_api_key
        or os.environ.get("LLMCHECK_LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or _dotenv_value("LLMCHECK_LLM_API_KEY")
        or _dotenv_value("OPENAI_API_KEY")
        or ""
    )
    if not value and required:
        raise LlmCheckError("缺少 LLM API key：请传 --llm-api-key 或设置 LLMCHECK_LLM_API_KEY/OPENAI_API_KEY")
    return value


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
