from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import hashlib
import json
import re

from llmcheck.llm import LlmClient, LlmConfig, acceptance_result_payload, correction_result_payload, repair_result_payload
from llmcheck.pdf import write_text_pdf
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
from llmcheck.quality import (
    clean_markdown_text,
    final_acceptance_report,
    finalize_standard_document,
    quality_errors,
    quality_hints,
    repair_acceptance_locally,
    safe_stem,
)


class LlmCheckError(RuntimeError):
    pass


DEFAULT_LLM_CHUNK_CHARS = 2_000
MIN_LLM_CHUNK_CHARS = 1_000
DEFAULT_LLM_CONCURRENCY = 10
PROCESS_DIR_NAME = "process"
FINAL_MARKDOWN_DIR_NAME = "md"
TEXT_PDF_DIR_NAME = "文字版pdf"


@dataclass(frozen=True)
class LlmCheckSettings:
    llm_api_url: str
    llm_api_key: str
    llm_model: str
    profile_id: str = DEFAULT_PROFILE_ID
    concurrency: int = DEFAULT_LLM_CONCURRENCY
    timeout_seconds: int = 600
    llm_chunk_chars: int = DEFAULT_LLM_CHUNK_CHARS
    acceptance_repair_rounds: int = 1
    mineru_api_url: str = "https://mineru.net"
    mineru_api_key: str = ""
    mineru_model: str = "vlm"
    mineru_concurrency: int = 12
    mineru_batch_size: int = DEFAULT_MINERU_BATCH_SIZE
    mineru_timeout_seconds: int = 3600
    mineru_request_timeout_seconds: int = DEFAULT_MINERU_REQUEST_TIMEOUT_SECONDS
    mineru_max_retries: int = DEFAULT_MINERU_MAX_RETRIES
    mineru_retry_backoff_seconds: float = DEFAULT_MINERU_RETRY_BACKOFF_SECONDS
    pdf_page_chunk_size: int = DEFAULT_PDF_PAGE_CHUNK_SIZE
    ppx_command: str = "/mnt/d/codex/memect-ppx/ppx"
    ppx_cwd: str = "/mnt/d/codex/memect-ppx"
    ppx_timeout_seconds: int = 3600
    ppx_backend: str = "default"
    ppx_ocr: str = "auto"
    ppx_formula: str = "no"


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
    repair_report_path: str = ""
    profile_id: str = ""
    finalization_report_path: str = ""
    final_acceptance_report_path: str = ""
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
    profile = get_profile(settings.profile_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    process_dir = output_dir / PROCESS_DIR_NAME
    docs = (
        preprocess_runner(input_path=input_path, output_dir=process_dir, settings=settings)
        if preprocess_runner is not None
        else prepare_markdown_inputs(input_path=input_path, output_dir=process_dir, settings=_preprocess_settings(settings))
    )
    if not docs:
        raise LlmCheckError(f"未发现可处理输入：{input_path}")
    actual_client = client or LlmClient(
        LlmConfig(
            api_url=settings.llm_api_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout_seconds=max(10, settings.timeout_seconds),
        )
    )
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
    report = {
        "status": "passed" if all(result.status == "passed" for result in ordered) else "review_required",
        "input_path": str(input_path.resolve()),
        "output_dir": str(output_dir.resolve()),
        "document_count": len(ordered),
        "passed_count": sum(1 for result in ordered if result.status == "passed"),
        "failed_count": sum(1 for result in ordered if result.status != "passed"),
        "model": settings.llm_model,
        "profile_id": profile.id,
        "concurrency": worker_count,
        "documents": [result.to_dict() for result in ordered],
    }
    reports_dir = process_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "llmcheck_summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (reports_dir / "llmcheck_manifest.jsonl").write_text(
        "\n".join(json.dumps(result.to_dict(), ensure_ascii=False) for result in ordered) + "\n",
        encoding="utf-8",
    )
    return report


