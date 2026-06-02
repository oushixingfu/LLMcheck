from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable, NamedTuple
import json
import os
import shutil

from llmcheck.pipeline import FINAL_MARKDOWN_DIR_NAME, PROCESS_DIR_NAME, TEXT_PDF_DIR_NAME, LlmCheckSettings, process_documents
from llmcheck.preprocess import SUPPORTED_SUFFIXES
from llmcheck.quality import safe_stem


GENERATED_NAME_MARKERS = (
    "__myocr_final",
    "__myocr_text",
    "__llmcheck_final",
    "__llmcheck_text",
)


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


class SourceSelection(NamedTuple):
    index: int
    source: Path


def run_batch(
    *,
    source_dir: Path,
    output_dir: Path,
    settings: LlmCheckSettings,
    book_concurrency: int = 1,
    start_index: int = 1,
    limit: int = 0,
    force: bool = False,
    runner: ProcessRunner | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    all_sources = discover_batch_sources(source_dir=source_dir, output_dir=output_dir)
    all_items = [SourceSelection(index=index, source=source) for index, source in enumerate(all_sources, start=1)]
    selected_items = all_items[max(0, start_index - 1) :]
    if limit > 0:
        selected_items = selected_items[:limit]
    output_dir.mkdir(parents=True, exist_ok=True)
    process_dir = output_dir / PROCESS_DIR_NAME
    process_dir.mkdir(parents=True, exist_ok=True)
    state_path = process_dir / "llmcheck_batch_state.jsonl"
    actual_runner = runner or process_documents
    rows: list[BatchItem | None] = [None] * len(selected_items)
    valid_sources = {str(source.resolve()) for source in all_sources}
    state_lock = Lock()

    if not selected_items:
        return _write_batch_summary(
            source_dir=source_dir,
            output_dir=output_dir,
            discovered_total=len(all_sources),
            start_index=start_index,
            limit=limit,
            selected_total=0,
            state_path=state_path,
            summary_rows=[],
        )

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
                    discovered_total=len(all_sources),
                    start_index=start_index,
                    limit=limit,
                    selected_total=len(selected_items),
                    state_path=state_path,
                    summary_rows=_latest_state_rows(state_path, valid_sources=valid_sources),
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
                        discovered_total=len(all_sources),
                        start_index=start_index,
                        limit=limit,
                        selected_total=len(selected_items),
                        state_path=state_path,
                        summary_rows=_latest_state_rows(state_path, valid_sources=valid_sources),
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
    with ThreadPoolExecutor(max_workers=max(1, book_concurrency)) as executor:
        futures = {
            executor.submit(
                _run_one,
                index=item.index,
                total=len(all_sources),
                source=item.source,
                output_dir=output_dir,
                settings=settings,
                force=force,
                runner=actual_runner,
                progress_callback=record_progress,
            ): position
            for position, item in enumerate(selected_items)
        }
        for future in as_completed(futures):
            row = future.result()
            rows[futures[future]] = row
    ordered = [row for row in rows if row is not None]
    summary_rows = _latest_state_rows(state_path, valid_sources=valid_sources)
    if not summary_rows:
        summary_rows = ordered
    return _write_batch_summary(
        source_dir=source_dir,
        output_dir=output_dir,
        discovered_total=len(all_sources),
        start_index=start_index,
        limit=limit,
        selected_total=len(ordered),
        state_path=state_path,
        summary_rows=summary_rows,
    )


def discover_batch_sources(*, source_dir: Path, output_dir: Path) -> list[Path]:
    root = source_dir.expanduser().resolve()
    output = output_dir.expanduser().resolve()
    if _is_relative_to(root, output):
        return []
    rows: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath).resolve()
        dirnames[:] = sorted(
            dirname
            for dirname in dirnames
            if not _is_relative_to((current / dirname).resolve(), output)
        )
        for filename in sorted(filenames):
            path = current / filename
            if not _is_supported_source(path, output=output):
                continue
            rows.append(path.resolve())
    return sorted(rows)


def _is_supported_source(path: Path, *, output: Path) -> bool:
    resolved = path.resolve()
    if _is_relative_to(resolved, output):
        return False
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        return False
    if any(marker in path.stem for marker in GENERATED_NAME_MARKERS):
        return False
    return True


def _is_successful_row(row: BatchItem) -> bool:
    return row.status == "passed" or (row.status == "skipped" and row.result_status == "passed")


