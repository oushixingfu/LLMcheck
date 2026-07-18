from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable, NamedTuple, Sequence
import hashlib
import json
import os
import re
import shutil

from llmcheck.pipeline import FINAL_MARKDOWN_DIR_NAME, PROCESS_DIR_NAME, TEXT_PDF_DIR_NAME, LlmCheckSettings, process_documents
from llmcheck.preprocess import SUPPORTED_SUFFIXES
from llmcheck.cleaning import safe_stem
from llmcheck.run_guard import acquire_run_lock


GENERATED_NAME_MARKERS = (
    "__myocr_final",
    "__myocr_text",
    "__llmcheck_final",
    "__llmcheck_text",
)
DELIVERY_MANIFEST_NAME = "llmcheck_delivery_manifest.jsonl"


@dataclass(frozen=True)
class BatchItem:
    index: int
    total: int
    source_path: str
    output_dir: str
    status: str
    started_at: str
    finished_at: str
    result_status: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ProcessRunner = Callable[..., dict[str, Any]]
ProgressCallback = Callable[[dict[str, Any]], None]
LlmPreflightRunner = Callable[[LlmCheckSettings], dict[str, Any]]


class SourceSelection(NamedTuple):
    index: int
    source: Path


def run_batch(
    *,
    source_dir: Path,
    output_dir: Path,
    settings: LlmCheckSettings,
    final_md_dir: Path | None = None,
    process_dir: Path | None = None,
    book_concurrency: int = 1,
    start_index: int = 1,
    limit: int = 0,
    force: bool = False,
    skip_existing: bool = True,
    dry_run: bool = False,
    preflight_only: bool = False,
    llm_preflight: LlmPreflightRunner | None = None,
    stop_on_global_service_error: bool = True,
    runner: ProcessRunner | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    actual_process_dir = process_dir or output_dir / PROCESS_DIR_NAME
    actual_final_md_dir = final_md_dir or output_dir / FINAL_MARKDOWN_DIR_NAME
    actual_pdf_dir = output_dir / TEXT_PDF_DIR_NAME
    actual_process_dir.mkdir(parents=True, exist_ok=True)
    batch_run_id = f"batch-{datetime.now().strftime('%Y%m%d%H%M%S')}-{os.getpid()}"
    run_lock = acquire_run_lock(actual_process_dir, run_id=batch_run_id)
    try:
        run_lock.heartbeat(status="running", stage="discover")
        all_sources = discover_batch_sources(
            source_dir=source_dir,
            output_dir=output_dir,
            excluded_dirs=[actual_process_dir, actual_final_md_dir, actual_pdf_dir],
        )
        all_items = [SourceSelection(index=index, source=source) for index, source in enumerate(all_sources, start=1)]
        selected_items = all_items[max(0, start_index - 1) :]
        if limit > 0:
            selected_items = selected_items[:limit]
        summary_max_index = selected_items[-1].index if selected_items else 0
        state_path = actual_process_dir / "llmcheck_batch_state.jsonl"
        actual_runner = runner or process_documents
        rows: list[BatchItem | None] = [None] * len(selected_items)
        valid_sources = {str(source.resolve()) for source in all_sources}
        state_lock = Lock()
        llm_preflight_report = llm_preflight(settings) if llm_preflight is not None and (preflight_only or not dry_run) else None

        if not selected_items:
            summary = _write_batch_summary(
                source_dir=source_dir,
                output_dir=output_dir,
                process_dir=actual_process_dir,
                final_md_dir=actual_final_md_dir,
                discovered_total=len(all_sources),
                start_index=start_index,
                limit=limit,
                selected_total=0,
                state_path=state_path,
                summary_rows=[],
            )
            run_lock.release(status=str(summary.get("status") or "finished"))
            return summary

        if dry_run or preflight_only:
            dry_rows = [
                _item(
                    index=item.index,
                    total=len(all_sources),
                    source=item.source,
                    output_dir=_book_output_dir(output_dir=output_dir, process_dir=actual_process_dir, index=item.index, source=item.source),
                    status="skipped",
                    started=datetime.now().isoformat(timespec="seconds"),
                    result_status="already_delivered"
                    if skip_existing
                    and _final_markdown_already_delivered(
                        final_md_dir=actual_final_md_dir,
                        process_dir=actual_process_dir,
                        index=item.index,
                        source=item.source,
                    )
                    else "would_process",
                )
                for item in selected_items
            ]
            summary = _write_batch_summary(
                source_dir=source_dir,
                output_dir=output_dir,
                process_dir=actual_process_dir,
                final_md_dir=actual_final_md_dir,
                discovered_total=len(all_sources),
                start_index=start_index,
                limit=limit,
                selected_total=len(dry_rows),
                state_path=state_path,
                summary_rows=dry_rows,
                status_override="preflight_only" if preflight_only else "dry_run",
                llm_preflight_report=llm_preflight_report,
            )
            run_lock.release(status=str(summary.get("status") or "finished"))
            return summary

        if _llm_preflight_blocks(llm_preflight_report):
            summary = _write_batch_summary(
                source_dir=source_dir,
                output_dir=output_dir,
                process_dir=actual_process_dir,
                final_md_dir=actual_final_md_dir,
                discovered_total=len(all_sources),
                start_index=start_index,
                limit=limit,
                selected_total=len(selected_items),
                state_path=state_path,
                summary_rows=[],
                status_override="blocked_needs_llm_service",
                error_override=str(llm_preflight_report.get("error") or "all_review_models_unavailable") if isinstance(llm_preflight_report, dict) else "all_review_models_unavailable",
                llm_preflight_report=llm_preflight_report,
            )
            run_lock.release(status=str(summary.get("status") or "finished"))
            return summary

        def record_progress(event: dict[str, Any]) -> None:
            if event.get("event") == "book_started":
                row = BatchItem(
                    index=int(event.get("index") or 0),
                    total=int(event.get("total") or len(all_sources)),
                    source_path=str(event.get("source_path") or ""),
                    output_dir=str(event.get("output_dir") or ""),
                    status="in_progress",
                    started_at=str(event.get("started_at") or datetime.now().isoformat(timespec="seconds")),
                    finished_at="",
                )
                with state_lock:
                    _append_jsonl(state_path, row.to_dict())
                    _write_batch_summary(
                        source_dir=source_dir,
                        output_dir=output_dir,
                        process_dir=actual_process_dir,
                        final_md_dir=actual_final_md_dir,
                        discovered_total=len(all_sources),
                        start_index=start_index,
                        limit=limit,
                        selected_total=len(selected_items),
                        state_path=state_path,
                        summary_rows=_latest_state_rows(state_path, valid_sources=valid_sources, max_index=summary_max_index),
                    )
            elif event.get("event") == "book_finished":
                try:
                    row = BatchItem(
                        index=int(event.get("index") or 0),
                        total=int(event.get("total") or len(all_sources)),
                        source_path=str(event.get("source_path") or ""),
                        output_dir=str(event.get("output_dir") or ""),
                        status=str(event.get("status") or ""),
                        started_at=str(event.get("started_at") or ""),
                        finished_at=str(event.get("finished_at") or datetime.now().isoformat(timespec="seconds")),
                        result_status=str(event.get("result_status") or ""),
                        error=str(event.get("error") or ""),
                    )
                except (TypeError, ValueError):
                    row = None
                if row is not None:
                    with state_lock:
                        _append_jsonl(state_path, row.to_dict())
                        _write_batch_summary(
                            source_dir=source_dir,
                            output_dir=output_dir,
                            process_dir=actual_process_dir,
                            final_md_dir=actual_final_md_dir,
                            discovered_total=len(all_sources),
                            start_index=start_index,
                            limit=limit,
                            selected_total=len(selected_items),
                            state_path=state_path,
                            summary_rows=_latest_state_rows(state_path, valid_sources=valid_sources, max_index=summary_max_index),
                        )
            _emit_progress(progress_callback, event)

        _emit_progress(
            progress_callback,
            {
                "event": "batch_started",
                "source_dir": str(source_dir.resolve()),
                "output_dir": str(output_dir.resolve()),
                "discovered_total": len(all_sources),
                "selected_total": len(selected_items),
                "start_index": max(1, start_index),
                "limit": max(0, limit),
            },
        )
        run_lock.heartbeat(status="running", stage="books")
        global_error = ""
        if stop_on_global_service_error:
            for position, item in enumerate(selected_items):
                row = _run_one(
                    index=item.index,
                    total=len(all_sources),
                    source=item.source,
                    output_dir=output_dir,
                    process_dir=actual_process_dir,
                    final_md_dir=actual_final_md_dir,
                    pdf_dir=actual_pdf_dir,
                    settings=settings,
                    force=force,
                    skip_existing=skip_existing,
                    runner=actual_runner,
                    progress_callback=record_progress,
                )
                rows[position] = row
                global_error = _global_service_error(row)
                if global_error:
                    break
        else:
            with ThreadPoolExecutor(max_workers=max(1, book_concurrency)) as executor:
                futures = {
                    executor.submit(
                        _run_one,
                        index=item.index,
                        total=len(all_sources),
                        source=item.source,
                        output_dir=output_dir,
                        process_dir=actual_process_dir,
                        final_md_dir=actual_final_md_dir,
                        pdf_dir=actual_pdf_dir,
                        settings=settings,
                        force=force,
                        skip_existing=skip_existing,
                        runner=actual_runner,
                        progress_callback=record_progress,
                    ): position
                    for position, item in enumerate(selected_items)
                }
                for future in as_completed(futures):
                    row = future.result()
                    rows[futures[future]] = row
        ordered = [row for row in rows if row is not None]
        summary_rows = _latest_state_rows(state_path, valid_sources=valid_sources, max_index=summary_max_index)
        if not summary_rows:
            summary_rows = ordered
        summary = _write_batch_summary(
            source_dir=source_dir,
            output_dir=output_dir,
            process_dir=actual_process_dir,
            final_md_dir=actual_final_md_dir,
            discovered_total=len(all_sources),
            start_index=start_index,
            limit=limit,
            selected_total=len(ordered),
            state_path=state_path,
            summary_rows=summary_rows,
            status_override="blocked_needs_llm_service" if global_error else "",
            error_override=global_error,
            llm_preflight_report=llm_preflight_report,
        )
        run_lock.release(status=str(summary.get("status") or "finished"))
        return summary
    except Exception:
        run_lock.release(status="failed")
        raise

def discover_batch_sources(*, source_dir: Path, output_dir: Path, excluded_dirs: Sequence[Path] | None = None) -> list[Path]:
    root = source_dir.expanduser().resolve()
    output = output_dir.expanduser().resolve()
    excluded_roots = [path.expanduser().resolve() for path in (excluded_dirs or [output])]
    if any(_is_relative_to(root, excluded) for excluded in excluded_roots):
        return []
    rows: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath).resolve()
        dirnames[:] = sorted(
            dirname
            for dirname in dirnames
            if not _is_relative_to((current / dirname).resolve(), output)
        )
        for filename in sorted(filenames, key=_filename_sort_key):
            path = current / filename
            if not _is_supported_source(path, excluded_dirs=excluded_roots):
                continue
            rows.append(path.resolve())
    return sorted(rows, key=_source_sort_key)


