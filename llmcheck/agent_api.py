"""Agent-callable surface for LLMcheck (P0 sync JobReport contract)."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import fields
from pathlib import Path
from typing import Any

from llmcheck.pipeline import (
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_MODEL_API_URLS,
    FINAL_MARKDOWN_DIR_NAME,
    PROCESS_DIR_NAME,
    LlmCheckError,
    LlmCheckSettings,
    model_api_url_from_overrides,
    process_documents,
)
from llmcheck.profiles import DEFAULT_PROFILE_ID, get_profile, list_profiles as _list_profiles

SCHEMA_VERSION = "1.0"
AGENT_JOB_REPORT_NAME = "agent_job_report.json"
SUMMARY_REPORT_NAME = "llmcheck_summary.json"

_VALID_JOB_STATUSES = frozenset({"passed", "review_required", "failed", "running"})


def list_profiles() -> dict[str, Any]:
    """Return built-in profiles in the agent-facing envelope."""
    return {
        "schema_version": SCHEMA_VERSION,
        "default_profile_id": DEFAULT_PROFILE_ID,
        "profiles": _list_profiles(),
    }


def build_settings_from_env_and_args(
    *,
    settings: LlmCheckSettings | None = None,
    llm_api_url: str | None = None,
    llm_api_key: str | None = None,
    llm_model: str | None = None,
    llm_mode: str | None = None,
    profile_id: str | None = None,
    mineru_api_key: str | None = None,
    concurrency: int | None = None,
    **extra: Any,
) -> LlmCheckSettings:
    """Build settings from an existing object, kwargs, and environment defaults.

    Agent convert defaults ``llm_mode`` to ``local-gate`` when not provided.
    Non-agent CLI defaults are unchanged.
    """
    if settings is not None:
        base = _settings_to_dict(settings)
    else:
        base = {}

    resolved_mode = (
        llm_mode
        if llm_mode is not None
        else base.get("llm_mode")
        or "local-gate"
    )
    resolved_model = (
        llm_model
        if llm_model is not None
        else base.get("llm_model")
        or _env_first("LLM_MODEL", "LLMCHECK_LLM_MODEL")
        or DEFAULT_LLM_MODEL
    )
    model_api_urls = str(
        extra.pop("llm_model_api_urls", None)
        or base.get("llm_model_api_urls")
        or _env_first("LLMCHECK_LLM_MODEL_API_URLS")
        or DEFAULT_LLM_MODEL_API_URLS
    )
    resolved_url = (
        llm_api_url
        if llm_api_url is not None
        else base.get("llm_api_url")
        or _env_first(
            "LLM_API_URL",
            "LLMCHECK_LLM_API_URL",
            "OPENAI_BASE_URL",
        )
        or model_api_url_from_overrides(model_api_urls, str(resolved_model))
        or ""
    )
    resolved_key = (
        llm_api_key
        if llm_api_key is not None
        else base.get("llm_api_key")
        or _env_first(
            "LLM_API_KEY",
            "LLMCHECK_LLM_API_KEY",
            "OPENAI_API_KEY",
        )
        or ""
    )
    resolved_profile = get_profile(
        profile_id
        if profile_id is not None
        else base.get("profile_id")
        or DEFAULT_PROFILE_ID
    ).id
    resolved_mineru = (
        mineru_api_key
        if mineru_api_key is not None
        else base.get("mineru_api_key")
        or _env_first(
            "MINERU_CLOUD_API_TOKEN",
            "MINERU_API_KEY",
            "LLMCHECK_MINERU_API_KEY",
        )
        or ""
    )

    llm_required = str(resolved_mode) != "local-gate"
    if llm_required and not resolved_url:
        raise LlmCheckError(
            "缺少 LLM API URL：请传 llm_api_url 或设置 LLM_API_URL/LLMCHECK_LLM_API_URL/OPENAI_BASE_URL"
        )
    if llm_required and not resolved_key:
        raise LlmCheckError(
            "缺少 LLM API key：请传 llm_api_key 或设置 LLM_API_KEY/LLMCHECK_LLM_API_KEY/OPENAI_API_KEY"
        )

    payload: dict[str, Any] = {
        **base,
        "llm_api_url": str(resolved_url or ""),
        "llm_api_key": str(resolved_key or ""),
        "llm_model": str(resolved_model),
        "llm_mode": str(resolved_mode),
        "profile_id": resolved_profile,
        "mineru_api_key": str(resolved_mineru),
        "llm_model_api_urls": model_api_urls,
    }
    if concurrency is not None:
        payload["concurrency"] = max(1, int(concurrency))
    for key, value in extra.items():
        if value is not None:
            payload[key] = value

    allowed = {field.name for field in fields(LlmCheckSettings)}
    filtered = {key: value for key, value in payload.items() if key in allowed}
    return LlmCheckSettings(**filtered)


def submit_convert(
    *,
    input_path: str | Path,
    output_dir: str | Path,
    settings: LlmCheckSettings | None = None,
    client: Any | None = None,
    preprocess_runner: Any | None = None,
    process_runner: Any | None = None,
    job_id: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run conversion synchronously and return a normalized agent JobReport."""
    source = Path(input_path)
    target = Path(output_dir)
    if not source.exists():
        raise LlmCheckError(f"输入路径不存在：{source}")

    resolved_settings = settings or build_settings_from_env_and_args(**kwargs)
    if settings is not None and kwargs:
        resolved_settings = build_settings_from_env_and_args(settings=settings, **kwargs)

    runner = process_runner or process_documents
    try:
        raw_report = runner(
            input_path=source,
            output_dir=target,
            settings=resolved_settings,
            client=client,
            preprocess_runner=preprocess_runner,
        )
    except TypeError:
        # Some mocks / older runners may not accept preprocess_runner/client kwargs.
        raw_report = runner(
            input_path=source,
            output_dir=target,
            settings=resolved_settings,
        )
    except LlmCheckError:
        raise
    except Exception as exc:  # pragma: no cover - defensive mapping
        failed = _empty_job_report(
            job_id=job_id or uuid.uuid4().hex,
            status="failed",
            profile_id=resolved_settings.profile_id,
            output_dir=target,
            error=str(exc),
        )
        _persist_agent_report(target, failed)
        return failed

    if not isinstance(raw_report, dict):
        raise LlmCheckError("process_documents 返回了非 dict 报告")

    report = normalize_job_report(
        raw_report,
        output_dir=target,
        job_id=job_id,
        profile_id=resolved_settings.profile_id,
    )
    _persist_agent_report(target, report)
    return report


