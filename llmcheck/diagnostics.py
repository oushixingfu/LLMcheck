from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import time

from llmcheck.pipeline import FINAL_MARKDOWN_DIR_NAME, PROCESS_DIR_NAME, TEXT_PDF_DIR_NAME
from llmcheck.quality import safe_stem


def summarize_book_output(book_output_dir: Path, *, book_name: str = "") -> dict[str, Any]:
    output_dir = book_output_dir.expanduser()
    process_dir = output_dir / PROCESS_DIR_NAME
    reports_dir = _preferred_existing_dir(process_dir / "reports", output_dir / "reports")
    preprocess_dir = _preferred_existing_dir(process_dir / "preprocess", output_dir / "preprocess")
    mineru_segments = read_mineru_segment_status(output_dir, book_name=book_name)
    summary = _read_json(reports_dir / "llmcheck_summary.json")
    artifacts = {
        "llm_summary_size": _path_size(reports_dir / "llmcheck_summary.json"),
        "llm_correction_report_count": _nonempty_count(reports_dir, "*.llm_correction.json"),
        "llm_acceptance_report_count": _nonempty_count(reports_dir, "*.llm_acceptance.json"),
        "ppx_audit_size": _first_match_size(preprocess_dir, "*/ppx/clean/ppx.md"),
        "final_markdown_count": _nonempty_count(output_dir / FINAL_MARKDOWN_DIR_NAME, "*.md"),
        "text_pdf_count": _nonempty_count(output_dir / TEXT_PDF_DIR_NAME, "*.pdf"),
    }
    return {
        "book_output_dir": str(output_dir),
        "book_name": mineru_segments.get("book_name") or _infer_book_name(output_dir),
        "stage": _diagnose_stage(mineru_segments=mineru_segments, artifacts=artifacts, summary=summary),
        "mineru_segments": mineru_segments,
        "artifacts": artifacts,
        "summary_status": summary.get("status") if isinstance(summary, dict) else "",
    }


def read_mineru_segment_status(book_output_dir: Path, *, book_name: str = "") -> dict[str, Any]:
    mineru_dir = _find_mineru_dir(book_output_dir.expanduser(), book_name=book_name)
    status_files = sorted(mineru_dir.glob("segment_*/status.json")) if mineru_dir is not None else []
    status_counts: dict[str, int] = {}
    cloud_state_counts: dict[str, int] = {}
    newest_mtime = 0.0
    for status_path in status_files:
        payload = _read_json(status_path)
        status = str(payload.get("status") or payload.get("local_state") or payload.get("state") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        state_counts = payload.get("state_counts")
        if isinstance(state_counts, dict):
            for state, count in state_counts.items():
                if isinstance(count, int):
                    key = str(state)
                    cloud_state_counts[key] = cloud_state_counts.get(key, 0) + count
        try:
            newest_mtime = max(newest_mtime, status_path.stat().st_mtime)
        except OSError:
            pass
    markdown_path = mineru_dir / "mineru_vlm.md" if mineru_dir is not None else None
    return {
        "book_name": mineru_dir.parent.name if mineru_dir is not None else _infer_book_name(book_output_dir),
        "mineru_dir": str(mineru_dir) if mineru_dir is not None else "",
        "total": len(status_files),
        "status_counts": status_counts,
        "cloud_state_counts": cloud_state_counts,
        "mineru_markdown_size": _path_size(markdown_path),
        "updated_at_epoch": newest_mtime,
        "updated_age_seconds": max(0.0, time.time() - newest_mtime) if newest_mtime else None,
    }


def _diagnose_stage(*, mineru_segments: dict[str, Any], artifacts: dict[str, int], summary: dict[str, Any]) -> str:
    if artifacts["final_markdown_count"] and artifacts["text_pdf_count"]:
        return "final_ready"
    if artifacts["llm_summary_size"]:
        return "llm_finished"
    if int(mineru_segments.get("mineru_markdown_size") or 0) > 0:
        return "llm_ready"
    status_counts = mineru_segments.get("status_counts")
    cloud_state_counts = mineru_segments.get("cloud_state_counts")
    if isinstance(cloud_state_counts, dict) and cloud_state_counts.get("pending"):
        return "mineru_pending"
    if isinstance(status_counts, dict) and any(status in status_counts for status in ("polling", "creating_task", "uploading")):
        return "mineru_pending"
    if int(mineru_segments.get("total") or 0) > 0:
        return "mineru_partial"
    if summary.get("status"):
        return "llm_finished"
    return "not_started"


def _find_mineru_dir(book_output_dir: Path, *, book_name: str) -> Path | None:
    preprocess_dir = _preferred_existing_dir(book_output_dir / PROCESS_DIR_NAME / "preprocess", book_output_dir / "preprocess")
    if book_name:
        candidate = preprocess_dir / safe_stem(Path(book_name).stem) / "mineru"
        if candidate.exists():
            return candidate
    candidates = sorted(preprocess_dir.glob("*/mineru"))
    return candidates[0] if candidates else None


def _preferred_existing_dir(primary: Path, legacy: Path) -> Path:
    return primary if primary.exists() or not legacy.exists() else legacy


def _infer_book_name(book_output_dir: Path) -> str:
    name = book_output_dir.name
    return name.split("_", 1)[1] if "_" in name else name


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _path_size(path: Path | None) -> int:
    if path is None:
        return 0
    try:
        return path.stat().st_size if path.exists() else 0
    except OSError:
        return 0


def _nonempty_count(directory: Path, pattern: str) -> int:
    try:
        return sum(1 for path in directory.glob(pattern) if path.is_file() and path.stat().st_size > 0)
    except OSError:
        return 0


def _first_match_size(directory: Path, pattern: str) -> int:
    try:
        for path in sorted(directory.glob(pattern)):
            size = _path_size(path)
            if size > 0:
                return size
    except OSError:
        return 0
    return 0
