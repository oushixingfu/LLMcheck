from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from llmcheck.llm import LlmClient, LlmConfig
from llmcheck.pipeline import LlmCheckSettings, model_api_url_for_model


ClientFactory = Callable[[LlmConfig], Any]


def preflight_llm_models(
    settings: LlmCheckSettings,
    *,
    client_factory: ClientFactory = LlmClient,
) -> dict[str, Any]:
    models = _planned_models(settings)
    missing_url_models = [model for model in models if not model_api_url_for_model(settings, model).strip()]
    if missing_url_models or not settings.llm_api_key.strip():
        return {
            "status": "blocked_needs_llm_service",
            "error": "missing_llm_service_configuration",
            "models": [
                {
                    "model": model,
                    "available": False,
                    "error": "missing LLM API URL or key" if model in missing_url_models else "missing LLM API key",
                }
                for model in models
            ],
        }

    base_config = LlmConfig(
        api_url=model_api_url_for_model(settings, settings.llm_model),
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout_seconds=max(10, settings.llm_call_timeout_seconds or settings.timeout_seconds),
        max_calls_per_book=0,
    )
    rows: list[dict[str, Any]] = []
    preferred_available_model = ""
    for model in models:
        client = client_factory(replace(base_config, api_url=model_api_url_for_model(settings, model), model=model))
        result = client.complete_json('Return JSON only: {"status":"ok"}')
        available = isinstance(result, dict) and result.get("status") != "error"
        row: dict[str, Any] = {"model": model, "available": available}
        if not available:
            row["error"] = str(result.get("error") or "LLM model unavailable") if isinstance(result, dict) else "invalid LLM probe result"
        rows.append(row)
        if available and not preferred_available_model:
            preferred_available_model = model

    if preferred_available_model:
        return {
            "status": "passed",
            "models": rows,
            "preferred_available_model": preferred_available_model,
        }
    return {
        "status": "blocked_needs_llm_service",
        "error": "all_review_models_unavailable",
        "models": rows,
    }


def _planned_models(settings: LlmCheckSettings) -> list[str]:
    return _dedupe([settings.llm_model, *[item.strip() for item in settings.fallback_models.split(",") if item.strip()]])


def _dedupe(models: list[str]) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for model in models:
        normalized = model.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        rows.append(normalized)
    return rows