def _filename_sort_key(value: str) -> tuple[int, int, str]:
    match = re.match(r"^(\d+)", value)
    if match:
        return (0, int(match.group(1)), value.casefold())
    return (1, 0, value.casefold())


def _source_sort_key(path: Path) -> tuple[int, int, str]:
    return _filename_sort_key(path.name)


def _is_supported_source(path: Path, *, excluded_dirs: Sequence[Path]) -> bool:
    resolved = path.resolve()
    if any(_is_relative_to(resolved, excluded) for excluded in excluded_dirs):
        return False
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        return False
    if any(marker in path.stem for marker in GENERATED_NAME_MARKERS):
        return False
    return True


def _is_successful_row(row: BatchItem) -> bool:
    return row.status == "passed" or (row.status == "skipped" and row.result_status in {"passed", "already_delivered"})


def _global_service_error(row: BatchItem) -> str:
    haystack = f"{row.result_status}\n{row.error}".lower()
    if "all_review_models_unavailable" in haystack:
        return "all_review_models_unavailable"
    if "blocked_needs_llm_service" in haystack:
        return "blocked_needs_llm_service"
    return ""


def _llm_preflight_blocks(report: dict[str, Any] | None) -> bool:
    return isinstance(report, dict) and str(report.get("status") or "") == "blocked_needs_llm_service"


