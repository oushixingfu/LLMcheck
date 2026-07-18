from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import time

from llmcheck.pipeline import FINAL_MARKDOWN_DIR_NAME, PROCESS_DIR_NAME, TEXT_PDF_DIR_NAME
from llmcheck.cleaning import safe_stem


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


def inspect_output(book_output_dir: Path, *, book_name: str = "") -> dict[str, Any]:
    output_dir = book_output_dir.expanduser()
    process_dir = output_dir / PROCESS_DIR_NAME
    reports_dir = _preferred_existing_dir(process_dir / "reports", output_dir / "reports")
    summary = _read_json(reports_dir / "llmcheck_summary.json")
    manifest_rows = _read_jsonl(reports_dir / "llmcheck_manifest.jsonl")
    heartbeat = _read_json(process_dir / "heartbeat.json")
    cost_report = _read_json(process_dir / "cost_report.json")
    stage_timings = _read_json(process_dir / "stage_timings.json")
    run_lock = _read_json(process_dir / "llmcheck_run.lock")
    llm_calls = _read_jsonl(process_dir / "llm_calls.jsonl")
    call_status_counts: dict[str, int] = {}
    call_stage_counts: dict[str, int] = {}
    latest_error = ""
    for row in llm_calls:
        status = str(row.get("status") or "unknown")
        stage = str(row.get("stage") or "unknown")
        call_status_counts[status] = call_status_counts.get(status, 0) + 1
        call_stage_counts[stage] = call_stage_counts.get(stage, 0) + 1
        if row.get("error"):
            latest_error = str(row.get("error") or "")
    return {
        "book_output_dir": str(output_dir),
        "book_name": book_name or _infer_book_name(output_dir),
        "summary_status": summary.get("status") if isinstance(summary, dict) else "",
        "document_count": len(manifest_rows) or (int(summary.get("document_count") or 0) if isinstance(summary, dict) else 0),
        "documents": [
            {
                "document_id": row.get("document_id"),
                "status": row.get("status"),
                "error": row.get("error"),
                "final_markdown_path": row.get("final_markdown_path"),
                "text_pdf_path": row.get("text_pdf_path"),
            }
            for row in manifest_rows
            if isinstance(row, dict)
        ],
        "heartbeat": heartbeat,
        "run_lock": run_lock,
        "cost_report": cost_report,
        "stage_timings": stage_timings,
        "llm_calls": {
            "count": len(llm_calls),
            "status_counts": call_status_counts,
            "stage_counts": call_stage_counts,
            "latest_error": latest_error,
        },
        "artifacts": {
            "final_markdown_count": _nonempty_count(output_dir / FINAL_MARKDOWN_DIR_NAME, "*.md"),
            "text_pdf_count": _nonempty_count(output_dir / TEXT_PDF_DIR_NAME, "*.pdf"),
            "draft_count": _nonempty_count(process_dir / "drafts", "*.md"),
            "cleaned_count": _nonempty_count(process_dir / "clean", "*.cleaned.md"),
            "review_report_count": _nonempty_count(reports_dir, "*.llm_review.json"),
            "artifact_binding_report_count": _nonempty_count(reports_dir, "*.artifact_binding.json"),
        },
    }


