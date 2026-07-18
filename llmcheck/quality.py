from __future__ import annotations

import hashlib
import re
from typing import Any

import llmcheck.cleaning as cleaning
from llmcheck.rules import normalize_write_mode, rule_definition


BAD_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
MOJIBAKE_PATTERNS = ("锟斤拷", "Ã", "Â", "â€™", "â€œ", "â€�")
ZERO_WIDTH_RE = re.compile(r"[​‌‍﻿]")
MINERU_FLOWCHART_DETAILS_RE = re.compile(
    r"\n*<details>\s*<summary>\s*flowchart\s*</summary>(?P<body>.*?)</details>\s*",
    re.IGNORECASE | re.DOTALL,
)
MINERU_NOISE_DETAILS_RE = re.compile(
    r"\n*<details>\s*<summary>\s*(?:line|text_image|flowchart|natural_image|radar)\s*</summary>.*?</details>\s*",
    re.IGNORECASE | re.DOTALL,
)
DEFAULT_STRUCTURE_LABELS = (
    "头部：",
    "面部：",
    "项部：",
    "颈部：",
    "肩部：",
    "背部：",
    "胸部：",
    "腹部：",
    "腰部：",
    "肘部：",
    "腕部：",
    "手部：",
    "髀部：",
    "髋部：",
    "膝部：",
    "踝部：",
    "足部：",
)
DEFAULT_TABLE_HEADERS: tuple[tuple[str, ...], ...] = (
    ("分类", "药名", "功效", "主治", "用量"),
    ("类别", "药名", "功效", "主治", "用量"),
    ("方名", "出处", "组成", "功效", "主治", "用法"),
    ("方名", "来源", "药物组成", "功效", "主治", "制用法"),
)
clean_markdown_text = cleaning.clean_markdown_text
clean_markdown_with_report = cleaning.clean_markdown_with_report

def quality_hints(text: str) -> dict[str, Any]:
    from llmcheck.final_gate import quality_hints as _quality_hints

    return _quality_hints(text)



def quality_errors(text: str) -> list[str]:
    from llmcheck.final_gate import quality_errors as _quality_errors

    return _quality_errors(text)



def finalize_standard_document(text: str) -> dict[str, object]:
    from llmcheck.structure import finalize_standard_document as _finalize_standard_document

    return _finalize_standard_document(text)



def final_acceptance_report(text: str) -> dict[str, object]:
    from llmcheck.final_gate import final_acceptance_report as _final_acceptance_report

    return _final_acceptance_report(text)



def forced_line_break_candidates(text: str, *, limit: int | None = None, structure_labels: tuple[str, ...] | None = None) -> list[dict[str, object]]:
    from llmcheck.final_gate import forced_line_break_candidates as _forced_line_break_candidates

    return _forced_line_break_candidates(text, limit=limit, structure_labels=structure_labels)



def safe_stem(value: str) -> str:
    return cleaning.safe_stem(value)



def repair_acceptance_locally(text: str, acceptance: dict[str, Any]) -> dict[str, Any]:
    return cleaning.repair_acceptance_locally(text, acceptance)



def _apply_cleaning_rule(
    rule_changes: list[dict[str, object]],
    value: str,
    *,
    rule_id: str,
    risk_level: str,
    transform: Any,
    match_count: int | None = None,
    write_mode: str = "auto_apply",
) -> str:
    updated = str(transform(value))
    if updated != value:
        rule_changes.append(_rule_change(rule_id, value, updated, risk_level=risk_level, write_mode=write_mode, match_count=match_count))
    return updated



def _record_line_rule(
    rule_changes: list[dict[str, object]],
    *,
    before: str,
    after: str,
    rule_id: str,
    risk_level: str,
    write_mode: str = "auto",
) -> str:
    if after != before:
        rule_changes.append(_rule_change(rule_id, before, after, risk_level=risk_level, write_mode=write_mode))
    return after



def _rule_change(
    rule_id: str,
    before: str,
    after: str,
    *,
    risk_level: str,
    write_mode: str,
    match_count: int | None = None,
) -> dict[str, object]:
    definition = rule_definition(rule_id)
    normalized_write_mode = normalize_write_mode(write_mode or (definition.write_mode if definition else "auto_apply"))
    normalized_risk_level = risk_level or (definition.risk_level if definition else "medium")
    return {
        "rule_id": rule_id,
        "description": definition.description if definition else "",
        "risk_level": normalized_risk_level,
        "write_mode": normalized_write_mode,
        "match_count": max(1, int(match_count or 1)),
        "input_sha256": _text_sha256(before),
        "output_sha256": _text_sha256(after),
        "examples": [{"before": _preview_change_text(before), "after": _preview_change_text(after)}],
    }