def _book_output_dir(*, output_dir: Path, process_dir: Path, index: int, source: Path) -> Path:
    canonical = process_dir / "books" / f"{index:04d}_{safe_stem(source.stem)}"
    if canonical.exists():
        return canonical
    legacy_matches = sorted(output_dir.glob(f"[0-9][0-9][0-9][0-9]_{safe_stem(source.stem)}"))
    return legacy_matches[0] if len(legacy_matches) == 1 else canonical


def _run_one(
    *,
    index: int,
    total: int,
    source: Path,
    output_dir: Path,
    process_dir: Path,
    final_md_dir: Path,
    pdf_dir: Path,
    settings: LlmCheckSettings,
    force: bool,
    skip_existing: bool,
    runner: ProcessRunner,
    progress_callback: ProgressCallback | None = None,
    ) -> BatchItem:
    started = datetime.now().isoformat(timespec="seconds")
    book_output = _book_output_dir(output_dir=output_dir, process_dir=process_dir, index=index, source=source)
    _emit_progress(
        progress_callback,
        {
            "event": "book_started",
            "index": index,
            "total": total,
            "source_path": str(source),
            "book_name": source.name,
            "output_dir": str(book_output),
            "started_at": started,
        },
    )
    summary_path = _book_summary_path(book_output)
    if skip_existing and not force and _final_markdown_already_delivered(final_md_dir=final_md_dir, process_dir=process_dir, index=index, source=source):
        row = _item(
            index=index,
            total=total,
            source=source,
            output_dir=book_output,
            status="skipped",
            started=started,
            result_status="already_delivered",
        )
        _emit_progress(progress_callback, {"event": "book_finished", **row.to_dict()})
        return row
    if skip_existing and not force and summary_path.exists():
        try:
            existing = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        if _existing_passed_is_valid(existing, source=source, book_output=book_output):
            _publish_delivery_artifacts(
                result=existing,
                final_md_dir=final_md_dir,
                pdf_dir=pdf_dir,
                process_dir=process_dir,
                book_output=book_output,
                index=index,
                source=source,
            )
            row = _item(index=index, total=total, source=source, output_dir=book_output, status="skipped", started=started, result_status="passed")
            _emit_progress(progress_callback, {"event": "book_finished", **row.to_dict()})
            return row
    try:
        result = runner(input_path=source, output_dir=book_output, settings=settings)
        status = "passed" if result.get("status") == "passed" else "failed"
        if status == "passed":
            _publish_delivery_artifacts(
                result=result,
                final_md_dir=final_md_dir,
                pdf_dir=pdf_dir,
                process_dir=process_dir,
                book_output=book_output,
                index=index,
                source=source,
            )
        row = _item(
            index=index,
            total=total,
            source=source,
            output_dir=book_output,
            status=status,
            started=started,
            result_status=str(result.get("status") or ""),
            error="" if status == "passed" else json.dumps(result, ensure_ascii=False)[-1000:],
        )
        _emit_progress(progress_callback, {"event": "book_finished", **row.to_dict()})
        return row
    except Exception as error:  # noqa: BLE001 - batch should report the failing book and continue.
        row = _item(index=index, total=total, source=source, output_dir=book_output, status="failed", started=started, error=str(error))
        _emit_progress(progress_callback, {"event": "book_finished", **row.to_dict()})
        return row