def cost_summary_report(book_output_dir: Path, *, book_name: str = "") -> dict[str, Any]:
    output_dir = book_output_dir.expanduser()
    process_dir = output_dir / PROCESS_DIR_NAME
    reports_dir = _preferred_existing_dir(process_dir / "reports", output_dir / "reports")
    cost_report = _read_json(process_dir / "cost_report.json")
    llm_calls = _read_jsonl(process_dir / "llm_calls.jsonl")
    if not llm_calls:
        for path in sorted(reports_dir.glob("*.llm_calls.jsonl")):
            llm_calls.extend(_read_jsonl(path))
    status_counts: dict[str, int] = {}
    stage_counts: dict[str, int] = {}
    model_counts: dict[str, int] = {}
    fallback_call_count = 0
    duration_seconds = 0.0
    estimated_cost_total = 0.0
    has_estimated_cost = False
    latest_error = ""
    primary_model = str(cost_report.get("model") or "") if isinstance(cost_report, dict) else ""
    for row in llm_calls:
        status = str(row.get("status") or "unknown")
        stage = str(row.get("stage") or "unknown")
        model = str(row.get("model") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        model_counts[model] = model_counts.get(model, 0) + 1
        if primary_model and model not in ("", "unknown", primary_model):
            fallback_call_count += 1
        try:
            duration_seconds += float(row.get("duration_seconds") or 0.0)
        except (TypeError, ValueError):
            pass
        value = row.get("estimated_cost")
        if value is not None:
            try:
                estimated_cost_total += float(value)
                has_estimated_cost = True
            except (TypeError, ValueError):
                pass
        if row.get("error"):
            latest_error = str(row.get("error") or "")
    return {
        "book_output_dir": str(output_dir),
        "book_name": book_name or _infer_book_name(output_dir),
        "model": primary_model,
        "fallback_allowed": cost_report.get("fallback_allowed", False) if isinstance(cost_report, dict) else False,
        "fallback_models": cost_report.get("fallback_models", []) if isinstance(cost_report, dict) else [],
        "llm_call_count": len(llm_calls) or int(cost_report.get("llm_call_count") or 0) if isinstance(cost_report, dict) else len(llm_calls),
        "llm_max_calls_per_book": cost_report.get("llm_max_calls_per_book", 0) if isinstance(cost_report, dict) else 0,
        "estimated_cost": round(estimated_cost_total, 6) if has_estimated_cost else cost_report.get("estimated_cost") if isinstance(cost_report, dict) else None,
        "currency": cost_report.get("currency", "unknown") if isinstance(cost_report, dict) else "unknown",
        "pricing_available": cost_report.get("pricing_available", False) if isinstance(cost_report, dict) else False,
        "duration_seconds": round(duration_seconds, 3),
        "status_counts": status_counts,
        "stage_counts": stage_counts,
        "model_counts": model_counts,
        "fallback_call_count": fallback_call_count,
        "latest_error": latest_error,
    }


def quality_report(book_output_dir: Path, *, book_name: str = "") -> dict[str, Any]:
    output_dir = book_output_dir.expanduser()
    process_dir = output_dir / PROCESS_DIR_NAME
    reports_dir = _preferred_existing_dir(process_dir / "reports", output_dir / "reports")
    clean_dir = _preferred_existing_dir(process_dir / "clean", output_dir / "clean")
    manifest_rows = _read_jsonl(reports_dir / "llmcheck_manifest.jsonl")
    quality_paths = sorted(reports_dir.glob("*.quality.json"))
    final_paths = sorted(reports_dir.glob("*.final_acceptance.json"))
    cleaning_paths = sorted(clean_dir.glob("*.cleaning_report.json"))
    final_by_document = {path.name.removesuffix(".final_acceptance.json"): _read_json(path) for path in final_paths}
    cleaning_by_document = {path.name.removesuffix(".cleaning_report.json"): _read_json(path) for path in cleaning_paths}
    manifest_by_document = {str(row.get("document_id") or ""): row for row in manifest_rows if isinstance(row, dict)}
    documents: list[dict[str, Any]] = []
    error_counts: dict[str, int] = {}
    hint_counts: dict[str, int] = {}
    rule_change_counts: dict[str, int] = {}
    final_status_counts: dict[str, int] = {}

    for path in quality_paths:
        document_id = path.name.removesuffix(".quality.json")
        payload = _read_json(path)
        errors = [str(item) for item in payload.get("errors", []) if str(item)] if isinstance(payload.get("errors"), list) else []
        hints = [str(item) for item in payload.get("hints", []) if str(item)] if isinstance(payload.get("hints"), list) else []
        for error in errors:
            error_counts[error] = error_counts.get(error, 0) + 1
        for hint in hints:
            hint_counts[hint] = hint_counts.get(hint, 0) + 1
        cleaning = cleaning_by_document.get(document_id, {})
        rule_changes = cleaning.get("rule_changes", []) if isinstance(cleaning, dict) else []
        if isinstance(rule_changes, list):
            for change in rule_changes:
                if isinstance(change, dict):
                    rule_id = str(change.get("rule_id") or change.get("rule") or "unknown")
                else:
                    rule_id = str(change or "unknown")
                rule_change_counts[rule_id] = rule_change_counts.get(rule_id, 0) + 1
        final = final_by_document.get(document_id, {})
        final_status = "accepted" if final.get("accepted") is True else "needs_revision" if final else "missing"
        final_status_counts[final_status] = final_status_counts.get(final_status, 0) + 1
        manifest = manifest_by_document.get(document_id, {})
        documents.append(
            {
                "document_id": document_id,
                "document_status": manifest.get("status", ""),
                "profile_id": payload.get("profile_id", ""),
                "error_count": len(errors),
                "hint_count": len(hints),
                "errors": errors[:20],
                "hints": hints[:20],
                "rule_change_count": len(rule_changes) if isinstance(rule_changes, list) else 0,
                "final_acceptance_status": final_status,
                "blocking_error_count": len(final.get("blocking_errors", [])) if isinstance(final.get("blocking_errors"), list) else 0,
                "quality_report_path": str(path),
                "final_acceptance_report_path": str(reports_dir / f"{document_id}.final_acceptance.json") if final else "",
                "cleaning_report_path": str(clean_dir / f"{document_id}.cleaning_report.json") if cleaning else "",
            }
        )
    return {
        "book_output_dir": str(output_dir),
        "book_name": book_name or _infer_book_name(output_dir),
        "document_count": len(documents),
        "quality_report_count": len(quality_paths),
        "final_acceptance_report_count": len(final_paths),
        "cleaning_report_count": len(cleaning_paths),
        "error_counts": error_counts,
        "hint_counts": hint_counts,
        "rule_change_counts": rule_change_counts,
        "final_acceptance_status_counts": final_status_counts,
        "documents": documents,
    }


def review_report(book_output_dir: Path, *, book_name: str = "") -> dict[str, Any]:
    output_dir = book_output_dir.expanduser()
    process_dir = output_dir / PROCESS_DIR_NAME
    reports_dir = _preferred_existing_dir(process_dir / "reports", output_dir / "reports")
    manifest_rows = _read_jsonl(reports_dir / "llmcheck_manifest.jsonl")
    review_paths = sorted(reports_dir.glob("*.llm_review.json"))
    safe_patch_paths = sorted(reports_dir.glob("*.safe_patch.json"))
    safe_patch_by_document = {
        path.name.removesuffix(".safe_patch.json"): _read_json(path)
        for path in safe_patch_paths
    }
    documents: list[dict[str, Any]] = []
    issue_counts: dict[str, int] = {}
    severity_counts: dict[str, int] = {}
    safe_fix_counts: dict[str, int] = {}
    manual_review_notes: list[str] = []

    manifest_by_document = {
        str(row.get("document_id") or ""): row
        for row in manifest_rows
        if isinstance(row, dict)
    }
    for path in review_paths:
        document_id = path.name.removesuffix(".llm_review.json")
        payload = _read_json(path)
        issues = [issue for issue in payload.get("issues", []) if isinstance(issue, dict)]
        notes = [note for note in payload.get("manual_review_notes", []) if isinstance(note, str) and note.strip()]
        for issue in issues:
            category = str(issue.get("category") or "unknown")
            severity = str(issue.get("severity") or "unknown")
            safe_fix_type = str(issue.get("safe_fix_type") or "unknown")
            issue_counts[category] = issue_counts.get(category, 0) + 1
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
            safe_fix_counts[safe_fix_type] = safe_fix_counts.get(safe_fix_type, 0) + 1
        manual_review_notes.extend(notes)
        patch = safe_patch_by_document.get(document_id, {})
        manifest = manifest_by_document.get(document_id, {})
        documents.append(
            {
                "document_id": document_id,
                "document_status": manifest.get("status", ""),
                "review_status": payload.get("status", ""),
                "accepted": payload.get("accepted") is True,
                "issue_count": len(issues),
                "manual_review_note_count": len(notes),
                "safe_patch_status": patch.get("status", ""),
                "applied_patch_count": patch.get("applied_patch_count", 0),
                "skipped_patch_count": patch.get("skipped_patch_count", 0),
                "unresolved_issue_count": len(patch.get("unresolved_issues", [])) if isinstance(patch.get("unresolved_issues"), list) else 0,
                "review_report_path": str(path),
                "safe_patch_report_path": str(reports_dir / f"{document_id}.safe_patch.json") if patch else "",
                "top_issues": [_compact_issue(issue) for issue in issues[:10]],
            }
        )
    return {
        "book_output_dir": str(output_dir),
        "book_name": book_name or _infer_book_name(output_dir),
        "document_count": len(documents),
        "review_report_count": len(review_paths),
        "safe_patch_report_count": len(safe_patch_paths),
        "issue_counts": issue_counts,
        "severity_counts": severity_counts,
        "safe_fix_counts": safe_fix_counts,
        "manual_review_note_count": len(manual_review_notes),
        "manual_review_notes": manual_review_notes[:50],
        "documents": documents,
    }


def _compact_issue(issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": issue.get("id", ""),
        "category": issue.get("category", ""),
        "severity": issue.get("severity", ""),
        "safe_fix_type": issue.get("safe_fix_type", ""),
        "location_hint": issue.get("location_hint", ""),
        "excerpt": str(issue.get("excerpt") or "")[:160],
        "reason": str(issue.get("reason") or "")[:240],
    }

def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows
