from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from llmcheck.batch import run_batch
from llmcheck.pipeline import LlmCheckSettings, model_api_url_for_model
from llmcheck.cleaning import safe_stem


BatchRunner = Callable[..., dict[str, Any]]


def run_model_compare(
    *,
    source_dir: Path,
    output_dir: Path,
    settings: LlmCheckSettings,
    models: list[str],
    preferred_model: str,
    fallback_model: str,
    limit: int = 3,
    start_index: int = 1,
    book_concurrency: int = 1,
    force: bool = False,
    runner: BatchRunner = run_batch,
) -> dict[str, Any]:
    selected_models = _dedupe_models(models)
    if preferred_model not in selected_models:
        selected_models.insert(0, preferred_model)
    if fallback_model not in selected_models:
        selected_models.append(fallback_model)

    model_reports: list[dict[str, Any]] = []
    for model in selected_models:
        model_output_dir = output_dir / safe_stem(model)
        model_api_url = model_api_url_for_model(settings, model)
        model_settings = replace(settings, llm_api_url=model_api_url, llm_model=model, allow_fallback_models=False, fallback_models="")
        summary = runner(
            source_dir=source_dir,
            output_dir=model_output_dir,
            settings=model_settings,
            book_concurrency=max(1, book_concurrency),
            start_index=max(1, start_index),
            limit=max(1, limit),
            force=force,
        )
        model_reports.append(
            {
                "model": model,
                "llm_api_url": model_api_url,
                "output_dir": str(model_output_dir),
                "summary": summary,
                "quality": _quality_fingerprint(summary),
            }
        )

    recommendation = _recommend_model(
        model_reports=model_reports,
        preferred_model=preferred_model,
        fallback_model=fallback_model,
    )
    report = {
        "status": "passed" if all(str(row["summary"].get("status") or "") == "passed" for row in model_reports) else "review_required",
        "source_dir": str(source_dir.resolve()),
        "output_dir": str(output_dir.resolve()),
        "limit": max(1, limit),
        "start_index": max(1, start_index),
        "models": model_reports,
        "recommendation": recommendation,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "model_compare_summary.json").write_text(__import__("json").dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _dedupe_models(models: list[str]) -> list[str]:
    seen: set[str] = set()
    rows: list[str] = []
    for model in models:
        normalized = model.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        rows.append(normalized)
    return rows


def _quality_fingerprint(summary: dict[str, Any]) -> dict[str, Any]:
    documents = summary.get("documents")
    statuses = []
    if isinstance(documents, list):
        statuses = [str(row.get("status") or "") for row in documents if isinstance(row, dict)]
    return {
        "status": str(summary.get("status") or ""),
        "total": int(summary.get("total") or summary.get("document_count") or len(statuses) or 0),
        "passed": int(summary.get("passed") or summary.get("passed_count") or statuses.count("passed") or 0),
        "failed": int(summary.get("failed") or summary.get("failed_count") or 0),
        "skipped": int(summary.get("skipped") or statuses.count("skipped") or 0),
        "document_statuses": statuses,
    }


def _recommend_model(
    *,
    model_reports: list[dict[str, Any]],
    preferred_model: str,
    fallback_model: str,
) -> dict[str, Any]:
    by_model = {str(row["model"]): row for row in model_reports}
    preferred = by_model.get(preferred_model)
    fallback = by_model.get(fallback_model)

    def is_successful(report: dict[str, Any] | None) -> bool:
        if report is None:
            return False
        quality = report.get("quality")
        if not isinstance(quality, dict):
            return False
        return str(quality.get("status") or "") == "passed" and int(quality.get("passed") or 0) > 0

    preferred_success = is_successful(preferred)
    fallback_success = is_successful(fallback)
    equivalent = bool(preferred_success and fallback_success and preferred and fallback and preferred["quality"] == fallback["quality"])

    if equivalent:
        selected = preferred_model
        basis = "batch_summary_quality_fingerprint"
    elif fallback_success:
        selected = fallback_model
        basis = "fallback_has_better_success"
    elif preferred_success:
        selected = preferred_model
        basis = "preferred_only_successful_model"
    elif fallback is not None:
        selected = fallback_model
        basis = "no_successful_model"
    else:
        selected = preferred_model
        basis = "preferred_only_candidate"
    return {
        "preferred_model": selected,
        "fallback_model": fallback_model,
        "quality_equivalent": equivalent,
        "comparison_basis": basis,
    }