def _final_markdown_already_delivered(*, final_md_dir: Path, process_dir: Path, index: int, source: Path) -> bool:
    markdown_dir = final_md_dir
    source_stem = safe_stem(source.stem)
    candidates = (
        markdown_dir / f"{source_stem}.md",
        markdown_dir / f"{index:04d}_{source_stem}.md",
    )
    if any(path.exists() and path.is_file() and path.stat().st_size > 0 for path in candidates):
        return True
    return _delivery_manifest_has_accepted_source(process_dir=process_dir, source=source)


def _delivery_manifest_has_accepted_source(*, process_dir: Path, source: Path) -> bool:
    manifest_path = _delivery_manifest_path(process_dir)
    if not manifest_path.exists():
        return False
    try:
        source_hash = _sha256_bytes(source.read_bytes())
    except OSError:
        return False
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("accepted") is not True:
            continue
        if str(row.get("source_sha256") or "") != source_hash:
            continue
        final_path = Path(str(row.get("final_markdown_path") or ""))
        if not final_path.exists() or not final_path.is_file() or final_path.stat().st_size <= 0:
            continue
        expected_final_hash = str(row.get("final_markdown_sha256") or "")
        if expected_final_hash:
            try:
                if _sha256_bytes(final_path.read_bytes()) != expected_final_hash:
                    continue
            except OSError:
                continue
        return True
    return False