def _preview_change_text(value: str, *, limit: int = 160) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    return compact[:limit]



def _normalize_latex_unit_artifacts(text: str) -> str:
    unit_names = r"g|mg|kg|ug|μg|ml|mL|L|mm|cm|m|min|s|h|kPa|mol|U"
    cleaned = re.sub(
        r"\$?\s*(?P<number>\d+(?:\.\d+)?)\s*(?:\^\s*\{\s*\\circ\s*\}|\^\s*\\circ|\\circ)\s*(?:\\mathrm\s*\{\s*C\s*\}|C)\s*\$?",
        lambda match: f" {match.group('number')}℃",
        text,
    )
    cleaned = re.sub(r"\$?\s*\\mathrm\s*\{\s*\}\s*\$?", " ", cleaned)
    latex_symbols = {
        "degreeC": "℃",
        "degree": "°",
        "circ": "°",
        "times": "×",
        "cdot": "·",
        "pm": "±",
        "mu": "μ",
        "alpha": "α",
        "beta": "β",
        "gamma": "γ",
        "delta": "δ",
        "Delta": "Δ",
        "sim": "~",
        "rightarrow": "→",
        "to": "→",
        "leq": "≤",
        "geq": "≥",
    }
    for command, replacement in latex_symbols.items():
        cleaned = re.sub(rf"\\{command}(?![A-Za-z])", replacement, cleaned)
    cleaned = cleaned.replace(r"\%", "%")
    cleaned = re.sub(
        rf"\$?\s*(?P<number>\d+(?:\.\d+)?)\s*\\mathrm\s*\{{\s*(?P<unit>{unit_names})\s*\}}\s*\$?",
        lambda match: f" {match.group('number')}{match.group('unit')}",
        cleaned,
    )
    cleaned = re.sub(
        rf"\$?\s*\\mathrm\s*\{{\s*(?P<unit>{unit_names}|C)\s*\}}\s*\$?",
        lambda match: match.group("unit"),
        cleaned,
    )
    wrapper_commands = r"mathrm|mathbf|mathsf|text|operatorname|mathit|mathcal|mathbb"
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = re.sub(rf"\\(?:{wrapper_commands})\s*\{{\s*([^{{}}\n]{{1,160}})\s*\}}", r"\1", cleaned)
    cleaned = re.sub(r"\^\s*\{\s*([^{}\n]{1,32})\s*\}", r"^\1", cleaned)
    cleaned = re.sub(r"_\s*\{\s*([^{}\n]{1,32})\s*\}", r"_\1", cleaned)
    cleaned = re.sub(r"\$\s*([^$\n]{1,120})\s*\$", r"\1", cleaned)
    cleaned = cleaned.replace("$", "")
    cleaned = re.sub(r"(?<=\d)\s*~\s*(?=\d)", "~", cleaned)
    cleaned = re.sub(r"μ\s*mol\s*/\s*L", "μmol/L", cleaned)
    cleaned = re.sub(r"\b([A-Za-z])\s*/\s*([A-Za-z])\s*=\s*", r"\1/\2=", cleaned)
    cleaned = re.sub(r"([㐀-鿿]{2,8})\n\n(\d+~\d+)", r"\1\2", cleaned)
    cleaned = re.sub(r"\$\s*([^$\n]{0,80}?(?:℃|\d+(?:\.\d+)?(?:g|mg|kg|ug|μg|ml|mL|L|mm|cm|m|min|s|h|kPa)))\s*\$", r"\1", cleaned)
    cleaned = re.sub(r"\\([A-Za-z]+)\s*\{\s*([^{}\n]{1,160})\s*\}", r"\2", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+(?=\d+(?:\.\d+)?(?:g|mg|kg|ug|μg|ml|mL|L|mm|cm|m|min|s|h|kPa)\b)", " ", cleaned)
    return cleaned



def _acceptance_blocking_issues(acceptance: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for chunk in acceptance.get("chunks", []):
        if not isinstance(chunk, dict) or chunk.get("accepted") is True:
            continue
        llm_result = chunk.get("llm_result")
        if not isinstance(llm_result, dict):
            continue
        for issue in llm_result.get("blocking_issues", []):
            if isinstance(issue, dict):
                issues.append(issue)
    llm_result = acceptance.get("llm_result")
    if isinstance(llm_result, dict):
        for issue in llm_result.get("blocking_issues", []):
            if isinstance(issue, dict):
                issues.append(issue)
    return issues



def _transcribe_table_flowchart_details(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        prefix = text[max(0, match.start() - 500) : match.start()]
        if not _near_table_image(prefix):
            return "\n\n"
        rows = _flowchart_rows(match.group("body"))
        if not rows:
            return "\n\n"
        table = ["表格结构转写（由 MinerU flowchart 提取）", "", "| 项目 | 内容 |", "|---|---|"]
        table.extend(f"| {left} | {right} |" for left, right in rows)
        return "\n\n" + "\n".join(table) + "\n\n"

    return MINERU_FLOWCHART_DETAILS_RE.sub(replace, text)



def _near_table_image(prefix: str) -> bool:
    tail = prefix[-500:]
    if re.search(r"!\[[^\]]*(?:表|table)[^\]]*\]\([^)]*\)\s*$", tail, flags=re.IGNORECASE):
        return True
    return bool(re.search(r"(?:^|\n)\s*(?:表|Table)\s*\d+", tail, flags=re.IGNORECASE))



def _flowchart_rows(body: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in body.splitlines():
        line = raw_line.strip().strip(";")
        if not line or line.startswith("```") or re.match(r"^(?:flowchart|graph)\b", line, flags=re.IGNORECASE):
            continue
        if "-->" not in line:
            continue
        left_raw, right_raw = line.split("-->", 1)
        left = _flowchart_node_label(left_raw)
        right = _flowchart_node_label(right_raw)
        if not left or not right:
            continue
        row = (left, right)
        if row in seen:
            continue
        seen.add(row)
        rows.append(row)
    return rows



def _flowchart_node_label(raw_value: str) -> str:
    value = raw_value.strip().strip(";")
    value = re.sub(r"\s*[-.=]+.*$", "", value).strip()
    bracketed = re.search(r'[\[\(\{]\s*["“]?(.+?)["”]?\s*[\]\)\}]', value)
    if bracketed:
        value = bracketed.group(1)
    value = re.sub(r"^[A-Za-z0-9_]+$", "", value).strip()
    value = value.strip("`\"'“”")
    return value.replace("|", "｜").strip()



def _split_local_structure_glue(text: str, *, structure_labels: tuple[str, ...] | None = None) -> str:
    labels = structure_labels or DEFAULT_STRUCTURE_LABELS
    cleaned = text
    for label in labels:
        escaped = re.escape(label)
        cleaned = re.sub(rf"(?<!^)(?<!\n)(?<=[㐀-鿿A-Za-z0-9）)])({escaped})", r"\n\1", cleaned)
    cleaned = re.sub(r"(?<=年版)(?=(?:[㐀-鿿]{2,4})?《)", "\n", cleaned)
    return cleaned



def _html_table_tags_to_markdown(text: str) -> str:
    cleaned = re.sub(r"</(?:td|th)>\s*<(?:td|th)[^>]*>", " | ", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<tr[^>]*>\s*<(?:td|th)[^>]*>", "\n| ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</(?:td|th)>\s*</tr>", " |\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?(?:table|thead|tbody|tfoot)[^>]*>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?tr[^>]*>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?(?:td|th)[^>]*>", " | ", cleaned, flags=re.IGNORECASE)
    return cleaned



def _normalize_markdown_tables(lines: list[str], *, table_headers: tuple[tuple[str, ...], ...] | None = None) -> list[str]:
    merged: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _is_table_row(line) and not line.strip().endswith("|"):
            current = line.strip()
            index += 1
            while index < len(lines) and not current.endswith("|"):
                next_line = lines[index].strip()
                index += 1
                if next_line:
                    current = f"{current} {next_line}"
            merged.append(current)
            continue
        merged.append(line)
        index += 1

    merged = _merge_table_continuation_rows(merged, known_headers=table_headers)
    normalized: list[str] = []
    for index, line in enumerate(merged):
        stripped = line.strip()
        if not stripped:
            previous = normalized[-1].strip() if normalized else ""
            following = _next_nonblank(merged, index + 1)
            if _is_table_row(previous) and _is_table_row(following):
                continue
        normalized.append(line)
        if _is_known_table_header(stripped, known_headers=table_headers):
            following = _next_nonblank(merged, index + 1)
            if not _is_table_separator(following):
                normalized.append(_table_separator(stripped))
    return normalized



def _next_nonblank(lines: list[str], start: int) -> str:
    for line in lines[start:]:
        stripped = line.strip()
        if stripped:
            return stripped
    return ""



def _merge_table_continuation_rows(lines: list[str], *, known_headers: tuple[tuple[str, ...], ...] | None = None) -> list[str]:
    normalized: list[str] = []
    active_cols = 0
    for line in lines:
        stripped = line.strip()
        if _is_known_table_header(stripped, known_headers=known_headers):
            active_cols = len(_table_cells(stripped))
            normalized.append(line)
            continue
        if not _is_table_row(stripped) or _is_table_separator(stripped) or not active_cols:
            if stripped and not _is_table_row(stripped):
                active_cols = 0
            normalized.append(line)
            continue
        cells = _table_cells(stripped)
        if _has_embedded_table_header(cells, known_headers=known_headers):
            continue
        if normalized and len(cells) < active_cols:
            previous = normalized[-1].strip()
            previous_cells = _table_cells(previous) if _is_table_row(previous) else []
            trailing_empty = _trailing_empty_cell_count(previous_cells)
            if len(previous_cells) == active_cols and trailing_empty >= len(cells):
                normalized[-1] = _format_table_row(previous_cells[: active_cols - len(cells)] + cells)
                continue
            continue
        normalized.append(line)
    return normalized



def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2



def _is_table_separator(line: str) -> bool:
    stripped = line.strip()
    return bool(re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?", stripped))



def _is_known_table_header(line: str, *, known_headers: tuple[tuple[str, ...], ...] | None = None) -> bool:
    headers = known_headers if known_headers is not None else DEFAULT_TABLE_HEADERS
    return tuple(_table_cells(line)) in headers



def _has_embedded_table_header(cells: list[str], *, known_headers: tuple[tuple[str, ...], ...] | None = None) -> bool:
    headers = known_headers if known_headers is not None else DEFAULT_TABLE_HEADERS
    for header in headers:
        header_len = len(header)
        for start in range(1, len(cells) - header_len + 1):
            if tuple(cells[start : start + header_len]) == header:
                return True
    return False



def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]



def _format_table_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"



def _trailing_empty_cell_count(cells: list[str]) -> int:
    count = 0
    for cell in reversed(cells):
        if cell:
            break
        count += 1
    return count



def _table_separator(header: str) -> str:
    cell_count = max(1, len(header.strip().strip("|").split("|")))
    return "|" + "|".join("---" for _ in range(cell_count)) + "|"



def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()



def _merge_soft_wrapped_lines(lines: list[str], *, structure_labels: tuple[str, ...] | None = None) -> list[str]:
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if result and stripped and _looks_like_forced_break(result[-1].strip(), stripped, structure_labels=structure_labels):
            result[-1] = result[-1].rstrip() + stripped
        else:
            result.append(line)
    return result



def _looks_like_forced_break(previous: str, current: str, *, structure_labels: tuple[str, ...] | None = None) -> bool:
    labels = structure_labels or DEFAULT_STRUCTURE_LABELS
    if not previous or not current:
        return False
    if previous.startswith("#") or current.startswith("#"):
        return False
    if current.startswith(labels) or previous.endswith("年版"):
        return False
    if re.match(r"^([一二三四五六七八九十]+、|\d+[.、])", current):
        return False
    return _looks_like_general_paragraph_fragment(previous, current)



def _looks_like_general_paragraph_fragment(previous: str, current: str) -> bool:
    if len(previous) < 8:
        return False
    if previous.startswith(("#", "|")) or current.startswith(("#", "|")):
        return False
    if current.startswith(("- ", "* ", "+ ")) or re.match(r"^\d+[.)、]\s*", current):
        return False
    if previous.endswith(tuple("。！？.!?：:；;”』】)）`")):
        return False
    if _looks_like_cjk_numbered_list_line(previous) and _looks_like_cjk_numbered_list_line(current):
        return False
    if len(current) < 8:
        if (
            len(previous) >= 18
            and re.search(r"[㐀-鿿]$", previous)
            and re.fullmatch(r"[㐀-鿿]{1,7}[，,。；;：:、]?", current)
        ):
            return True
        return False
    cjk_boundary = bool(re.search(r"[㐀-鿿]$", previous) and re.search(r"^[㐀-鿿]", current))
    comma_boundary = previous.endswith(("，", ",", "、"))
    if comma_boundary and _looks_like_short_cjk_verse_line(previous) and _looks_like_short_cjk_verse_line(current):
        return False
    if not cjk_boundary and not comma_boundary and len(previous) + len(current) < 80:
        return False
    return bool(re.search(r"[㐀-鿿A-Za-z0-9，,、]$", previous) and re.search(r"^[㐀-鿿A-Za-z0-9]", current))



def _looks_like_short_cjk_verse_line(value: str) -> bool:
    if len(value) > 28:
        return False
    if re.search(r"[A-Za-z0-9]", value):
        return False
    return bool(re.fullmatch(r"[㐀-鿿，、。！？；：”“‘’（）《》〈〉·]+", value))



def _looks_like_cjk_numbered_list_line(value: str) -> bool:
    return bool(re.match(r"^[一二三四五六七八九十百千〇零两]+、\S+", value))
