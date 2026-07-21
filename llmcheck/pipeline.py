from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
import hashlib
import json
import re
import time
import uuid

from llmcheck.llm import LlmClient, LlmConfig, acceptance_result_payload, correction_result_payload, repair_result_payload, review_result_payload
from llmcheck.pdf import write_text_pdf
from llmcheck.cleaning import clean_markdown_with_report, clean_markdown_text, repair_acceptance_locally, safe_stem
from llmcheck.final_gate import final_acceptance_report, quality_errors, quality_hints
from llmcheck.preprocess import (
    DEFAULT_MINERU_BATCH_SIZE,
    DEFAULT_MINERU_MAX_RETRIES,
    DEFAULT_MINERU_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_MINERU_RETRY_BACKOFF_SECONDS,
    DEFAULT_PDF_PAGE_CHUNK_SIZE,
    PreprocessSettings,
    prepare_markdown_inputs,
)
from llmcheck.profiles import DEFAULT_PROFILE_ID, DocumentProfile, get_profile
from llmcheck.run_guard import RunAlreadyActiveError, acquire_run_lock
from llmcheck.structure import finalize_standard_document


class LlmCheckError(RuntimeError):
    pass


DEFAULT_LLM_CHUNK_CHARS = 2_000
MIN_LLM_CHUNK_CHARS = 1_000
DEFAULT_LLM_CONCURRENCY = 10
DEFAULT_LLM_MODEL = "deepseek-v4-pro"
DEFAULT_LLM_MAX_CALLS_PER_BOOK = 300
DEFAULT_LLM_ESTIMATED_COST_PER_1K_CHARS = 0.0
DEFAULT_LLM_MODEL_API_URLS = "mimo-v2.5-pro=https://api.iaigc.fun/v1,gpt-5.5=http://127.0.0.1:3022"
PROCESS_DIR_NAME = "process"
FINAL_MARKDOWN_DIR_NAME = "md"
TEXT_PDF_DIR_NAME = "pdf"
FINALIZATION_SAFE_ACCEPTANCE_CATEGORIES = {"layout", "ocr_noise", "punctuation"}

# Module-level in-memory cache: keyed by file path string, holds parsed JSON.
_mem_cache: dict[str, Any] = {}


@dataclass(frozen=True)
class LlmCheckSettings:
    llm_api_url: str
    llm_api_key: str
    llm_model: str = DEFAULT_LLM_MODEL
    llm_mode: str = "review-first"
    profile_id: str = DEFAULT_PROFILE_ID
    concurrency: int = DEFAULT_LLM_CONCURRENCY
    review_concurrency: int = 0
    patch_concurrency: int = 0
    timeout_seconds: int = 600
    llm_call_timeout_seconds: int = 0
    llm_stage_timeout_seconds: int = 0
    llm_idle_timeout_seconds: int = 0
    llm_max_calls_per_book: int = DEFAULT_LLM_MAX_CALLS_PER_BOOK
    llm_max_cost_per_book: float = 0.0
    allow_fallback_models: bool = False
    fallback_models: str = ""
    llm_model_api_urls: str = DEFAULT_LLM_MODEL_API_URLS
    llm_estimated_cost_per_1k_chars: float = DEFAULT_LLM_ESTIMATED_COST_PER_1K_CHARS
    llm_chunk_chars: int = DEFAULT_LLM_CHUNK_CHARS
    acceptance_repair_rounds: int = 1
    mineru_api_url: str = "https://mineru.net"
    mineru_api_key: str = ""
    mineru_model: str = "vlm"
    mineru_concurrency: int = 2
    mineru_batch_size: int = DEFAULT_MINERU_BATCH_SIZE
    mineru_timeout_seconds: int = 7200
    mineru_request_timeout_seconds: int = DEFAULT_MINERU_REQUEST_TIMEOUT_SECONDS
    mineru_max_retries: int = DEFAULT_MINERU_MAX_RETRIES
    mineru_retry_backoff_seconds: float = DEFAULT_MINERU_RETRY_BACKOFF_SECONDS
    # PPX is opt-in only. Default off to avoid local resource exhaustion.
    enable_ppx: bool = False
    mineru_fallback: str = "none"
    pdf_page_chunk_size: int = DEFAULT_PDF_PAGE_CHUNK_SIZE
    ppx_command: str = "/mnt/d/codex/memect-ppx/ppx"
    ppx_cwd: str = "/mnt/d/codex/memect-ppx"
    ppx_timeout_seconds: int = 3600
    ppx_backend: str = "default"
    ppx_ocr: str = "auto"
    ppx_formula: str = "no"
    max_cleanup_loops: int = 3


@dataclass(frozen=True)
class DocumentResult:
    document_id: str
    source_path: str
    status: str
    final_markdown_path: str = ""
    text_pdf_path: str = ""
    draft_path: str = ""
    correction_report_path: str = ""
    acceptance_report_path: str = ""
    review_report_path: str = ""
    repair_report_path: str = ""
    llm_calls_report_path: str = ""
    artifact_binding_report_path: str = ""
    artifact_binding_status: str = ""
    final_markdown_sha256: str = ""
    pdf_source_sha256: str = ""
    pdf_sha256: str = ""
    profile_id: str = ""
    finalization_report_path: str = ""
    final_acceptance_report_path: str = ""
    cross_report_path: str = ""
    char_count: int = 0
    sha256: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TextChunk:
    index: int
    total: int
    text: str


def process_documents(
    *,
    input_path: Path,
    output_dir: Path,
    settings: LlmCheckSettings,
    client: Any | None = None,
    preprocess_runner: Any | None = None,
) -> dict[str, Any]:
    run_started_at = datetime.now(timezone.utc)
    run_started = time.monotonic()
    run_id = uuid.uuid4().hex
    profile = get_profile(settings.profile_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    process_dir = output_dir / PROCESS_DIR_NAME
    run_lock = acquire_run_lock(process_dir, run_id=run_id)
    try:
        run_lock.heartbeat(status="running", stage="preprocess")
        docs = (
            preprocess_runner(input_path=input_path, output_dir=process_dir, settings=settings)
            if preprocess_runner is not None
            else prepare_markdown_inputs(input_path=input_path, output_dir=process_dir, settings=_preprocess_settings(settings))
        )
        if not docs:
            raise LlmCheckError(f"未发现可处理输入：{input_path}")
        run_lock.heartbeat(status="running", stage="llm")
        actual_client = client or _build_llm_client(settings)
        results: list[DocumentResult | None] = [None] * len(docs)
        worker_count = max(1, settings.concurrency)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    process_one_document,
                    path=path,
                    output_dir=output_dir,
                    settings=settings,
                    client=actual_client,
                    document_title=input_path.stem if input_path.is_file() else None,
                ): index
                for index, path in enumerate(docs)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        ordered = [result for result in results if result is not None]
        run_lock.heartbeat(status="running", stage="finalize")
        report = {
            "schema_version": "1.0",
            "job_id": run_id,
            "status": "passed" if all(result.status == "passed" for result in ordered) else "review_required",
            "input_path": str(input_path.resolve()),
            "output_dir": str(output_dir.resolve()),
            "document_count": len(ordered),
            "passed_count": sum(1 for result in ordered if result.status == "passed"),
            "failed_count": sum(1 for result in ordered if result.status != "passed"),
            "model": settings.llm_model,
            "profile_id": profile.id,
            "concurrency": worker_count,
            "review_concurrency": _review_concurrency(settings),
            "patch_concurrency": _patch_concurrency(settings),
            "llm_call_count": int(getattr(actual_client, "call_count", 0) or 0),
            "llm_max_calls_per_book": max(0, settings.llm_max_calls_per_book),
            "allow_fallback_models": settings.allow_fallback_models,
            "fallback_models": settings.fallback_models,
            "documents": [result.to_dict() for result in ordered],
            "artifacts": {
                "md_dir": str((output_dir / FINAL_MARKDOWN_DIR_NAME).resolve()),
                "process_dir": str(process_dir.resolve()),
                "pdf_dir": str((output_dir / TEXT_PDF_DIR_NAME).resolve()),
            },
        }
        reports_dir = process_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / "llmcheck_summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (reports_dir / "llmcheck_manifest.jsonl").write_text(
            "\n".join(json.dumps(result.to_dict(), ensure_ascii=False) for result in ordered) + "\n",
            encoding="utf-8",
        )
        _write_run_observability_reports(
            process_dir=process_dir,
            report=report,
            settings=settings,
            started_at=run_started_at,
            duration_seconds=time.monotonic() - run_started,
            results=ordered,
            actual_client=actual_client,
        )
        run_lock.release(status=str(report.get("status") or "finished"))
        return report
    except Exception:
        run_lock.release(status="failed")
        raise