def _existing_passed_is_valid(summary: dict[str, Any], *, source: Path, book_output: Path) -> bool:
    if summary.get("status") != "passed":
        return False
    try:
        if Path(str(summary.get("input_path") or "")).resolve() != source.resolve():
            return False
    except (OSError, RuntimeError):
        return False
    documents = summary.get("documents")
    if not isinstance(documents, list) or not documents:
        return False
    for document in documents:
        if not isinstance(document, dict) or document.get("status") != "passed":
            return False
        final_path = _summary_artifact_path(document.get("final_markdown_path"), book_output=book_output)
        pdf_path = _summary_artifact_path(document.get("text_pdf_path") or document.get("pdf_path"), book_output=book_output)
        if final_path is None or pdf_path is None:
            return False
        if not final_path.exists() or final_path.stat().st_size <= 0:
            return False
        if not pdf_path.exists() or pdf_path.stat().st_size <= 5:
            return False
        if pdf_path.read_bytes()[:5] != b"%PDF-":
            return False
        if not _document_artifact_binding_is_valid(document, final_path=final_path, pdf_path=pdf_path, book_output=book_output):
            return False
    return True


def _document_artifact_binding_is_valid(document: dict[str, Any], *, final_path: Path, pdf_path: Path, book_output: Path) -> bool:
    final_sha = _sha256_text(final_path.read_text(encoding="utf-8"))
    pdf_sha = _sha256_bytes(pdf_path.read_bytes())
    if str(document.get("artifact_binding_status") or "") != "passed":
        return False
    if str(document.get("final_markdown_sha256") or document.get("sha256") or "") != final_sha:
        return False
    if str(document.get("pdf_source_sha256") or "") != final_sha:
        return False
    if str(document.get("pdf_sha256") or "") != pdf_sha:
        return False
    binding_path = _summary_artifact_path(document.get("artifact_binding_report_path"), book_output=book_output)
    if binding_path is None or not binding_path.exists() or binding_path.stat().st_size <= 0:
        return False
    try:
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return (
        binding.get("accepted") is True
        and str(binding.get("final_markdown_sha256") or "") == final_sha
        and str(binding.get("pdf_source_text_sha256") or "") == final_sha
        and str(binding.get("pdf_binary_sha256") or "") == pdf_sha
    )


def _book_summary_path(book_output: Path) -> Path:
    summary_path = book_output / PROCESS_DIR_NAME / "reports" / "llmcheck_summary.json"
    if summary_path.exists():
        return summary_path
    legacy_path = book_output / "reports" / "llmcheck_summary.json"
    return legacy_path if legacy_path.exists() else summary_path


