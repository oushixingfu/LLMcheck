from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re

from llmcheck.final_gate import (
    BAD_CONTROL_RE,
    MOJIBAKE_PATTERNS,
    ZERO_WIDTH_RE,
    forced_line_break_candidates,
)


READABLE_RE = re.compile(r"[㐀-鿿A-Za-z0-9]")
CJK_RE = re.compile(r"[㐀-鿿]")
PUNCTUATION_CHARS = "，。；：！？、,.!?;:"
NEAR_TIE_MARGIN = 5.0
MIN_USABLE_READABLE_CHARS = 1


def score_candidate(text: str) -> dict[str, Any]:
    normalized = text if isinstance(text, str) else ""
    stripped = normalized.strip()
    empty = not stripped
    readable_chars = len(READABLE_RE.findall(normalized))
    chinese_chars = len(CJK_RE.findall(normalized))
    punctuation_count = sum(normalized.count(char) for char in PUNCTUATION_CHARS)
    punctuation_density = punctuation_count / max(1, chinese_chars if chinese_chars else readable_chars)
    mojibake = any(pattern in normalized for pattern in MOJIBAKE_PATTERNS)
    replacement_chars = "�" in normalized
    control_chars = bool(BAD_CONTROL_RE.search(normalized))
    zero_width_chars = bool(ZERO_WIDTH_RE.search(normalized))
    forced_breaks = len(forced_line_break_candidates(normalized))
    repeated_short_lines = _repeated_short_line_count(normalized)

    reasons: list[str] = []
    score = 0.0

    if empty:
        score -= 1000.0
        reasons.append("empty")
    elif readable_chars < 20:
        score -= 200.0
        reasons.append("low_readable_chars")

    if mojibake:
        score -= 300.0
        reasons.append("mojibake")
    if replacement_chars:
        score -= 300.0
        reasons.append("replacement_characters")
    if control_chars:
        score -= 250.0
        reasons.append("bad_control_characters")
    if zero_width_chars:
        score -= 50.0
        reasons.append("zero_width_characters")
    if forced_breaks:
        penalty = min(120.0, forced_breaks * 2.0)
        score -= penalty
        reasons.append(f"forced_line_breaks:{forced_breaks}")
    if repeated_short_lines:
        penalty = min(80.0, repeated_short_lines * 5.0)
        score -= penalty
        reasons.append(f"repeated_short_lines:{repeated_short_lines}")

    if readable_chars:
        score += min(readable_chars, 50_000) * 0.01
        reasons.append(f"readable_chars:{readable_chars}")
    if punctuation_count:
        score += min(punctuation_density, 0.5) * 100.0
        reasons.append(f"punctuation_density:{punctuation_density:.4f}")

    usable = (not empty) and readable_chars >= MIN_USABLE_READABLE_CHARS and score > -900.0
    return {
        "readable_chars": readable_chars,
        "chinese_chars": chinese_chars,
        "punctuation_count": punctuation_count,
        "punctuation_density": round(punctuation_density, 4),
        "empty": empty,
        "mojibake": mojibake,
        "replacement_characters": replacement_chars,
        "control_chars": control_chars,
        "zero_width_chars": zero_width_chars,
        "forced_line_breaks": forced_breaks,
        "repeated_short_lines": repeated_short_lines,
        "usable": usable,
        "score": round(score, 4),
        "reasons": reasons,
    }