def _write_run_observability_reports(
    *,
    process_dir: Path,
    report: dict[str, Any],
    settings: LlmCheckSettings,
    started_at: datetime,
    duration_seconds: float,
    results: list[DocumentResult],
    actual_client: Any,
) -> None:
    finished_at = datetime.now(timezone.utc)
    call_rows = _collect_llm_call_rows(results)
    stage_counts: dict[str, int] = {}
    stage_durations: dict[str, float] = {}
    for row in call_rows:
        stage = str(row.get("stage") or "llm_unknown")
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        try:
            stage_durations[stage] = stage_durations.get(stage, 0.0) + float(row.get("duration_seconds") or 0.0)
        except (TypeError, ValueError):
            stage_durations[stage] = stage_durations.get(stage, 0.0)
    call_count = len(call_rows) or int(getattr(actual_client, "call_count", 0) or 0)
    run_event = {
        "event": "run_finished",
        "status": report.get("status"),
        "started_at": _isoformat_utc(started_at),
        "finished_at": _isoformat_utc(finished_at),
        "duration_seconds": round(duration_seconds, 3),
        "document_count": len(results),
        "passed_count": report.get("passed_count"),
        "failed_count": report.get("failed_count"),
        "model": settings.llm_model,
        "llm_call_count": call_count,
    }
    _append_jsonl(process_dir / "run_events.jsonl", run_event)
    _write_json(
        process_dir / "heartbeat.json",
        {
            "status": report.get("status"),
            "stage": "finished",
            "updated_at": _isoformat_utc(finished_at),
            "duration_seconds": round(duration_seconds, 3),
            "document_count": len(results),
            "llm_call_count": call_count,
        },
    )
    _write_json(
        process_dir / "stage_timings.json",
        {
            "started_at": _isoformat_utc(started_at),
            "finished_at": _isoformat_utc(finished_at),
            "total_duration_seconds": round(duration_seconds, 3),
            "stages": {
                "run": {"duration_seconds": round(duration_seconds, 3)},
                **{
                    stage: {
                        "call_count": stage_counts[stage],
                        "duration_seconds": round(stage_durations.get(stage, 0.0), 3),
                    }
                    for stage in sorted(stage_counts)
                },
            },
        },
    )
    _write_json(
        process_dir / "cost_report.json",
        {
            "model": settings.llm_model,
            "fallback_allowed": settings.allow_fallback_models,
            "fallback_models": _fallback_model_list(settings.fallback_models),
            "llm_call_count": call_count,
            "llm_max_calls_per_book": max(0, settings.llm_max_calls_per_book),
            "llm_max_cost_per_book": max(0.0, settings.llm_max_cost_per_book),
            "estimated_cost": _sum_estimated_cost(call_rows),
            "currency": "unknown",
            "pricing_available": False,
        },
    )
    calls_path = process_dir / "llm_calls.jsonl"
    calls_path.write_text(
        ("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in call_rows) + "\n") if call_rows else "",
        encoding="utf-8",
    )


def _collect_llm_call_rows(results: list[DocumentResult]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in results:
        if not result.llm_calls_report_path:
            continue
        path = Path(result.llm_calls_report_path)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _sum_estimated_cost(rows: list[dict[str, Any]]) -> float | None:
    total = 0.0
    seen = False
    for row in rows:
        value = row.get("estimated_cost")
        if value is None:
            continue
        try:
            total += float(value)
        except (TypeError, ValueError):
            continue
        seen = True
    return round(total, 6) if seen else None


def _fallback_model_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def model_api_url_overrides(value: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    for item in value.split(","):
        entry = item.strip()
        if not entry or "=" not in entry:
            continue
        model, api_url = entry.split("=", 1)
        model = model.strip()
        api_url = api_url.strip()
        if model and api_url:
            rows[model] = api_url
    return rows


def model_api_url_from_overrides(value: str, model: str) -> str:
    return model_api_url_overrides(value).get(model.strip(), "")


def model_api_url_for_model(settings: LlmCheckSettings, model: str) -> str:
    return model_api_url_from_overrides(settings.llm_model_api_urls, model) or settings.llm_api_url


def _llm_config(settings: LlmCheckSettings) -> LlmConfig:
    return LlmConfig(
        api_url=model_api_url_for_model(settings, settings.llm_model),
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout_seconds=max(10, settings.llm_call_timeout_seconds or settings.timeout_seconds),
        max_calls_per_book=max(0, settings.llm_max_calls_per_book),
    )


def _build_llm_client(settings: LlmCheckSettings) -> Any:
    config = _llm_config(settings)
    fallback_models = _fallback_model_list(settings.fallback_models)
    if settings.allow_fallback_models or fallback_models:
        return FallbackLlmClient(
            config,
            fallback_models=fallback_models,
            model_api_urls=model_api_url_overrides(settings.llm_model_api_urls),
        )
    return LlmClient(config)


class FallbackLlmClient:
    def __init__(self, primary_config: LlmConfig, *, fallback_models: list[str], model_api_urls: dict[str, str] | None = None) -> None:
        self.primary_config = primary_config
        self.models = _dedupe_models([primary_config.model, *fallback_models])
        api_urls = model_api_urls or {}
        self._clients = {
            model: LlmClient(replace(primary_config, model=model, api_url=api_urls.get(model, primary_config.api_url)))
            for model in self.models
        }

    @property
    def call_count(self) -> int:
        return sum(int(getattr(client, "call_count", 0) or 0) for client in self._clients.values())

    def complete_json(self, prompt: str) -> dict[str, Any]:
        errors: list[dict[str, str]] = []
        for model in self.models:
            result = self._clients[model].complete_json(prompt)
            if not isinstance(result, dict):
                return {
                    "status": "error",
                    "error": f"unexpected LLM result type from {model}: {type(result).__name__}",
                    "_llm_model": model,
                    "_llm_attempted_models": self.models,
                }
            if result.get("status") != "error":
                tagged = dict(result)
                tagged["_llm_model"] = model
                tagged["_llm_attempted_models"] = self.models[: len(errors) + 1]
                tagged["_llm_fallback_used"] = model != self.primary_config.model
                if errors:
                    tagged["_llm_prior_errors"] = errors
                return tagged
            errors.append({"model": model, "error": str(result.get("error") or "")})
            if not _llm_error_allows_fallback(result):
                break
        return {
            "status": "error",
            "code": "all_review_models_unavailable",
            "error": "All configured LLM reviewer models are unavailable; update LLM service information.",
            "_llm_model": self.models[min(len(errors), len(self.models)) - 1] if errors else self.primary_config.model,
            "_llm_attempted_models": self.models[: max(1, len(errors))],
            "_llm_prior_errors": errors,
        }


def _dedupe_models(models: list[str]) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for model in models:
        normalized = model.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        rows.append(normalized)
    return rows


def _llm_error_allows_fallback(result: dict[str, Any]) -> bool:
    code = str(result.get("code") or "")
    if code in {"llm_call_budget_exceeded", "llm_cost_budget_exceeded"}:
        return False
    error = str(result.get("error") or "").lower()
    return "budget exceeded" not in error


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

def _review_concurrency(settings: LlmCheckSettings) -> int:
    return max(1, settings.review_concurrency or settings.concurrency)


def _patch_concurrency(settings: LlmCheckSettings) -> int:
    return max(1, settings.patch_concurrency or settings.review_concurrency or settings.concurrency)


class LlmCallRecorder:
    def __init__(
        self,
        client: Any,
        *,
        report_path: Path,
        model: str,
        document_id: str,
        max_calls: int = 0,
        max_cost: float = 0.0,
        estimated_cost_per_1k_chars: float = DEFAULT_LLM_ESTIMATED_COST_PER_1K_CHARS,
    ) -> None:
        self._client = client
        self._report_path = report_path
        self._model = model
        self._document_id = document_id
        self._max_calls = max(0, int(max_calls))
        self._max_cost = max(0.0, float(max_cost))
        self._estimated_cost_per_1k_chars = max(0.0, float(estimated_cost_per_1k_chars))
        self._estimated_cost_total = 0.0
        self._lock = Lock()
        self._local_call_count = 0
        self._report_path.parent.mkdir(parents=True, exist_ok=True)
        self._report_path.write_text("", encoding="utf-8")

    @property
    def call_count(self) -> int:
        return int(getattr(self._client, "call_count", self._local_call_count) or self._local_call_count)

    def complete_json(self, prompt: str) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        stage = _infer_llm_call_stage(prompt)
        started_at = datetime.now(timezone.utc)
        started = time.monotonic()
        estimated_input_cost = self._estimate_cost(len(prompt), 0)
        with self._lock:
            if self._max_calls and self._local_call_count >= self._max_calls:
                return self._budget_error_result(
                    call_index=self._local_call_count + 1,
                    stage=stage,
                    request_id=request_id,
                    started_at=started_at,
                    started=started,
                    input_chars=len(prompt),
                    code="llm_call_budget_exceeded",
                    error=f"LLM call budget exceeded: {self._local_call_count}/{self._max_calls}",
                )
            if self._max_cost and self._estimated_cost_total + estimated_input_cost > self._max_cost:
                return self._budget_error_result(
                    call_index=self._local_call_count + 1,
                    stage=stage,
                    request_id=request_id,
                    started_at=started_at,
                    started=started,
                    input_chars=len(prompt),
                    code="llm_cost_budget_exceeded",
                    error=f"LLM cost budget exceeded: {round(self._estimated_cost_total + estimated_input_cost, 6)}/{self._max_cost}",
                )
            self._local_call_count += 1
            call_index = self._local_call_count
        try:
            result = self._client.complete_json(prompt)
        except Exception as exc:
            finished_at = datetime.now(timezone.utc)
            self._append_call_record(
                {
                    "document_id": self._document_id,
                    "call_index": call_index,
                    "stage": stage,
                    "model": self._model,
                    "request_id": request_id,
                    "started_at": _isoformat_utc(started_at),
                    "finished_at": _isoformat_utc(finished_at),
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "status": "error",
                    "retry_count": 0,
                    "input_chars": len(prompt),
                    "output_chars": 0,
                    "estimated_cost": None,
                    "error": str(exc),
                }
            )
            raise
        finished_at = datetime.now(timezone.utc)
        if not isinstance(result, dict):
            output_text = str(result)
            status = "error"
            error = f"unexpected result type: {type(result).__name__}"
        else:
            output_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
            status = "error" if result.get("status") == "error" else "ok"
            error = str(result.get("error") or "") if status == "error" else ""
        record_model = str(result.get("_llm_model") or self._model) if isinstance(result, dict) else self._model
        estimated_cost = self._estimate_cost(len(prompt), len(output_text))
        with self._lock:
            self._estimated_cost_total += estimated_cost
        record = {
            "document_id": self._document_id,
            "call_index": call_index,
            "stage": stage,
            "model": record_model,
            "request_id": request_id,
            "started_at": _isoformat_utc(started_at),
            "finished_at": _isoformat_utc(finished_at),
            "duration_seconds": round(time.monotonic() - started, 3),
            "status": status,
            "retry_count": int(result.get("retry_count") or 0) if isinstance(result, dict) else 0,
            "input_chars": len(prompt),
            "output_chars": len(output_text),
            "estimated_cost": estimated_cost,
            "estimated_cost_total": round(self._estimated_cost_total, 6),
            "error": error,
        }
        if isinstance(result, dict):
            if result.get("_llm_fallback_used") is not None:
                record["fallback_used"] = bool(result.get("_llm_fallback_used"))
            if result.get("_llm_attempted_models"):
                record["attempted_models"] = result.get("_llm_attempted_models")
            if result.get("_llm_prior_errors"):
                record["prior_model_errors"] = result.get("_llm_prior_errors")
        self._append_call_record(record)
        return result


    def _budget_error_result(
        self,
        *,
        call_index: int,
        stage: str,
        request_id: str,
        started_at: datetime,
        started: float,
        input_chars: int,
        code: str,
        error: str,
    ) -> dict[str, Any]:
        finished_at = datetime.now(timezone.utc)
        self._append_call_record(
            {
                "document_id": self._document_id,
                "call_index": call_index,
                "stage": stage,
                "model": self._model,
                "request_id": request_id,
                "started_at": _isoformat_utc(started_at),
                "finished_at": _isoformat_utc(finished_at),
                "duration_seconds": round(time.monotonic() - started, 3),
                "status": "error",
                "retry_count": 0,
                "input_chars": input_chars,
                "output_chars": 0,
                "estimated_cost": 0.0,
                "estimated_cost_total": round(self._estimated_cost_total, 6),
                "error": error,
                "code": code,
            }
        )
        return {"status": "error", "error": error, "code": code}

    def _estimate_cost(self, input_chars: int, output_chars: int) -> float:
        if self._estimated_cost_per_1k_chars <= 0:
            return 0.0
        return round(((max(0, input_chars) + max(0, output_chars)) / 1000.0) * self._estimated_cost_per_1k_chars, 6)

    def _append_call_record(self, record: dict[str, Any]) -> None:
        with self._lock:
            with self._report_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _infer_llm_call_stage(prompt: str) -> str:
    if "人类视角审查员" in prompt or "safe_fix_type" in prompt:
        return "llm_review"
    if '"corrected_text"' in prompt:
        return "llm_correction"
    if '"repaired_text"' in prompt:
        return "llm_repair"
    if '"blocking_issues"' in prompt:
        return "llm_acceptance"
    return "llm_unknown"


def _isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _artifact_binding_report(*, final_path: Path, pdf_path: Path, source_text: str) -> dict[str, Any]:
    markdown_text = final_path.read_text(encoding="utf-8") if final_path.exists() else ""
    final_markdown_sha256 = _sha256(markdown_text) if markdown_text else ""
    pdf_source_text_sha256 = _sha256(source_text)
    pdf_binary_sha256 = _sha256_bytes(pdf_path.read_bytes()) if pdf_path.exists() and pdf_path.stat().st_size > 0 else ""
    errors: list[str] = []
    if not final_markdown_sha256:
        errors.append("missing_final_markdown")
    if not pdf_binary_sha256:
        errors.append("missing_text_pdf")
    if final_markdown_sha256 and final_markdown_sha256 != pdf_source_text_sha256:
        errors.append("pdf_md_mismatch")
    return {
        "status": "passed" if not errors else "failed",
        "accepted": not errors,
        "blocking_errors": errors,
        "final_markdown_path": str(final_path),
        "text_pdf_path": str(pdf_path),
        "final_markdown_sha256": final_markdown_sha256,
        "pdf_source_text_sha256": pdf_source_text_sha256,
        "pdf_binary_sha256": pdf_binary_sha256,
    }


def _remove_file_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def process_one_document(
    *,
    path: Path,
    output_dir: Path,
    settings: LlmCheckSettings,
    client: Any,
    document_title: str | None = None,
) -> DocumentResult:
    document_id = _document_id_for_path(path)
    process_dir = output_dir / PROCESS_DIR_NAME
    reports_dir = process_dir / "reports"
    drafts_dir = process_dir / "drafts"
    clean_dir = process_dir / "clean"
    final_dir = output_dir / FINAL_MARKDOWN_DIR_NAME
    pdf_dir = output_dir / TEXT_PDF_DIR_NAME
    for directory in (reports_dir, drafts_dir, clean_dir, final_dir, pdf_dir):
        directory.mkdir(parents=True, exist_ok=True)
    cross_report_path = _resolve_cross_report_path(path, process_dir=process_dir, document_id=document_id)

    correction_report = reports_dir / f"{document_id}.llm_correction.json"
    acceptance_report = reports_dir / f"{document_id}.llm_acceptance.json"
    review_report = reports_dir / f"{document_id}.llm_review.json"
    repair_report = reports_dir / f"{document_id}.llm_repair.json"
    safe_patch_report = reports_dir / f"{document_id}.safe_patch.json"
    local_repair_report = reports_dir / f"{document_id}.local_repair.json"
    quality_report = reports_dir / f"{document_id}.quality.json"
    cleaning_report = clean_dir / f"{document_id}.cleaning_report.json"
    rule_changes_report = clean_dir / f"{document_id}.rule_changes.json"
    cleaned_path = clean_dir / f"{document_id}.cleaned.md"
    pre_llm_path = clean_dir / f"{document_id}.pre_llm.md"
    finalization_report = reports_dir / f"{document_id}.finalization.json"
    final_acceptance_report_path = reports_dir / f"{document_id}.final_acceptance.json"
    llm_calls_report = reports_dir / f"{document_id}.llm_calls.jsonl"
    artifact_binding_report = reports_dir / f"{document_id}.artifact_binding.json"
    draft_path = drafts_dir / f"{document_id}.md"
    final_path = final_dir / f"{document_id}.md"
    pdf_path = pdf_dir / f"{document_id}.pdf"
    pdf_title = document_title or path.stem
    profile = get_profile(settings.profile_id)
    document_client = LlmCallRecorder(
        client,
        report_path=llm_calls_report,
        model=settings.llm_model,
        document_id=document_id,
        max_calls=settings.llm_max_calls_per_book,
        max_cost=settings.llm_max_cost_per_book,
        estimated_cost_per_1k_chars=settings.llm_estimated_cost_per_1k_chars,
    )

    try:
        _mem_cache.clear()
        source_text = path.read_text(encoding="utf-8", errors="replace")
        cleaning_payload = clean_markdown_with_report(source_text)
        cleaned = str(cleaning_payload.get("text") or "")
        cleaned_path.write_text(cleaned, encoding="utf-8")
        _write_json(cleaning_report, {key: value for key, value in cleaning_payload.items() if key != "text"})
        _write_json(rule_changes_report, {"rule_changes": cleaning_payload.get("rule_changes", [])})
        pre_llm_text = cleaned
        pre_llm_gate: dict[str, Any] | None = None
        if settings.llm_mode in {"review-first", "local-gate"}:
            pre_llm_finalization = finalize_standard_document(cleaned)
            _write_json(finalization_report, pre_llm_finalization)
            candidate_text = clean_markdown_text(str(pre_llm_finalization.get("text") or cleaned))
            # Refuse structure collapse: if finalize/clean is worse than cleaned for
            # semantic-unit metrics, keep cleaned and record the guard decision.
            pre_llm_text, structure_guard = _prefer_better_structure_text(
                cleaned=cleaned,
                candidate=candidate_text,
            )
            if structure_guard.get("used_cleaned_fallback"):
                pre_llm_finalization = {
                    **pre_llm_finalization,
                    "structure_guard": structure_guard,
                    "text": pre_llm_text,
                }
                _write_json(finalization_report, pre_llm_finalization)
            pre_llm_path.write_text(pre_llm_text, encoding="utf-8")
            pre_llm_gate = final_acceptance_report(pre_llm_text)
            pre_llm_gate["phase"] = "pre_llm"
            if structure_guard:
                pre_llm_gate["structure_guard"] = structure_guard
        quality_payload = {
            "source_path": str(path),
            "cleaned_path": str(cleaned_path),
            "pre_llm_path": str(pre_llm_path) if pre_llm_path.exists() else "",
            "cleaning_report_path": str(cleaning_report),
            "rule_changes_report_path": str(rule_changes_report),
            "llm_calls_report_path": str(llm_calls_report),
            "input_sha256": _sha256(source_text),
            "cleaned_sha256": _sha256(cleaned),
            "pre_llm_sha256": _sha256(pre_llm_text),
            "profile_id": profile.id,
            "llm_chunk_chars": max(MIN_LLM_CHUNK_CHARS, settings.llm_chunk_chars),
            "llm_chunk_count": len(split_text_chunks(pre_llm_text, max_chars=settings.llm_chunk_chars)),
            "errors": quality_errors(pre_llm_text),
            "hints": quality_hints(pre_llm_text),
        }
        if pre_llm_gate is not None:
            quality_payload["pre_llm_gate"] = pre_llm_gate
        _write_json(quality_report, quality_payload)
        if settings.llm_mode in {"review-first", "local-gate"}:
            draft_path.write_text(pre_llm_text, encoding="utf-8")
            if pre_llm_gate is not None and pre_llm_gate.get("accepted") is not True:
                _write_json(final_acceptance_report_path, pre_llm_gate)
                if not llm_calls_report.exists():
                    llm_calls_report.write_text("", encoding="utf-8")
                return DocumentResult(
                    document_id=document_id,
                    source_path=str(path),
                    status="pre_llm_quality_failed",
                    draft_path=str(draft_path),
                    llm_calls_report_path=str(llm_calls_report),
                    profile_id=profile.id,
                    finalization_report_path=str(finalization_report),
                    final_acceptance_report_path=str(final_acceptance_report_path),
                    cross_report_path=cross_report_path,
                    error="pre-LLM deterministic quality gate failed",
                )
            if settings.llm_mode == "local-gate":
                if not llm_calls_report.exists():
                    llm_calls_report.write_text("", encoding="utf-8")
                return _finalize_and_write_document(
                    document_id=document_id,
                    source_path=str(path),
                    corrected_text=pre_llm_text,
                    draft_path=draft_path,
                    final_path=final_path,
                    pdf_path=pdf_path,
                    pdf_title=pdf_title,
                    correction_report=None,
                    acceptance_report=None,
                    repair_report=None,
                    finalization_report=finalization_report,
                    final_acceptance_report_path=final_acceptance_report_path,
                    profile=profile,
                    review_report=None,
                    llm_calls_report=llm_calls_report,
                    artifact_binding_report=artifact_binding_report,
                    cross_report_path=cross_report_path,
                )
            review = review_text_concurrently(
                source_name=path.name,
                text_path=pre_llm_path,
                text=pre_llm_text,
                client=document_client,
                model=settings.llm_model,
                concurrency=_review_concurrency(settings),
                max_chars=settings.llm_chunk_chars,
                chunk_report_dir=reports_dir / f"{document_id}.llm_review_chunks",
                profile=profile,
            )
            _write_json(review_report, review)
            if review.get("accepted") is True:
                return _finalize_and_write_document(
                    document_id=document_id,
                    source_path=str(path),
                    corrected_text=pre_llm_text,
                    draft_path=draft_path,
                    final_path=final_path,
                    pdf_path=pdf_path,
                    pdf_title=pdf_title,
                    correction_report=None,
                    acceptance_report=None,
                    repair_report=None,
                    finalization_report=finalization_report,
                    final_acceptance_report_path=final_acceptance_report_path,
                    profile=profile,
                    review_report=review_report,
                    llm_calls_report=llm_calls_report,
                    artifact_binding_report=artifact_binding_report,
                    cross_report_path=cross_report_path,
                )
            review_status = str(review.get("status") or "")
            if review_status != "llm_review_error":
                safe_patch = _apply_safe_review_patches(pre_llm_text, review)
                _write_json(safe_patch_report, safe_patch)
                if safe_patch.get("accepted") is True:
                    patched_text = str(safe_patch.get("patched_text") or "")
                    draft_path.write_text(patched_text, encoding="utf-8")
                    return _finalize_and_write_document(
                        document_id=document_id,
                        source_path=str(path),
                        corrected_text=patched_text,
                        draft_path=draft_path,
                        final_path=final_path,
                        pdf_path=pdf_path,
                        pdf_title=pdf_title,
                        correction_report=None,
                        acceptance_report=None,
                        repair_report=safe_patch_report,
                        finalization_report=finalization_report,
                        final_acceptance_report_path=final_acceptance_report_path,
                        profile=profile,
                        review_report=review_report,
                        llm_calls_report=llm_calls_report,
                        artifact_binding_report=artifact_binding_report,
                        cross_report_path=cross_report_path,
                    )
            return DocumentResult(
                document_id=document_id,
                source_path=str(path),
                status="llm_review_error" if review_status == "llm_review_error" else "llm_review_failed",
                draft_path=str(draft_path),
                review_report_path=str(review_report),
                repair_report_path=str(safe_patch_report if safe_patch_report.exists() else ""),
                llm_calls_report_path=str(llm_calls_report),
                profile_id=profile.id,
                cross_report_path=cross_report_path,
                error=str(review.get("llm_result", {}).get("summary") or review.get("status") or "LLM review 未通过"),
            )

        if settings.llm_mode != "legacy-correction":
            return DocumentResult(
                document_id=document_id,
                source_path=str(path),
                status="error",
                llm_calls_report_path=str(llm_calls_report),
                profile_id=profile.id,
                cross_report_path=cross_report_path,
                error=f"未知 LLM 模式：{settings.llm_mode}",
            )

        correction = correct_text_concurrently(
            source_name=path.name,
            text_path=path,
            text=cleaned,
            client=document_client,
            model=settings.llm_model,
            concurrency=_review_concurrency(settings),
            max_chars=settings.llm_chunk_chars,
            chunk_report_dir=reports_dir / f"{document_id}.llm_correction_chunks",
            profile=profile,
        )
        _write_json(correction_report, correction)
        if correction.get("draft_ready") is not True:
            return DocumentResult(
                document_id=document_id,
                source_path=str(path),
                status="correction_failed",
                correction_report_path=str(correction_report),
                llm_calls_report_path=str(llm_calls_report),
                profile_id=profile.id,
                cross_report_path=cross_report_path,
                error=str(correction.get("llm_result", {}).get("error") or correction.get("status") or "LLM 纠错未通过"),
            )
        corrected_text = clean_markdown_text(str(correction.get("corrected_text") or ""))
        if not corrected_text:
            return DocumentResult(
                document_id=document_id,
                source_path=str(path),
                status="empty_corrected_text",
                correction_report_path=str(correction_report),
                llm_calls_report_path=str(llm_calls_report),
                profile_id=profile.id,
                cross_report_path=cross_report_path,
                error="LLM corrected_text 为空",
            )
        draft_path.write_text(corrected_text, encoding="utf-8")

        # ── 预分片：纠错阶段复用 ──
        initial_chunks = split_text_chunks(corrected_text, max_chars=settings.llm_chunk_chars)

        # ── 验收 → 本地修复 → LLM 返修循环 ──
        acceptance = _accept_with_local_repair(
            source_name=path.name,
            text_path=draft_path,
            text=corrected_text,
            client=document_client,
            model=settings.llm_model,
            concurrency=_review_concurrency(settings),
            max_chars=settings.llm_chunk_chars,
            chunk_report_dir=reports_dir / f"{document_id}.llm_acceptance_chunks",
            local_repair_report=local_repair_report,
            acceptance_report=acceptance_report,
            profile=profile,
            pre_split_chunks=initial_chunks,
        )
        corrected_text = draft_path.read_text(encoding="utf-8")

        if acceptance.get("accepted") is True:
            return _finalize_and_write_document(
                document_id=document_id,
                source_path=str(path),
                corrected_text=corrected_text,
                draft_path=draft_path,
                final_path=final_path,
                pdf_path=pdf_path,
                pdf_title=pdf_title,
                correction_report=correction_report,
                acceptance_report=acceptance_report,
                repair_report=local_repair_report if local_repair_report.exists() else None,
                finalization_report=finalization_report,
                final_acceptance_report_path=final_acceptance_report_path,
                profile=profile,
                llm_calls_report=llm_calls_report,
                artifact_binding_report=artifact_binding_report,
                cross_report_path=cross_report_path,
            )

        # LLM 返修轮次
        repair_rounds = max(0, settings.acceptance_repair_rounds)
        last_repair_report_path: Path = local_repair_report
        for repair_round in range(1, repair_rounds + 1):
            current_chunks = split_text_chunks(corrected_text, max_chars=settings.llm_chunk_chars)
            repair = repair_failed_acceptance_chunks(
                source_name=path.name,
                text_path=draft_path,
                text=corrected_text,
                acceptance=acceptance,
                client=document_client,
                model=settings.llm_model,
                concurrency=_patch_concurrency(settings),
                max_chars=settings.llm_chunk_chars,
                repair_rounds=settings.acceptance_repair_rounds,
                repair_report_dir=reports_dir / f"{document_id}.llm_repair_chunks",
                audit_text=_load_preprocess_audit_text(path),
                profile=profile,
                pre_split_chunks=current_chunks,
            )
            repair["round"] = repair_round
            _write_json(repair_report, repair)
            last_repair_report_path = repair_report
            if repair.get("repaired") is not True:
                break
            corrected_text = clean_markdown_text(str(repair.get("repaired_text") or ""))
            draft_path.write_text(corrected_text, encoding="utf-8")

            acceptance = _accept_with_local_repair(
                source_name=path.name,
                text_path=draft_path,
                text=corrected_text,
                client=document_client,
                model=settings.llm_model,
                concurrency=_review_concurrency(settings),
                max_chars=settings.llm_chunk_chars,
                chunk_report_dir=reports_dir / f"{document_id}.llm_acceptance_round_{repair_round}_chunks",
                local_repair_report=local_repair_report,
                acceptance_report=acceptance_report,
                profile=profile,
            )
            corrected_text = draft_path.read_text(encoding="utf-8")

            if acceptance.get("accepted") is True:
                return _finalize_and_write_document(
                    document_id=document_id,
                    source_path=str(path),
                    corrected_text=corrected_text,
                    draft_path=draft_path,
                    final_path=final_path,
                    pdf_path=pdf_path,
                    pdf_title=pdf_title,
                    correction_report=correction_report,
                    acceptance_report=acceptance_report,
                    repair_report=repair_report,
                    finalization_report=finalization_report,
                    final_acceptance_report_path=final_acceptance_report_path,
                    profile=profile,
                    llm_calls_report=llm_calls_report,
                    artifact_binding_report=artifact_binding_report,
                    cross_report_path=cross_report_path,
                )

        if _acceptance_failure_allows_finalization(acceptance):
            return _finalize_and_write_document(
                document_id=document_id,
                source_path=str(path),
                corrected_text=corrected_text,
                draft_path=draft_path,
                final_path=final_path,
                pdf_path=pdf_path,
                pdf_title=pdf_title,
                correction_report=correction_report,
                acceptance_report=acceptance_report,
                repair_report=last_repair_report_path,
                finalization_report=finalization_report,
                final_acceptance_report_path=final_acceptance_report_path,
                profile=profile,
                llm_calls_report=llm_calls_report,
                artifact_binding_report=artifact_binding_report,
                cross_report_path=cross_report_path,
            )

        return DocumentResult(
            document_id=document_id,
            source_path=str(path),
            status="acceptance_failed",
            draft_path=str(draft_path),
            correction_report_path=str(correction_report),
            acceptance_report_path=str(acceptance_report),
            repair_report_path=str(last_repair_report_path if last_repair_report_path.exists() else local_repair_report),
            llm_calls_report_path=str(llm_calls_report),
            profile_id=profile.id,
            cross_report_path=cross_report_path,
            error=str(acceptance.get("llm_result", {}).get("summary") or acceptance.get("status") or "LLM 验收未通过"),
        )
    except Exception as error:  # noqa: BLE001 - one bad document should be reported, not hide the batch state.
        return DocumentResult(
            document_id=document_id,
            source_path=str(path),
            status="error",
            llm_calls_report_path=str(llm_calls_report),
            profile_id=profile.id,
            cross_report_path=cross_report_path,
            error=str(error),
        )

SAFE_REVIEW_PATCH_CATEGORIES = {"latex_artifact"}
SAFE_REVIEW_PATCH_TYPES = {"rule_fix", "safe_llm_patch"}
SAFE_REVIEW_PATCH_SEVERITIES = {"blocking", "major"}


def _apply_safe_review_patches(text: str, review: dict[str, Any]) -> dict[str, Any]:
    """Apply only deterministic, excerpt-scoped fixes for review issues."""
    issues = [issue for issue in review.get("issues", []) if isinstance(issue, dict)]
    blocking_issues = [_review_issue_summary(issue) for issue in issues if _review_issue_is_blocking(issue)]
    patched_text = text
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for issue in issues:
        if not _review_issue_is_blocking(issue):
            continue
        if not _review_issue_is_safe_patch_candidate(issue):
            skipped.append({**_review_issue_summary(issue), "reason": "not_safe_patch_candidate"})
            continue
        excerpt = str(issue.get("excerpt") or "").strip()
        if not excerpt:
            skipped.append({**_review_issue_summary(issue), "reason": "missing_excerpt"})
            continue
        replacement = clean_markdown_text(excerpt).strip()
        safety_error = _safe_patch_replacement_error(excerpt, replacement, patched_text)
        if safety_error:
            skipped.append({**_review_issue_summary(issue), "reason": safety_error, "before": excerpt[:160], "after": replacement[:160]})
            continue
        patched_text = patched_text.replace(excerpt, replacement, 1)
        applied.append({**_review_issue_summary(issue), "before": excerpt[:160], "after": replacement[:160], "source": "rule_fix"})

    final_report = final_acceptance_report(patched_text) if applied else {"accepted": False, "blocking_errors": []}
    unresolved = [item for item in blocking_issues if item.get("id") not in {change.get("id") for change in applied}]
    accepted = bool(applied) and not unresolved and final_report.get("accepted") is True
    return {
        "status": "patched" if accepted else "manual_review_required",
        "accepted": accepted,
        "input_sha256": _sha256(text),
        "output_sha256": _sha256(patched_text) if applied else "",
        "applied_patch_count": len(applied),
        "skipped_patch_count": len(skipped),
        "blocking_issue_count": len(blocking_issues),
        "applied_patches": applied,
        "skipped_patches": skipped,
        "unresolved_issues": unresolved,
        "final_acceptance": final_report,
        "patched_text": patched_text if accepted else "",
        "summary": "安全补丁已通过最终验收" if accepted else "仍需人工复核或更强修复链路",
    }


def _review_issue_is_blocking(issue: dict[str, Any]) -> bool:
    return str(issue.get("severity") or "").strip().lower() in SAFE_REVIEW_PATCH_SEVERITIES


def _review_issue_is_safe_patch_candidate(issue: dict[str, Any]) -> bool:
    category = str(issue.get("category") or "").strip()
    safe_fix_type = str(issue.get("safe_fix_type") or "").strip()
    return category in SAFE_REVIEW_PATCH_CATEGORIES and safe_fix_type in SAFE_REVIEW_PATCH_TYPES


def _safe_patch_replacement_error(excerpt: str, replacement: str, text: str) -> str:
    if not replacement or replacement == excerpt:
        return "no_deterministic_change"
    if len(excerpt) > 240 or len(replacement) > 240:
        return "excerpt_too_large"
    if text.count(excerpt) != 1:
        return "excerpt_not_unique"
    if _numeric_tokens(excerpt) != _numeric_tokens(replacement):
        return "numeric_tokens_changed"
    return ""


def _numeric_tokens(value: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?", value)


def _review_issue_summary(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(issue.get("id") or ""),
        "chunk_index": issue.get("chunk_index"),
        "category": str(issue.get("category") or ""),
        "severity": str(issue.get("severity") or ""),
        "safe_fix_type": str(issue.get("safe_fix_type") or ""),
        "location_hint": str(issue.get("location_hint") or ""),
        "reason": str(issue.get("reason") or ""),
    }

def _acceptance_failure_allows_finalization(acceptance: dict[str, Any]) -> bool:
    if acceptance.get("accepted") is True:
        return False
    failed_chunks = [
        chunk
        for chunk in acceptance.get("chunks", [])
        if isinstance(chunk, dict) and chunk.get("accepted") is not True
    ]
    if not failed_chunks:
        return False

    saw_issue = False
    for chunk in failed_chunks:
        llm_result = chunk.get("llm_result")
        if not isinstance(llm_result, dict):
            return False
        issues = llm_result.get("blocking_issues")
        if not isinstance(issues, list) or not issues:
            return False
        if not _issues_are_finalization_safe(issues):
            return False
        saw_issue = True

    llm_result = acceptance.get("llm_result")
    if isinstance(llm_result, dict):
        issues = llm_result.get("blocking_issues")
        if isinstance(issues, list) and issues and not _issues_are_finalization_safe(issues):
            return False
    return saw_issue


def _issues_are_finalization_safe(issues: list[Any]) -> bool:
    for issue in issues:
        if not isinstance(issue, dict):
            return False
        category = str(issue.get("category") or "").strip()
        if category not in FINALIZATION_SAFE_ACCEPTANCE_CATEGORIES:
            return False
        excerpt = str(issue.get("excerpt") or "")
        suggested_action = str(issue.get("suggested_action") or "").lower()
        if category == "layout" and "\n" not in excerpt and "merge" not in suggested_action:
            return False
    return True


def _accept_with_local_repair(
    *,
    source_name: str,
    text_path: Path,
    text: str,
    client: Any,
    model: str,
    concurrency: int,
    max_chars: int,
    chunk_report_dir: Path,
    local_repair_report: Path,
    acceptance_report: Path,
    profile: DocumentProfile,
    pre_split_chunks: list[TextChunk] | None = None,
) -> dict[str, Any]:
    """验收文本，若未通过则尝试本地修复并重验。始终将最终验收结果写入 acceptance_report。"""
    acceptance = accept_text_concurrently(
        source_name=source_name,
        text_path=text_path,
        text=text,
        client=client,
        model=model,
        concurrency=concurrency,
        max_chars=max_chars,
        chunk_report_dir=chunk_report_dir,
        profile=profile,
        pre_split_chunks=pre_split_chunks,
    )
    _write_json(acceptance_report, acceptance)

    if acceptance.get("accepted") is True:
        return acceptance

    # 尝试本地定点修复
    local_repair = repair_acceptance_locally(text, acceptance)
    _write_json(local_repair_report, local_repair)
    if local_repair.get("repaired") is not True:
        return acceptance

    # 本地修复成功 → 重验
    repaired_text = clean_markdown_text(str(local_repair.get("repaired_text") or ""))
    text_path.write_text(repaired_text, encoding="utf-8")
    acceptance = accept_text_concurrently(
        source_name=source_name,
        text_path=text_path,
        text=repaired_text,
        client=client,
        model=model,
        concurrency=concurrency,
        max_chars=max_chars,
        chunk_report_dir=chunk_report_dir,
        profile=profile,
    )
    _write_json(acceptance_report, acceptance)
    return acceptance


def _finalize_and_write_document(
    *,
    document_id: str,
    source_path: str,
    corrected_text: str,
    draft_path: Path,
    final_path: Path,
    pdf_path: Path,
    pdf_title: str,
    correction_report: Path | None,
    acceptance_report: Path | None,
    repair_report: Path | None,
    finalization_report: Path,
    final_acceptance_report_path: Path,
    profile: DocumentProfile,
    review_report: Path | None = None,
    llm_calls_report: Path | None = None,
    artifact_binding_report: Path | None = None,
    cross_report_path: str = "",
) -> DocumentResult:
    finalization = finalize_standard_document(corrected_text)
    _write_json(finalization_report, finalization)
    final_text = clean_markdown_text(str(finalization.get("text") or corrected_text))
    final_acceptance = final_acceptance_report(final_text)
    _write_json(final_acceptance_report_path, final_acceptance)
    correction_report_path = str(correction_report) if correction_report is not None and correction_report.exists() else ""
    acceptance_report_path = str(acceptance_report) if acceptance_report is not None and acceptance_report.exists() else ""
    review_report_path = str(review_report) if review_report is not None and review_report.exists() else ""
    repair_report_path = str(repair_report) if repair_report is not None and repair_report.exists() else ""
    llm_calls_report_path = str(llm_calls_report) if llm_calls_report is not None and llm_calls_report.exists() else ""
    artifact_binding_report_path = (
        str(artifact_binding_report) if artifact_binding_report is not None and artifact_binding_report.exists() else ""
    )
    if final_acceptance.get("accepted") is not True:
        draft_path.write_text(final_text, encoding="utf-8")
        return DocumentResult(
            document_id=document_id,
            source_path=source_path,
            status="final_acceptance_failed",
            draft_path=str(draft_path),
            correction_report_path=correction_report_path,
            acceptance_report_path=acceptance_report_path,
            review_report_path=review_report_path,
            repair_report_path=repair_report_path,
            llm_calls_report_path=llm_calls_report_path,
            artifact_binding_report_path=artifact_binding_report_path,
            artifact_binding_status="failed",
            profile_id=profile.id,
            finalization_report_path=str(finalization_report),
            final_acceptance_report_path=str(final_acceptance_report_path),
            cross_report_path=cross_report_path,
            error="whole-document final acceptance failed",
        )
    final_path.write_text(final_text, encoding="utf-8")
    write_text_pdf(pdf_path, title=pdf_title, text=final_text)
    binding_report = _artifact_binding_report(final_path=final_path, pdf_path=pdf_path, source_text=final_text)
    if artifact_binding_report is not None:
        _write_json(artifact_binding_report, binding_report)
    artifact_binding_report_path = str(artifact_binding_report) if artifact_binding_report is not None and artifact_binding_report.exists() else ""
    if binding_report.get("accepted") is not True:
        draft_path.write_text(final_text, encoding="utf-8")
        _remove_file_if_exists(final_path)
        _remove_file_if_exists(pdf_path)
        return DocumentResult(
            document_id=document_id,
            source_path=source_path,
            status="final_artifact_binding_failed",
            draft_path=str(draft_path),
            correction_report_path=correction_report_path,
            acceptance_report_path=acceptance_report_path,
            review_report_path=review_report_path,
            repair_report_path=repair_report_path,
            llm_calls_report_path=llm_calls_report_path,
            artifact_binding_report_path=artifact_binding_report_path,
            profile_id=profile.id,
            finalization_report_path=str(finalization_report),
            final_acceptance_report_path=str(final_acceptance_report_path),
            cross_report_path=cross_report_path,
            error="final markdown/pdf artifact binding failed",
        )
    return DocumentResult(
        document_id=document_id,
        source_path=source_path,
        status="passed",
        final_markdown_path=str(final_path),
        text_pdf_path=str(pdf_path),
        draft_path=str(draft_path),
        correction_report_path=correction_report_path,
        acceptance_report_path=acceptance_report_path,
        review_report_path=review_report_path,
        repair_report_path=repair_report_path,
        llm_calls_report_path=llm_calls_report_path,
        artifact_binding_report_path=artifact_binding_report_path,
        artifact_binding_status=str(binding_report.get("status") or ""),
        final_markdown_sha256=str(binding_report.get("final_markdown_sha256") or ""),
        pdf_source_sha256=str(binding_report.get("pdf_source_text_sha256") or ""),
        pdf_sha256=str(binding_report.get("pdf_binary_sha256") or ""),
        profile_id=profile.id,
        finalization_report_path=str(finalization_report),
        final_acceptance_report_path=str(final_acceptance_report_path),
        cross_report_path=cross_report_path,
        char_count=len(final_text),
        sha256=str(binding_report.get("final_markdown_sha256") or _sha256(final_text)),
    )


def split_text_chunks(text: str, *, max_chars: int = DEFAULT_LLM_CHUNK_CHARS) -> list[TextChunk]:
    limit = max(MIN_LLM_CHUNK_CHARS, max_chars)
    units = _markdown_units(text)
    if not units:
        return []
    chunks: list[str] = []
    current = ""
    for unit in units:
        if len(unit) > limit:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            if _has_structural_block(unit):
                chunks.append(unit.strip())
                continue
            chunks.extend(_split_long_unit(unit, max_chars=limit))
            continue
        candidate = f"{current}\n\n{unit}".strip() if current else unit.strip()
        if current and len(candidate) > limit:
            chunks.append(current.strip())
            current = unit.strip()
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    total = len(chunks)
    return [TextChunk(index=index, total=total, text=chunk) for index, chunk in enumerate(chunks, start=1)]


def correct_text_concurrently(
    *,
    source_name: str,
    text_path: Path,
    text: str,
    client: Any,
    model: str,
    concurrency: int,
    max_chars: int,
    chunk_report_dir: Path | None = None,
    profile: DocumentProfile | None = None,
    pre_split_chunks: list[TextChunk] | None = None,
) -> dict[str, Any]:
    document_profile = profile or get_profile()
    chunks = pre_split_chunks if pre_split_chunks is not None else split_text_chunks(text, max_chars=max_chars)
    results = _run_chunk_jobs(
        chunks,
        worker=lambda chunk: correction_result_payload(
            source_name=_chunk_source_name(source_name, chunk),
            text_path=text_path,
            text=chunk.text,
            client=client,
            model=model,
            profile=document_profile,
        ),
        concurrency=concurrency,
        chunk_report_dir=chunk_report_dir,
        report_prefix="correction_chunk",
        expected_profile_id=document_profile.id,
    )
    failures = [result for result in results if result.get("draft_ready") is not True]
    if failures:
        return {
            "source_name": source_name,
            "text_path": str(text_path),
            "status": "needs_manual_review",
            "draft_ready": False,
            "chunk_count": len(chunks),
            "failed_chunk_count": len(failures),
            "chunks": results,
            "llm_result": {
                "status": "needs_manual_review",
                "summary": f"{len(failures)}/{len(chunks)} 个 LLM 纠错片段未通过",
            },
        }
    corrected_chunks = [clean_markdown_text(str(result.get("llm_result", {}).get("corrected_text") or "")) for result in results]
    empty_failures = [
        result
        for result, corrected_chunk in zip(results, corrected_chunks)
        if not corrected_chunk and result.get("empty_corrected_text_allowed") is not True
    ]
    if empty_failures:
        return {
            "source_name": source_name,
            "text_path": str(text_path),
            "status": "empty_corrected_text",
            "draft_ready": False,
            "failed_chunk_count": len(empty_failures),
            "chunk_count": len(chunks),
            "chunks": results,
            "llm_result": {"status": "empty_corrected_text", "summary": "至少一个 LLM 纠错片段返回空文本"},
        }
    corrected_text = "\n\n".join(chunk for chunk in corrected_chunks if chunk).strip() + "\n"
    return {
        "source_name": source_name,
        "text_path": str(text_path),
        "input_sha256": _sha256(text),
        "output_sha256": _sha256(corrected_text),
        "model": model,
        "profile_id": document_profile.id,
        "status": "draft_ready",
        "draft_ready": True,
        "chunk_count": len(chunks),
        "corrected_text": corrected_text,
        "chunks": results,
        "llm_result": {
            "status": "draft_ready",
            "confidence": min(float(result.get("llm_result", {}).get("confidence") or 0.0) for result in results) if results else 0.0,
            "summary": f"已完成 {len(chunks)} 个片段的并发纠错并按原顺序合并",
            "corrected_text": corrected_text,
        },
    }


def review_text_concurrently(
    *,
    source_name: str,
    text_path: Path,
    text: str,
    client: Any,
    model: str,
    concurrency: int,
    max_chars: int,
    chunk_report_dir: Path | None = None,
    profile: DocumentProfile | None = None,
    pre_split_chunks: list[TextChunk] | None = None,
) -> dict[str, Any]:
    document_profile = profile or get_profile()
    chunks = pre_split_chunks if pre_split_chunks is not None else split_text_chunks(text, max_chars=max_chars)
    results = _run_chunk_jobs(
        chunks,
        worker=lambda chunk: review_result_payload(
            source_name=_chunk_source_name(source_name, chunk),
            text_path=text_path,
            text=chunk.text,
            client=client,
            model=model,
            profile=document_profile,
        ),
        concurrency=concurrency,
        chunk_report_dir=chunk_report_dir,
        report_prefix="review_chunk",
        expected_profile_id=document_profile.id,
    )
    failures = [result for result in results if result.get("accepted") is not True]
    error_failures = [result for result in failures if str(result.get("status") or "") == "error" or str(result.get("llm_result", {}).get("status") or "") == "error"]
    issues: list[dict[str, Any]] = []
    manual_review_notes: list[str] = []
    for result in results:
        for issue in result.get("issues", []):
            if not isinstance(issue, dict):
                continue
            enriched = dict(issue)
            enriched.setdefault("chunk_index", result.get("chunk_index"))
            enriched.setdefault("chunk_total", result.get("chunk_total"))
            issues.append(enriched)
        for note in result.get("manual_review_notes", []):
            if isinstance(note, str) and note.strip():
                manual_review_notes.append(note.strip())
    accepted = not failures
    if accepted:
        summary = "全书所有片段审查通过"
        status = "reviewed"
    elif error_failures:
        first_error = str(error_failures[0].get("llm_result", {}).get("error") or error_failures[0].get("error") or "LLM review call failed")
        summary = f"{len(error_failures)}/{len(chunks)} 个片段 LLM 审查调用失败：{first_error}"
        status = "llm_review_error"
    else:
        summary = f"{len(failures)}/{len(chunks)} 个片段审查未通过"
        status = "reviewed"
    return {
        "source_name": source_name,
        "text_path": str(text_path),
        "content_sha256": _sha256(text),
        "model": model,
        "profile_id": document_profile.id,
        "status": status,
        "accepted": accepted,
        "chunk_count": len(chunks),
        "accepted_chunk_count": len(chunks) - len(failures),
        "failed_chunk_count": len(failures),
        "chunks": results,
        "issues": issues,
        "manual_review_notes": manual_review_notes,
        "llm_result": {
            "status": status,
            "summary": summary,
        },
    }


def accept_text_concurrently(
    *,
    source_name: str,
    text_path: Path,
    text: str,
    client: Any,
    model: str,
    concurrency: int,
    max_chars: int,
    chunk_report_dir: Path | None = None,
    profile: DocumentProfile | None = None,
    pre_split_chunks: list[TextChunk] | None = None,
) -> dict[str, Any]:
    document_profile = profile or get_profile()
    chunks = pre_split_chunks if pre_split_chunks is not None else split_text_chunks(text, max_chars=max_chars)
    results = _run_chunk_jobs(
        chunks,
        worker=lambda chunk: acceptance_result_payload(
            source_name=_chunk_source_name(source_name, chunk),
            text_path=text_path,
            text=chunk.text,
            client=client,
            model=model,
            profile=document_profile,
        ),
        concurrency=concurrency,
        chunk_report_dir=chunk_report_dir,
        report_prefix="acceptance_chunk",
        expected_profile_id=document_profile.id,
    )
    failures = [result for result in results if result.get("accepted") is not True]
    status = "passed" if not failures else "needs_revision"
    return {
        "source_name": source_name,
        "text_path": str(text_path),
        "content_sha256": _sha256(text),
        "model": model,
        "profile_id": document_profile.id,
        "status": status,
        "accepted": not failures,
        "chunk_count": len(chunks),
        "accepted_chunk_count": len(chunks) - len(failures),
        "failed_chunk_count": len(failures),
        "chunks": results,
        "llm_result": {
            "status": status,
            "summary": "全书所有片段验收通过" if not failures else f"{len(failures)}/{len(chunks)} 个片段验收未通过",
        },
    }


def repair_failed_acceptance_chunks(
    *,
    source_name: str,
    text_path: Path,
    text: str,
    acceptance: dict[str, Any],
    client: Any,
    model: str,
    concurrency: int,
    max_chars: int,
    repair_rounds: int,
    repair_report_dir: Path | None = None,
    audit_text: str = "",
    profile: DocumentProfile | None = None,
    pre_split_chunks: list[TextChunk] | None = None,
) -> dict[str, Any]:
    document_profile = profile or get_profile()
    if repair_rounds < 1 or acceptance.get("accepted") is True:
        return {"status": "skipped", "repaired": False, "summary": "无需返修"}
    chunks = pre_split_chunks if pre_split_chunks is not None else split_text_chunks(text, max_chars=max_chars)
    chunk_by_index = {chunk.index: chunk for chunk in chunks}
    failed_rows = [
        row
        for row in acceptance.get("chunks", [])
        if isinstance(row, dict) and row.get("accepted") is not True and int(row.get("chunk_index") or 0) in chunk_by_index
    ]
    if not failed_rows:
        return {"status": "skipped", "repaired": False, "summary": "没有可定位的失败片段"}
    if repair_report_dir is not None:
        repair_report_dir.mkdir(parents=True, exist_ok=True)
    repaired_by_index: dict[int, str] = {}
    repair_results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        futures = {}
        for failed in failed_rows:
            index = int(failed["chunk_index"])
            chunk = chunk_by_index[index]
            report_path = repair_report_dir / f"repair_chunk_{index:03d}.json" if repair_report_dir is not None else None
            cached = _cached_repair_result(report_path, chunk, expected_profile_id=document_profile.id) if report_path is not None else None
            if cached is not None:
                repair_results.append(cached)
                repaired_by_index[index] = clean_markdown_text(str(cached["llm_result"].get("repaired_text") or ""))
                continue
            futures[
                executor.submit(
                    repair_result_payload,
                    source_name=_chunk_source_name(source_name, chunk),
                    text_path=text_path,
                    text=chunk.text,
                    acceptance_issue=failed.get("llm_result") if isinstance(failed.get("llm_result"), dict) else failed,
                    previous_text=chunk_by_index[index - 1].text if index > 1 and index - 1 in chunk_by_index else "",
                    next_text=chunk_by_index[index + 1].text if index + 1 in chunk_by_index else "",
                    audit_text=_audit_snippet_for_chunk(chunk.text, audit_text),
                    client=client,
                    model=model,
                    profile=document_profile,
                )
            ] = (index, chunk, report_path)
        for future in as_completed(futures):
            index, chunk, report_path = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - isolate one bad repair chunk.
                result = {"status": "error", "error": str(exc)}
            if not isinstance(result, dict):
                result = {"status": "error", "error": f"unexpected result type: {type(result).__name__}"}
            result["chunk_index"] = index
            result["chunk_total"] = chunk.total
            result["input_chars"] = len(chunk.text)
            if result.get("repaired") is True:
                repaired_text = clean_markdown_text(str(result["llm_result"].get("repaired_text") or ""))
                if repaired_text:
                    repaired_by_index[index] = repaired_text
            repair_results.append(result)
            if report_path is not None:
                _write_json(report_path, result)
                _mem_cache[str(report_path)] = result
    repair_results.sort(key=lambda row: int(row.get("chunk_index") or 0))
    if not repaired_by_index:
        return {
            "status": "not_repaired",
            "repaired": False,
            "failed_chunk_count": len(failed_rows),
            "repaired_chunk_count": 0,
            "chunks": repair_results,
            "summary": "失败片段未产生可合并返修文本",
        }
    repaired_text = "\n\n".join(repaired_by_index.get(chunk.index, chunk.text).strip() for chunk in chunks if chunk.text.strip()).strip() + "\n"
    return {
        "status": "repaired",
        "repaired": True,
        "input_sha256": _sha256(text),
        "output_sha256": _sha256(repaired_text),
        "failed_chunk_count": len(failed_rows),
        "repaired_chunk_count": len(repaired_by_index),
        "chunks": repair_results,
        "repaired_text": repaired_text,
        "summary": f"已返修 {len(repaired_by_index)}/{len(failed_rows)} 个失败验收片段",
    }


def _run_chunk_jobs(
    chunks: list[TextChunk],
    *,
    worker: Any,
    concurrency: int,
    chunk_report_dir: Path | None = None,
    report_prefix: str = "chunk",
    expected_profile_id: str = "",
) -> list[dict[str, Any]]:
    results: list[dict[str, Any] | None] = [None] * len(chunks)
    if chunk_report_dir is not None:
        chunk_report_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        futures = {}
        for chunk in chunks:
            report_path = chunk_report_dir / f"{report_prefix}_{chunk.index:03d}.json" if chunk_report_dir is not None else None
            cached = (
                _cached_chunk_result(report_path, chunk, report_prefix=report_prefix, expected_profile_id=expected_profile_id)
                if report_path is not None
                else None
            )
            if cached is not None:
                results[chunk.index - 1] = cached
                continue
            futures[executor.submit(worker, chunk)] = chunk
        for future in as_completed(futures):
            chunk = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - isolate one bad chunk, don't kill the batch.
                result = {
                    "source_name": f"{report_prefix}_{chunk.index:03d}",
                    "status": "error",
                    "error": str(exc),
                }
            if not isinstance(result, dict):
                result = {"status": "error", "error": f"unexpected result type: {type(result).__name__}"}
            result["chunk_index"] = chunk.index
            result["chunk_total"] = chunk.total
            result["input_chars"] = len(chunk.text)
            results[chunk.index - 1] = result
            if chunk_report_dir is not None:
                report_path = chunk_report_dir / f"{report_prefix}_{chunk.index:03d}.json"
                _write_json(report_path, result)
                _mem_cache[str(report_path)] = result
    return [result for result in results if result is not None]


def _cached_repair_result(path: Path | None, chunk: TextChunk, *, expected_profile_id: str = "") -> dict[str, Any] | None:
    if path is None or not path.exists() or path.stat().st_size == 0:
        return None
    cache_key = str(path)
    if cache_key in _mem_cache:
        result = _mem_cache[cache_key]
    else:
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        _mem_cache[cache_key] = result
    if expected_profile_id and result.get("profile_id") != expected_profile_id:
        return None
    if result.get("input_sha256") != _sha256(chunk.text) or result.get("repaired") is not True:
        return None
    result["chunk_index"] = chunk.index
    result["chunk_total"] = chunk.total
    result["input_chars"] = len(chunk.text)
    return result


def _cached_chunk_result(path: Path, chunk: TextChunk, *, report_prefix: str, expected_profile_id: str = "") -> dict[str, Any] | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    cache_key = str(path)
    if cache_key in _mem_cache:
        result = _mem_cache[cache_key]
    else:
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        _mem_cache[cache_key] = result
    if expected_profile_id and result.get("profile_id") != expected_profile_id:
        return None
    content_hash = _sha256(chunk.text)
    if report_prefix.startswith("correction"):
        if result.get("input_sha256") != content_hash or result.get("draft_ready") is not True:
            return None
    elif report_prefix.startswith("acceptance"):
        if result.get("content_sha256") != content_hash or result.get("accepted") is not True:
            return None
    elif report_prefix.startswith("review"):
        if result.get("content_sha256") != content_hash or result.get("status") != "reviewed":
            return None
    result["chunk_index"] = chunk.index
    result["chunk_total"] = chunk.total
    result["input_chars"] = len(chunk.text)
    return result


def _chunk_source_name(source_name: str, chunk: TextChunk) -> str:
    return f"{source_name} 第 {chunk.index}/{chunk.total} 片段"


def _structure_metrics(text: str) -> dict[str, int]:
    lines = text.splitlines()
    heading_count = sum(1 for line in lines if re.match(r"^#{1,6}\s+\S", line.strip()))
    max_line = max((len(line) for line in lines), default=0)
    return {
        "heading_count": heading_count,
        "max_line_chars": max_line,
        "line_count": len(lines),
        "char_count": len(text),
    }


def _prefer_better_structure_text(*, cleaned: str, candidate: str) -> tuple[str, dict[str, Any]]:
    """If finalize/clean collapses structure vs cleaned, keep cleaned for delivery.

    Historical skill runs wrote mega-line pre_llm/final while cleaned still had
    normal headings. Prefer the non-collapsed text so local-gate cannot pass a
    destroyed semantic-unit layout.
    """
    cleaned_metrics = _structure_metrics(cleaned)
    candidate_metrics = _structure_metrics(candidate)
    guard: dict[str, Any] = {
        "cleaned": cleaned_metrics,
        "candidate": candidate_metrics,
        "used_cleaned_fallback": False,
        "reasons": [],
    }
    reasons: list[str] = []
    cleaned_heads = cleaned_metrics["heading_count"]
    candidate_heads = candidate_metrics["heading_count"]
    cleaned_max = cleaned_metrics["max_line_chars"]
    candidate_max = candidate_metrics["max_line_chars"]

    # Candidate grew a mega line while cleaned did not.
    if candidate_max >= 3_000 and candidate_max > max(cleaned_max * 2, cleaned_max + 500):
        reasons.append("candidate_mega_line")
    # Heading structure collapsed (e.g. 100+ heads → few heads).
    if cleaned_heads >= 10 and candidate_heads < max(5, cleaned_heads // 3):
        reasons.append("candidate_heading_collapse")
    # Candidate fails segmentation gate while cleaned is better.
    candidate_errors = set(quality_errors(candidate))
    cleaned_errors = set(quality_errors(cleaned))
    for code in ("mega_line", "low_heading_density", "headingless_long_document"):
        if code in candidate_errors and code not in cleaned_errors:
            reasons.append(f"candidate_has_{code}")

    if reasons:
        guard["used_cleaned_fallback"] = True
        guard["reasons"] = reasons
        return cleaned, guard
    return candidate, guard


def _markdown_units(text: str) -> list[str]:
    normalized = text.strip()
    if not normalized:
        return []
    units: list[str] = []
    current: list[str] = []
    in_code_block = False
    details_depth = 0
    for line in normalized.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
        if not in_code_block:
            details_depth += len(re.findall(r"<details\b", line, flags=re.IGNORECASE))
            details_depth = max(0, details_depth - len(re.findall(r"</details>", line, flags=re.IGNORECASE)))
        if not stripped and not in_code_block and details_depth == 0:
            if current:
                units.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)
    if current:
        units.append("\n".join(current).strip())
    return [unit for unit in units if unit]


def _has_structural_block(text: str) -> bool:
    return bool(re.search(r"```|<details\b|</details>", text, flags=re.IGNORECASE))


def _split_long_unit(text: str, *, max_chars: int) -> list[str]:
    rows = [row.strip() for row in text.splitlines() if row.strip()]
    if len(rows) <= 1:
        return [text[index : index + max_chars].strip() for index in range(0, len(text), max_chars) if text[index : index + max_chars].strip()]
    chunks: list[str] = []
    current = ""
    for row in rows:
        candidate = f"{current}\n{row}".strip() if current else row
        if current and len(candidate) > max_chars:
            chunks.append(current.strip())
            current = row
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return chunks


def discover_markdown_files(input_path: Path) -> list[Path]:
    resolved = input_path.expanduser().resolve()
    if resolved.is_file() and resolved.suffix.lower() == ".md":
        return [resolved]
    if resolved.is_dir():
        return sorted(path for path in resolved.rglob("*.md") if path.is_file())
    return []


def _load_preprocess_audit_text(path: Path) -> str:
    manifest_path = path.parent.parent / "preprocess_manifest.json"
    if not manifest_path.exists():
        return ""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    ppx_markdown = manifest.get("ppx_markdown")
    if not isinstance(ppx_markdown, str) or not ppx_markdown:
        return ""
    ppx_path = Path(ppx_markdown)
    if not ppx_path.exists() or not ppx_path.is_file():
        return ""
    return ppx_path.read_text(encoding="utf-8", errors="replace")


def _audit_snippet_for_chunk(chunk_text: str, audit_text: str, *, max_chars: int = 4000) -> str:
    if not audit_text:
        return ""
    for anchor in _audit_anchor_candidates(chunk_text):
        index = audit_text.find(anchor)
        if index < 0:
            continue
        start = max(0, index - max_chars // 2)
        end = min(len(audit_text), index + max_chars // 2)
        return audit_text[start:end].strip()
    return ""


def _audit_anchor_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for line in text.splitlines():
        stripped = line.strip().strip("#*`> ")
        if len(stripped) < 12:
            continue
        candidates.append(stripped[:60])
        if len(stripped) > 60:
            candidates.append(stripped[-60:])
    compact = re.sub(r"\s+", "", text)
    for match in re.finditer(r"[\u4e00-\u9fff，。、《》“”（）()；：]{16,}", compact):
        candidates.append(match.group(0)[:40])
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def _preprocess_settings(settings: LlmCheckSettings) -> PreprocessSettings:
    return PreprocessSettings(
        mineru_api_url=settings.mineru_api_url,
        mineru_api_key=settings.mineru_api_key,
        mineru_model=settings.mineru_model or "vlm",
        mineru_concurrency=max(1, settings.mineru_concurrency),
        mineru_batch_size=max(1, settings.mineru_batch_size),
        mineru_timeout_seconds=max(10, settings.mineru_timeout_seconds),
        mineru_request_timeout_seconds=max(10, settings.mineru_request_timeout_seconds),
        mineru_max_retries=max(1, settings.mineru_max_retries),
        mineru_retry_backoff_seconds=max(0.0, settings.mineru_retry_backoff_seconds),
        enable_ppx=bool(getattr(settings, "enable_ppx", False)),
        mineru_fallback=(settings.mineru_fallback or "none") if bool(getattr(settings, "enable_ppx", False)) else "none",
        pdf_page_chunk_size=max(1, settings.pdf_page_chunk_size),
        ppx_command=settings.ppx_command,
        ppx_cwd=settings.ppx_cwd,
        ppx_timeout_seconds=max(10, settings.ppx_timeout_seconds),
        ppx_backend=settings.ppx_backend or "default",
        ppx_ocr=settings.ppx_ocr or "auto",
        ppx_formula=settings.ppx_formula or "no",
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _document_id_for_path(path: Path) -> str:
    """Prefer preprocess/<doc>/cross/initial.md parent doc stem over literal 'initial'."""
    parts = list(path.parts)
    if len(parts) >= 3 and parts[-1] == "initial.md" and parts[-2] == "cross":
        return safe_stem(parts[-3])
    if path.parent.name == "cross" and path.name == "initial.md":
        return safe_stem(path.parent.parent.name)
    return safe_stem(path.stem)


def _resolve_cross_report_path(path: Path, *, process_dir: Path, document_id: str) -> str:
    """Return cross_report.json path when present near the preprocess tree; else empty string."""
    candidates: list[Path] = []
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    # Common layout: process/preprocess/<doc>/cross/cross_report.json
    candidates.append(process_dir / "preprocess" / document_id / "cross" / "cross_report.json")
    # Source may already live under preprocess/<doc>/cross/initial.md
    if resolved.parent.name == "cross":
        candidates.append(resolved.parent / "cross_report.json")
        candidates.append(resolved.parent.parent / "cross" / "cross_report.json")
    candidates.append(resolved.parent / "cross" / "cross_report.json")
    # Walk up a few levels from source for nested mineru/ppx trees.
    parent = resolved.parent
    for _ in range(4):
        candidates.append(parent / "cross" / "cross_report.json")
        if parent == parent.parent:
            break
        parent = parent.parent
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return ""