def _book_output_dir(*, output_dir: Path, index: int, source: Path) -> Path:
    canonical = output_dir / PROCESS_DIR_NAME / "books" / f"{index:04d}_{safe_stem(source.stem)}"
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
    settings: LlmCheckSettings,
    force: bool,
    runner: ProcessRunner,
    progress_callback: ProgressCallback | None = None,
) -> BatchItem:
    started = datetime.now().isoformat(timespec="seconds")
    book_output = _book_output_dir(output_dir=output_dir, index=index, source=source)
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
    if not force and summary_path.exists():
        try:
            existing = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        if _existing_passed_is_valid(existing, source=source, book_output=book_output):
            _publish_delivery_artifacts(result=existing, root_output_dir=output_dir, index=index, source=source)
            row = _item(index=index, total=total, source=source, output_dir=book_output, status="skipped", started=started, result_status="passed")
            _emit_progress(progress_callback, {"event": "book_finished", **row.to_dict()})
            return row
    try:
        result = runner(input_path=source, output_dir=book_output, settings=settings)
        status = "passed" if result.get("status") == "passed" else "failed"
        if status == "passed":
            _publish_delivery_artifacts(result=result, root_output_dir=output_dir, index=index, source=source)
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
    return True


def _book_summary_path(book_output: Path) -> Path:
    summary_path = book_output / PROCESS_DIR_NAME / "reports" / "llmcheck_summary.json"
    if summary_path.exists():
        return summary_path
    legacy_path = book_output / "reports" / "llmcheck_summary.json"
    return legacy_path if legacy_path.exists() else summary_path


def _publish_delivery_artifacts(*, result: dict[str, Any], root_output_dir: Path, index: int, source: Path) -> None:
    documents = result.get("documents")
    if not isinstance(documents, list):
        return
    markdown_dir = root_output_dir / FINAL_MARKDOWN_DIR_NAME
    pdf_dir = root_output_dir / TEXT_PDF_DIR_NAME
    markdown_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    delivery_stem = f"{index:04d}_{safe_stem(source.stem)}"
    for document in documents:
        if not isinstance(document, dict) or document.get("status") != "passed":
            continue
        final_path = Path(str(document.get("final_markdown_path") or ""))
        pdf_path = Path(str(document.get("text_pdf_path") or ""))
        if final_path.exists() and final_path.is_file():
            shutil.copy2(final_path, markdown_dir / f"{delivery_stem}.md")
        if pdf_path.exists() and pdf_path.is_file():
            shutil.copy2(pdf_path, pdf_dir / f"{delivery_stem}.pdf")


def _summary_artifact_path(value: object, *, book_output: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return path if path.is_absolute() else book_output / path


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
    discovered_total: int,
    start_index: int,
    limit: int,
    selected_total: int,
    state_path: Path,
    summary_rows: list[BatchItem],
) -> dict[str, Any]:
    has_sources = bool(summary_rows)
    summary = {
        "status": "passed" if has_sources and all(_is_successful_row(row) for row in summary_rows) else "review_required",
        "source_dir": str(source_dir.resolve()),
        "output_dir": str(output_dir.resolve()),
        "discovered_total": discovered_total,
        "start_index": max(1, start_index),
        "limit": max(0, limit),
        "selected_total": selected_total,
        "total": len(summary_rows),
        "passed": sum(1 for row in summary_rows if _is_successful_row(row)),
        "failed": sum(1 for row in summary_rows if row.status == "failed"),
        "skipped": sum(1 for row in summary_rows if row.status == "skipped"),
        "in_progress": sum(1 for row in summary_rows if row.status == "in_progress"),
        "error": "" if has_sources else "未发现可处理输入，或 start-index 超出书目范围",
        "state_path": str(state_path),
        "documents": [row.to_dict() for row in summary_rows],
    }
    summary_path = output_dir / PROCESS_DIR_NAME / "llmcheck_batch_summary.json"
    tmp_path = summary_path.with_name(f"{summary_path.name}.tmp")
    output_dir.mkdir(parents=True, exist_ok=True)
    tmp_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(summary_path)
    return summary


def _latest_state_rows(path: Path, *, valid_sources: set[str]) -> list[BatchItem]:
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
    rows = sorted(latest.values(), key=lambda row: row.index)
    # Auto-compact: if the jsonl has more than 2x the deduplicated row count, rewrite it.
    total_lines = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    if total_lines > len(rows) * 2:
        _compact_jsonl(path, rows)
    return rows


def _compact_jsonl(path: Path, rows: list[BatchItem]) -> None:
    """Rewrite the jsonl file keeping only the latest row per source."""
    tmp_path = path.with_name(f"{path.name}.compact")
    with tmp_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row.to_dict(), ensure_ascii=False) + "\n")
    tmp_path.replace(path)


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