def select_initial_markdown(
    *,
    mineru_text: str | None,
    ppx_text: str | None,
    existing_md: str | None = None,
) -> dict[str, Any]:
    if existing_md is not None:
        score = score_candidate(existing_md)
        if score["usable"]:
            return {
                "mode": "existing_md",
                "winner": "existing_md",
                "scores": {"existing_md": score},
                "reasons": ["source_is_existing_markdown", *score["reasons"]],
                "text": existing_md,
            }
        return {
            "mode": "failed",
            "winner": None,
            "scores": {"existing_md": score},
            "reasons": ["existing_markdown_unusable", *score["reasons"]],
            "text": "",
        }

    mineru_score = score_candidate(mineru_text or "") if mineru_text is not None else None
    ppx_score = score_candidate(ppx_text or "") if ppx_text is not None else None
    scores: dict[str, Any] = {}
    if mineru_score is not None:
        scores["mineru"] = mineru_score
    if ppx_score is not None:
        scores["ppx"] = ppx_score

    mineru_usable = bool(mineru_score and mineru_score["usable"])
    ppx_usable = bool(ppx_score and ppx_score["usable"])

    if mineru_usable and ppx_usable:
        assert mineru_score is not None and ppx_score is not None
        mineru_value = float(mineru_score["score"])
        ppx_value = float(ppx_score["score"])
        if abs(mineru_value - ppx_value) <= NEAR_TIE_MARGIN:
            return {
                "mode": "selected_mineru",
                "winner": "mineru",
                "scores": scores,
                "reasons": [
                    "near_tie_prefers_mineru",
                    f"mineru_score={mineru_value}",
                    f"ppx_score={ppx_value}",
                    *mineru_score["reasons"],
                ],
                "text": mineru_text or "",
            }
        if ppx_value > mineru_value:
            return {
                "mode": "selected_ppx",
                "winner": "ppx",
                "scores": scores,
                "reasons": [
                    "ppx_higher_score",
                    f"mineru_score={mineru_value}",
                    f"ppx_score={ppx_value}",
                    *ppx_score["reasons"],
                ],
                "text": ppx_text or "",
            }
        return {
            "mode": "selected_mineru",
            "winner": "mineru",
            "scores": scores,
            "reasons": [
                "mineru_higher_score",
                f"mineru_score={mineru_value}",
                f"ppx_score={ppx_value}",
                *mineru_score["reasons"],
            ],
            "text": mineru_text or "",
        }

    if mineru_usable:
        assert mineru_score is not None
        mode = "selected_mineru" if ppx_score is not None else "mineru_only"
        return {
            "mode": mode,
            "winner": "mineru",
            "scores": scores,
            "reasons": ["mineru_only_usable" if mode == "mineru_only" else "ppx_unusable", *mineru_score["reasons"]],
            "text": mineru_text or "",
        }

    if ppx_usable:
        assert ppx_score is not None
        # Both sides present (even if MinerU unusable) → selected_ppx; only PPX present → ppx_fallback.
        mode = "selected_ppx" if mineru_score is not None else "ppx_fallback"
        return {
            "mode": mode,
            "winner": "ppx",
            "scores": scores,
            "reasons": [
                "ppx_fallback" if mode == "ppx_fallback" else "mineru_unusable",
                *ppx_score["reasons"],
            ],
            "text": ppx_text or "",
        }

    reasons = ["no_usable_candidate"]
    if mineru_score is not None:
        reasons.extend(f"mineru:{item}" for item in mineru_score["reasons"])
    if ppx_score is not None:
        reasons.extend(f"ppx:{item}" for item in ppx_score["reasons"])
    return {
        "mode": "failed",
        "winner": None,
        "scores": scores,
        "reasons": reasons,
        "text": "",
    }


def write_cross_artifacts(
    work_dir: Path,
    *,
    mineru_path: Path | None,
    ppx_path: Path | None,
    existing_md_path: Path | None = None,
) -> Path:
    mineru_text = _read_optional_text(mineru_path)
    ppx_text = _read_optional_text(ppx_path)
    existing_text = _read_optional_text(existing_md_path)

    selection = select_initial_markdown(
        mineru_text=mineru_text if mineru_path is not None else None,
        ppx_text=ppx_text if ppx_path is not None else None,
        existing_md=existing_text if existing_md_path is not None else None,
    )

    cross_dir = work_dir / "cross"
    cross_dir.mkdir(parents=True, exist_ok=True)
    initial_path = cross_dir / "initial.md"
    report_path = cross_dir / "cross_report.json"

    text = str(selection.get("text") or "")
    if text and not text.endswith("\n"):
        text = text + "\n"
    initial_path.write_text(text, encoding="utf-8")

    report = {
        "mode": selection["mode"],
        "winner": selection["winner"],
        "scores": selection["scores"],
        "reasons": selection["reasons"],
        "mineru_path": str(mineru_path) if mineru_path is not None else "",
        "ppx_path": str(ppx_path) if ppx_path is not None else "",
        "existing_md_path": str(existing_md_path) if existing_md_path is not None else "",
        "initial_markdown": str(initial_path),
        "usable": selection["mode"] != "failed",
        "char_count": len(text),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return initial_path


def _read_optional_text(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        if not path.exists() or path.stat().st_size <= 0:
            return ""
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _repeated_short_line_count(text: str) -> int:
    counts: dict[str, int] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if 3 <= len(line) <= 40 and not line.startswith(("#", "|")):
            counts[line] = counts.get(line, 0) + 1
    return sum(1 for count in counts.values() if count >= 3)