def process_one_document(
    *,
    path: Path,
    output_dir: Path,
    settings: LlmCheckSettings,
    client: Any,
    document_title: str | None = None,
) -> DocumentResult:
    document_id = safe_stem(path.stem)
    process_dir = output_dir / PROCESS_DIR_NAME
    reports_dir = process_dir / "reports"
    drafts_dir = process_dir / "drafts"
    final_dir = output_dir / FINAL_MARKDOWN_DIR_NAME
    pdf_dir = output_dir / TEXT_PDF_DIR_NAME
    for directory in (reports_dir, drafts_dir, final_dir, pdf_dir):
        directory.mkdir(parents=True, exist_ok=True)

    correction_report = reports_dir / f"{document_id}.llm_correction.json"
    acceptance_report = reports_dir / f"{document_id}.llm_acceptance.json"
    repair_report = reports_dir / f"{document_id}.llm_repair.json"
    local_repair_report = reports_dir / f"{document_id}.local_repair.json"
    quality_report = reports_dir / f"{document_id}.quality.json"
    finalization_report = reports_dir / f"{document_id}.finalization.json"
    final_acceptance_report_path = reports_dir / f"{document_id}.final_acceptance.json"
    draft_path = drafts_dir / f"{document_id}.md"
    final_path = final_dir / f"{document_id}.md"
    pdf_path = pdf_dir / f"{document_id}.pdf"
    pdf_title = document_title or path.stem
    profile = get_profile(settings.profile_id)

    try:
        source_text = path.read_text(encoding="utf-8", errors="replace")
        cleaned = clean_markdown_text(source_text)
        quality_payload = {
            "source_path": str(path),
            "input_sha256": _sha256(source_text),
            "cleaned_sha256": _sha256(cleaned),
            "profile_id": profile.id,
            "llm_chunk_chars": max(MIN_LLM_CHUNK_CHARS, settings.llm_chunk_chars),
            "llm_chunk_count": len(split_text_chunks(cleaned, max_chars=settings.llm_chunk_chars)),
            "errors": quality_errors(cleaned),
            "hints": quality_hints(cleaned),
        }
        _write_json(quality_report, quality_payload)
        correction = correct_text_concurrently(
            source_name=path.name,
            text_path=path,
            text=cleaned,
            client=client,
            model=settings.llm_model,
            concurrency=settings.concurrency,
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
                profile_id=profile.id,
                error=str(correction.get("llm_result", {}).get("error") or correction.get("status") or "LLM 纠错未通过"),
            )
        corrected_text = clean_markdown_text(str(correction.get("corrected_text") or ""))
        if not corrected_text:
            return DocumentResult(
                document_id=document_id,
                source_path=str(path),
                status="empty_corrected_text",
                correction_report_path=str(correction_report),
                profile_id=profile.id,
                error="LLM corrected_text 为空",
            )
        draft_path.write_text(corrected_text, encoding="utf-8")
        acceptance = accept_text_concurrently(
            source_name=path.name,
            text_path=draft_path,
            text=corrected_text,
            client=client,
            model=settings.llm_model,
            concurrency=settings.concurrency,
            max_chars=settings.llm_chunk_chars,
            chunk_report_dir=reports_dir / f"{document_id}.llm_acceptance_chunks",
            profile=profile,
        )
        _write_json(acceptance_report, acceptance)
        if acceptance.get("accepted") is not True:
            local_repair = repair_acceptance_locally(corrected_text, acceptance)
            _write_json(local_repair_report, local_repair)
            if local_repair.get("repaired") is True:
                corrected_text = clean_markdown_text(str(local_repair.get("repaired_text") or ""))
                draft_path.write_text(corrected_text, encoding="utf-8")
                acceptance = accept_text_concurrently(
                    source_name=path.name,
                    text_path=draft_path,
                    text=corrected_text,
                    client=client,
                    model=settings.llm_model,
                    concurrency=settings.concurrency,
                    max_chars=settings.llm_chunk_chars,
                    chunk_report_dir=reports_dir / f"{document_id}.llm_acceptance_chunks",
                    profile=profile,
                )
                _write_json(acceptance_report, acceptance)
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
                        repair_report=local_repair_report,
                        finalization_report=finalization_report,
                        final_acceptance_report_path=final_acceptance_report_path,
                        profile=profile,
                    )
            repair_rounds = max(0, settings.acceptance_repair_rounds)
            for repair_round in range(1, repair_rounds + 1):
                repair = repair_failed_acceptance_chunks(
                    source_name=path.name,
                    text_path=draft_path,
                    text=corrected_text,
                    acceptance=acceptance,
                    client=client,
                    model=settings.llm_model,
                    concurrency=settings.concurrency,
                    max_chars=settings.llm_chunk_chars,
                    repair_rounds=settings.acceptance_repair_rounds,
                    repair_report_dir=reports_dir / f"{document_id}.llm_repair_chunks",
                    audit_text=_load_preprocess_audit_text(path),
                    profile=profile,
                )
                repair["round"] = repair_round
                _write_json(repair_report, repair)
                if repair.get("repaired") is not True:
                    break
                corrected_text = clean_markdown_text(str(repair.get("repaired_text") or ""))
                draft_path.write_text(corrected_text, encoding="utf-8")
                acceptance = accept_text_concurrently(
                    source_name=path.name,
                    text_path=draft_path,
                    text=corrected_text,
                    client=client,
                    model=settings.llm_model,
                    concurrency=settings.concurrency,
                    max_chars=settings.llm_chunk_chars,
                    chunk_report_dir=reports_dir / f"{document_id}.llm_acceptance_chunks",
                    profile=profile,
                )
                _write_json(acceptance_report, acceptance)
                if acceptance.get("accepted") is not True:
                    local_repair = repair_acceptance_locally(corrected_text, acceptance)
                    _write_json(local_repair_report, local_repair)
                    if local_repair.get("repaired") is True:
                        corrected_text = clean_markdown_text(str(local_repair.get("repaired_text") or ""))
                        draft_path.write_text(corrected_text, encoding="utf-8")
                        acceptance = accept_text_concurrently(
                            source_name=path.name,
                            text_path=draft_path,
                            text=corrected_text,
                            client=client,
                            model=settings.llm_model,
                            concurrency=settings.concurrency,
                            max_chars=settings.llm_chunk_chars,
                            chunk_report_dir=reports_dir / f"{document_id}.llm_acceptance_chunks",
                            profile=profile,
                        )
                _write_json(acceptance_report, acceptance)
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
                    )
            return DocumentResult(
                document_id=document_id,
                source_path=str(path),
                status="acceptance_failed",
                draft_path=str(draft_path),
                correction_report_path=str(correction_report),
                acceptance_report_path=str(acceptance_report),
                repair_report_path=str(repair_report if repair_report.exists() else local_repair_report),
                profile_id=profile.id,
                error=str(acceptance.get("llm_result", {}).get("summary") or acceptance.get("status") or "LLM 验收未通过"),
            )
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
            repair_report=repair_report if repair_report.exists() else None,
            finalization_report=finalization_report,
            final_acceptance_report_path=final_acceptance_report_path,
            profile=profile,
        )
    except Exception as error:  # noqa: BLE001 - one bad document should be reported, not hide the batch state.
        return DocumentResult(document_id=document_id, source_path=str(path), status="error", profile_id=profile.id, error=str(error))