def get_job(
    *,
    output_dir: str | Path | None = None,
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    """Load and normalize an agent job report from disk."""
    path = _resolve_report_path(output_dir=output_dir, report_path=report_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LlmCheckError(f"无法读取 job 报告：{path}") from exc
    except json.JSONDecodeError as exc:
        raise LlmCheckError(f"job 报告不是合法 JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise LlmCheckError(f"job 报告格式无效：{path}")

    inferred_output = Path(output_dir) if output_dir is not None else _infer_output_dir(path, payload)
    return normalize_job_report(payload, output_dir=inferred_output)


def get_final_markdown(
    *,
    output_dir: str | Path,
    document_id: str | None = None,
    max_chars: int | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Return final Markdown only when the document status is ``passed``.

    Iron rule: never return draft/process markdown as final.
    """
    root = Path(output_dir)
    report = get_job(output_dir=root)
    documents = list(report.get("documents") or [])

    selected: dict[str, Any] | None = None
    if document_id:
        for row in documents:
            if str(row.get("document_id") or "") == document_id:
                selected = row
                break
        if selected is None:
            return {
                "status": "error",
                "document_id": document_id,
                "error": f"document_id not found: {document_id}",
            }
    elif path is not None:
        wanted = str(Path(path).resolve())
        for row in documents:
            final_path = str(row.get("final_markdown_path") or "")
            if final_path and str(Path(final_path).resolve()) == wanted:
                selected = row
                break
        if selected is None:
            # Fall back to matching by basename under md/
            basename = Path(path).name
            for row in documents:
                final_path = str(row.get("final_markdown_path") or "")
                if Path(final_path).name == basename:
                    selected = row
                    break
        if selected is None:
            return {
                "status": "error",
                "document_id": "",
                "error": f"document path not found in job report: {path}",
            }
    elif len(documents) == 1:
        selected = documents[0]
    elif not documents:
        return {
            "status": "error",
            "document_id": "",
            "error": "job report has no documents",
        }
    else:
        return {
            "status": "error",
            "document_id": "",
            "error": "document_id is required when the job has multiple documents",
        }

    doc_id = str(selected.get("document_id") or "")
    status = str(selected.get("status") or "")
    if status != "passed":
        return {
            "status": status or "error",
            "document_id": doc_id,
            "error": "final markdown is only available when document status is passed",
            "path": str(selected.get("final_markdown_path") or ""),
        }

    final_path_raw = str(selected.get("final_markdown_path") or "")
    if not final_path_raw:
        return {
            "status": "error",
            "document_id": doc_id,
            "error": "passed document is missing final_markdown_path",
        }

    final_path = Path(final_path_raw)
    if not final_path.is_absolute():
        final_path = (root / final_path).resolve()
    else:
        final_path = final_path.resolve()

    md_root = (root / FINAL_MARKDOWN_DIR_NAME).resolve()
    try:
        final_path.relative_to(md_root)
    except ValueError:
        return {
            "status": "error",
            "document_id": doc_id,
            "error": "final markdown path is outside md/ delivery directory",
            "path": str(final_path),
        }

    if not final_path.is_file():
        return {
            "status": "error",
            "document_id": doc_id,
            "error": f"final markdown file does not exist: {final_path}",
            "path": str(final_path),
        }

    text = final_path.read_text(encoding="utf-8")
    char_count = len(text)
    sha256 = str(selected.get("final_markdown_sha256") or "") or _sha256_text(text)
    payload: dict[str, Any] = {
        "status": "passed",
        "document_id": doc_id,
        "path": str(final_path),
        "sha256": sha256,
        "char_count": char_count,
    }
    if max_chars is not None and max_chars >= 0 and char_count > max_chars:
        payload["text"] = text[:max_chars]
        payload["truncated"] = True
    else:
        payload["text"] = text
        payload["truncated"] = False
    return payload


def normalize_job_report(
    raw: dict[str, Any],
    *,
    output_dir: str | Path | None = None,
    job_id: str | None = None,
    profile_id: str | None = None,
) -> dict[str, Any]:
    """Normalize process_documents / summary reports into agent JobReport schema."""
    root = Path(output_dir) if output_dir is not None else Path(str(raw.get("output_dir") or ".")).resolve()
    root = root.resolve()
    process_dir = root / PROCESS_DIR_NAME
    md_dir = root / FINAL_MARKDOWN_DIR_NAME

    status = str(raw.get("status") or "failed")
    if status not in _VALID_JOB_STATUSES:
        # process_documents only emits passed/review_required; map unknowns carefully.
        status = "failed" if status in {"error", "failed"} else "review_required"

    resolved_job_id = (
        job_id
        or str(raw.get("job_id") or "")
        or str(raw.get("run_id") or "")
        or uuid.uuid4().hex
    )
    resolved_profile = (
        profile_id
        or str(raw.get("profile_id") or "")
        or DEFAULT_PROFILE_ID
    )

    documents: list[dict[str, Any]] = []
    for row in raw.get("documents") or []:
        if not isinstance(row, dict):
            continue
        documents.append(_normalize_document_row(row, output_dir=root))

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "job_id": resolved_job_id,
        "status": status,
        "profile_id": resolved_profile,
        "documents": documents,
        "artifacts": {
            "md_dir": str(md_dir),
            "process_dir": str(process_dir),
        },
    }

    # Preserve useful diagnostic counters when present.
    for key in (
        "input_path",
        "output_dir",
        "document_count",
        "passed_count",
        "failed_count",
        "model",
        "error",
    ):
        if key in raw and key not in report:
            report[key] = raw[key]
    if "output_dir" not in report:
        report["output_dir"] = str(root)
    return report


def _normalize_document_row(row: dict[str, Any], *, output_dir: Path) -> dict[str, Any]:
    document_id = str(row.get("document_id") or "")
    status = str(row.get("status") or "")
    final_path = str(row.get("final_markdown_path") or "")
    if final_path and not Path(final_path).is_absolute():
        final_path = str((output_dir / final_path).resolve())
    final_sha = str(row.get("final_markdown_sha256") or row.get("sha256") or "")
    final_acceptance = str(
        row.get("final_acceptance_report_path")
        or row.get("acceptance_report_path")
        or ""
    )
    cross_report = str(row.get("cross_report_path") or "")
    error = str(row.get("error") or "")
    return {
        "document_id": document_id,
        "status": status,
        "final_markdown_path": final_path if status == "passed" else final_path,
        "final_markdown_sha256": final_sha,
        "final_acceptance_report_path": final_acceptance,
        "cross_report_path": cross_report,
        "error": error,
    }


def _persist_agent_report(output_dir: Path, report: dict[str, Any]) -> Path:
    reports_dir = Path(output_dir) / PROCESS_DIR_NAME / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / AGENT_JOB_REPORT_NAME
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _resolve_report_path(
    *,
    output_dir: str | Path | None,
    report_path: str | Path | None,
) -> Path:
    if report_path is not None:
        path = Path(report_path)
        if not path.is_file():
            raise LlmCheckError(f"report_path 不存在：{path}")
        return path
    if output_dir is None:
        raise LlmCheckError("get_job 需要 output_dir 或 report_path")
    root = Path(output_dir)
    candidates = [
        root / PROCESS_DIR_NAME / "reports" / AGENT_JOB_REPORT_NAME,
        root / PROCESS_DIR_NAME / "reports" / SUMMARY_REPORT_NAME,
        root / AGENT_JOB_REPORT_NAME,
        root / SUMMARY_REPORT_NAME,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise LlmCheckError(
        f"未找到 agent job 报告：尝试过 {[str(item) for item in candidates]}"
    )


def _infer_output_dir(report_path: Path, payload: dict[str, Any]) -> Path:
    if payload.get("output_dir"):
        return Path(str(payload["output_dir"])).resolve()
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    process_dir = artifacts.get("process_dir") if isinstance(artifacts, dict) else None
    if process_dir:
        return Path(str(process_dir)).resolve().parent
    # process/reports/<file>
    if report_path.parent.name == "reports" and report_path.parent.parent.name == PROCESS_DIR_NAME:
        return report_path.parent.parent.parent.resolve()
    return report_path.parent.resolve()


def _empty_job_report(
    *,
    job_id: str,
    status: str,
    profile_id: str,
    output_dir: Path,
    error: str = "",
) -> dict[str, Any]:
    root = Path(output_dir).resolve()
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "status": status,
        "profile_id": profile_id,
        "documents": [],
        "artifacts": {
            "md_dir": str(root / FINAL_MARKDOWN_DIR_NAME),
            "process_dir": str(root / PROCESS_DIR_NAME),
        },
        "output_dir": str(root),
        "error": error,
    }


def _settings_to_dict(settings: LlmCheckSettings) -> dict[str, Any]:
    return {field.name: getattr(settings, field.name) for field in fields(settings)}


def _env_first(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