def _publish_delivery_artifacts(
    *,
    result: dict[str, Any],
    final_md_dir: Path,
    pdf_dir: Path,
    process_dir: Path,
    book_output: Path,
    index: int,
    source: Path,
) -> None:
    documents = result.get("documents")
    if not isinstance(documents, list):
        return
    markdown_dir = final_md_dir
    markdown_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    delivery_stem = safe_stem(source.stem)
    delivery_manifest_path = _delivery_manifest_path(process_dir)
    report_dir = _book_summary_path(book_output).parent
    try:
        source_path = source.resolve()
    except OSError:
        source_path = source
    source_sha = _sha256_bytes(source.read_bytes()) if source.exists() else ""
    for document in documents:
        if not isinstance(document, dict) or document.get("status") != "passed":
            continue
        final_path = Path(str(document.get("final_markdown_path") or ""))
        pdf_path = Path(str(document.get("text_pdf_path") or ""))
        delivery_md_path = markdown_dir / f"{delivery_stem}.md"
        delivery_pdf_path = pdf_dir / f"{delivery_stem}.pdf"
        if final_path.exists() and final_path.is_file():
            shutil.copy2(final_path, delivery_md_path)
        if pdf_path.exists() and pdf_path.is_file():
            shutil.copy2(pdf_path, delivery_pdf_path)
        if not delivery_md_path.exists() or delivery_md_path.stat().st_size <= 0:
            continue
        manifest = {
            "accepted": True,
            "status": "delivered",
            "acquisition_status": "delivered",
            "index": index,
            "document_id": str(document.get("document_id") or ""),
            "source_path": str(source_path),
            "source_sha256": source_sha,
            "book_output_dir": str(book_output.resolve()),
            "final_markdown_path": str(delivery_md_path.resolve()),
            "final_markdown_sha256": _sha256_bytes(delivery_md_path.read_bytes()),
            "text_pdf_path": str(delivery_pdf_path.resolve()) if delivery_pdf_path.exists() else "",
            "pdf_sha256": _sha256_bytes(delivery_pdf_path.read_bytes()) if delivery_pdf_path.exists() else "",
            "delivered_at": datetime.now().isoformat(timespec="seconds"),
        }
        _append_jsonl(delivery_manifest_path, manifest)
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / f"{delivery_stem}.delivery_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _summary_artifact_path(value: object, *, book_output: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return path if path.is_absolute() else book_output / path


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _item(
    *,
    index: int,
    total: int,
    source: Path,
    output_dir: Path,
    status: str,
    started: str,
    result_status: str = "",
    error: str = "",
) -> BatchItem:
    return BatchItem(
        index=index,
        total=total,
        source_path=str(source),
        output_dir=str(output_dir),
        status=status,
        started_at=started,
        finished_at=datetime.now().isoformat(timespec="seconds"),
        result_status=result_status,
        error=error,
    )


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_batch_summary(
    *,
    source_dir: Path,
    output_dir: Path,
    process_dir: Path,
    final_md_dir: Path,
    discovered_total: int,
    start_index: int,
    limit: int,
    selected_total: int,
    state_path: Path,
    summary_rows: list[BatchItem],
    status_override: str = "",
    error_override: str = "",
    llm_preflight_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    has_sources = bool(summary_rows)
    summary = {
        "status": status_override or ("passed" if has_sources and all(_is_successful_row(row) for row in summary_rows) else "review_required"),
        "source_dir": str(source_dir.resolve()),
        "output_dir": str(output_dir.resolve()),
        "process_dir": str(process_dir.resolve()),
        "final_md_dir": str(final_md_dir.resolve()),
        "discovered_total": discovered_total,
        "start_index": max(1, start_index),
        "limit": max(0, limit),
        "selected_total": selected_total,
        "total": len(summary_rows),
        "passed": sum(1 for row in summary_rows if _is_successful_row(row)),
        "failed": sum(1 for row in summary_rows if row.status == "failed"),
        "skipped": sum(1 for row in summary_rows if row.status == "skipped"),
        "in_progress": sum(1 for row in summary_rows if row.status == "in_progress"),
        "already_delivered": sum(1 for row in summary_rows if row.status == "skipped" and row.result_status == "already_delivered"),
        "error": "" if has_sources else "未发现可处理输入，或 start-index 超出书目范围",
        "state_path": str(state_path),
        "delivery_manifest_path": str(_delivery_manifest_path(process_dir)),
        "documents": [row.to_dict() for row in summary_rows],
    }
    if error_override:
        summary["error"] = error_override
    if llm_preflight_report is not None:
        summary["llm_preflight"] = llm_preflight_report
    summary_path = process_dir / "llmcheck_batch_summary.json"
    tmp_path = summary_path.with_name(f"{summary_path.name}.tmp")
    process_dir.mkdir(parents=True, exist_ok=True)
    tmp_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(summary_path)
    return summary


def _latest_state_rows(path: Path, *, valid_sources: set[str], max_index: int | None = None) -> list[BatchItem]:
    if not path.exists():
        return []
    latest: dict[str, BatchItem] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            row = BatchItem(**payload)
        except (TypeError, json.JSONDecodeError):
            continue
        try:
            source_key = str(Path(row.source_path).resolve())
        except (OSError, RuntimeError):
            source_key = row.source_path
        if source_key not in valid_sources:
            continue
        latest[source_key] = row
    all_rows = sorted(latest.values(), key=lambda row: row.index)
    rows = [row for row in all_rows if max_index is None or row.index <= max_index]
    # Auto-compact: if the jsonl has more than 2x the deduplicated row count, rewrite it.
    total_lines = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    if max_index is None and total_lines > len(all_rows) * 2:
        _compact_jsonl(path, all_rows)
    return rows


def _compact_jsonl(path: Path, rows: list[BatchItem]) -> None:
    """Rewrite the jsonl file keeping only the latest row per source."""
    tmp_path = path.with_name(f"{path.name}.compact")
    with tmp_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row.to_dict(), ensure_ascii=False) + "\n")
    tmp_path.replace(path)


def _delivery_manifest_path(process_dir: Path) -> Path:
    return process_dir / DELIVERY_MANIFEST_NAME


def _emit_progress(callback: ProgressCallback | None, event: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(event)
    except Exception:
        return


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