def _finalize_and_write_document(
    *,
    document_id: str,
    source_path: str,
    corrected_text: str,
    draft_path: Path,
    final_path: Path,
    pdf_path: Path,
    pdf_title: str,
    correction_report: Path,
    acceptance_report: Path,
    repair_report: Path | None,
    finalization_report: Path,
    final_acceptance_report_path: Path,
    profile: DocumentProfile,
) -> DocumentResult:
    finalization = finalize_standard_document(corrected_text)
    _write_json(finalization_report, finalization)
    final_text = clean_markdown_text(str(finalization.get("text") or corrected_text))
    final_acceptance = final_acceptance_report(final_text)
    _write_json(final_acceptance_report_path, final_acceptance)
    repair_report_path = str(repair_report) if repair_report is not None and repair_report.exists() else ""
    if final_acceptance.get("accepted") is not True:
        draft_path.write_text(final_text, encoding="utf-8")
        return DocumentResult(
            document_id=document_id,
            source_path=source_path,
            status="final_acceptance_failed",
            draft_path=str(draft_path),
            correction_report_path=str(correction_report),
            acceptance_report_path=str(acceptance_report),
            repair_report_path=repair_report_path,
            profile_id=profile.id,
            finalization_report_path=str(finalization_report),
            final_acceptance_report_path=str(final_acceptance_report_path),
            error="whole-document final acceptance failed",
        )
    final_path.write_text(final_text, encoding="utf-8")
    write_text_pdf(pdf_path, title=pdf_title, text=final_text)
    return DocumentResult(
        document_id=document_id,
        source_path=source_path,
        status="passed",
        final_markdown_path=str(final_path),
        text_pdf_path=str(pdf_path),
        draft_path=str(draft_path),
        correction_report_path=str(correction_report),
        acceptance_report_path=str(acceptance_report),
        repair_report_path=repair_report_path,
        profile_id=profile.id,
        finalization_report_path=str(finalization_report),
        final_acceptance_report_path=str(final_acceptance_report_path),
        char_count=len(final_text),
        sha256=_sha256(final_text),
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
) -> dict[str, Any]:
    document_profile = profile or get_profile()
    chunks = split_text_chunks(text, max_chars=max_chars)
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
    corrected_chunks = [clean_markdown_text(str(result["llm_result"].get("corrected_text") or "")) for result in results]
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
            "confidence": min(float(result["llm_result"].get("confidence") or 0.0) for result in results) if results else 0.0,
            "summary": f"已完成 {len(chunks)} 个片段的并发纠错并按原顺序合并",
            "corrected_text": corrected_text,
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
) -> dict[str, Any]:
    document_profile = profile or get_profile()
    chunks = split_text_chunks(text, max_chars=max_chars)
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
) -> dict[str, Any]:
    document_profile = profile or get_profile()
    if repair_rounds < 1 or acceptance.get("accepted") is True:
        return {"status": "skipped", "repaired": False, "summary": "无需返修"}
    chunks = split_text_chunks(text, max_chars=max_chars)
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
            result = future.result()
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
            result = future.result()
            result["chunk_index"] = chunk.index
            result["chunk_total"] = chunk.total
            result["input_chars"] = len(chunk.text)
            results[chunk.index - 1] = result
            if chunk_report_dir is not None:
                _write_json(chunk_report_dir / f"{report_prefix}_{chunk.index:03d}.json", result)
    return [result for result in results if result is not None]


def _cached_repair_result(path: Path | None, chunk: TextChunk, *, expected_profile_id: str = "") -> dict[str, Any] | None:
    if path is None or not path.exists() or path.stat().st_size == 0:
        return None
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
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
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if expected_profile_id and result.get("profile_id") != expected_profile_id:
        return None
    content_hash = _sha256(chunk.text)
    if report_prefix.startswith("correction"):
        if result.get("input_sha256") != content_hash or result.get("draft_ready") is not True:
            return None
    elif report_prefix.startswith("acceptance"):
        if result.get("content_sha256") != content_hash or result.get("accepted") is not True:
            return None
    result["chunk_index"] = chunk.index
    result["chunk_total"] = chunk.total
    result["input_chars"] = len(chunk.text)
    return result


def _chunk_source_name(source_name: str, chunk: TextChunk) -> str:
    return f"{source_name} 第 {chunk.index}/{chunk.total} 片段"


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
