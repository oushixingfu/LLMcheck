from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


RiskLevel = Literal["low", "medium", "high"]
WriteMode = Literal["auto_apply", "report_only", "block"]


@dataclass(frozen=True)
class RuleDefinition:
    rule_id: str
    description: str
    risk_level: RiskLevel
    write_mode: WriteMode

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


BUILTIN_RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition("latex.strip_empty_math", "Remove empty LaTeX math wrappers.", "low", "auto_apply"),
    RuleDefinition("latex.unit_math_to_text", "Convert simple LaTeX dosage units to plain text.", "low", "auto_apply"),
    RuleDefinition("latex.temperature_to_celsius", "Convert LaTeX Celsius expressions to ℃ text.", "low", "auto_apply"),
    RuleDefinition("latex.lab_value_to_text", "Convert simple LaTeX lab-value units to plain text.", "low", "auto_apply"),
    RuleDefinition("ocr.temperature_percent_to_celsius", "Convert OCR percent artifacts in temperature contexts to ℃ text.", "medium", "auto_apply"),
    RuleDefinition("ocr.embedded_foreign_math_noise_remove", "Remove embedded foreign theorem/math OCR noise fragments.", "medium", "auto_apply"),
    RuleDefinition("markdown.heading_spacing", "Normalize Markdown heading spacing.", "low", "auto_apply"),
    RuleDefinition("markdown.heading_marker", "Report or normalize heading marker issues.", "medium", "report_only"),
    RuleDefinition("paragraph.safe_line_join", "Join low-risk physical OCR line breaks.", "low", "auto_apply"),
    RuleDefinition("artifact.zero_width_remove", "Remove zero-width characters.", "low", "auto_apply"),
    RuleDefinition("artifact.replacement_char_remove", "Remove isolated Unicode replacement characters.", "medium", "auto_apply"),
    RuleDefinition("artifact.bad_control_remove", "Remove invalid control characters.", "low", "auto_apply"),
    RuleDefinition("metadata.trailing_archive_block_remove", "Remove trailing archive generator metadata blocks.", "low", "auto_apply"),
    RuleDefinition("artifact.mojibake_detect", "Detect visible encoding corruption.", "high", "block"),
    RuleDefinition("artifact.replacement_char_detect", "Detect Unicode replacement characters.", "high", "block"),
    RuleDefinition("artifact.duplicate_running_header_detect", "Detect repeated running headers or footers.", "medium", "report_only"),
)


RULE_REGISTRY: dict[str, RuleDefinition] = {rule.rule_id: rule for rule in BUILTIN_RULES}


def rule_definition(rule_id: str) -> RuleDefinition | None:
    return RULE_REGISTRY.get(rule_id)


def normalize_write_mode(value: object) -> WriteMode:
    raw = str(value or "auto_apply")
    aliases = {"auto": "auto_apply", "warn": "report_only", "error": "block"}
    normalized = aliases.get(raw, raw)
    if normalized in {"auto_apply", "report_only", "block"}:
        return normalized  # type: ignore[return-value]
    return "report_only"
