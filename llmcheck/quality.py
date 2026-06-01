from __future__ import annotations

import re
from typing import Any


BAD_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
MINERU_FLOWCHART_DETAILS_RE = re.compile(
    r"\n*<details>\s*<summary>\s*flowchart\s*</summary>(?P<body>.*?)</details>\s*",
    re.IGNORECASE | re.DOTALL,
)
MINERU_NOISE_DETAILS_RE = re.compile(
    r"\n*<details>\s*<summary>\s*(?:line|text_image|flowchart|natural_image|radar)\s*</summary>.*?</details>\s*",
    re.IGNORECASE | re.DOTALL,
)
STRUCTURE_LABELS = (
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
LOCAL_REPAIR_CATEGORIES = {"layout", "ocr_noise", "punctuation", "missing_text"}


def clean_markdown_text(text: str) -> str:
    cleaned = text.replace("\ufeff", "").replace("\x0c", "\n")
    cleaned = BAD_CONTROL_RE.sub("", cleaned)
    cleaned = re.sub(r"\r\n?", "\n", cleaned)
    cleaned = _transcribe_table_flowchart_details(cleaned)
    cleaned = MINERU_NOISE_DETAILS_RE.sub("\n\n", cleaned)
    cleaned = _html_table_tags_to_markdown(cleaned)
    cleaned = _split_local_structure_glue(cleaned)
    lines = [line.rstrip() for line in cleaned.split("\n")]
    lines = _normalize_markdown_tables(lines)
    lines = _merge_soft_wrapped_lines(lines)
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{4,}", "\n\n\n", cleaned).strip()
    return cleaned + "\n" if cleaned else ""


def repair_acceptance_locally(text: str, acceptance: dict[str, Any]) -> dict[str, Any]:
    issues = _acceptance_blocking_issues(acceptance)
    targeted = [issue for issue in issues if str(issue.get("category") or "") in LOCAL_REPAIR_CATEGORIES]
    if not targeted or acceptance.get("accepted") is True:
        return {"status": "skipped", "repaired": False, "issue_count": len(issues), "summary": "没有可本地定点修复的验收问题"}

    repaired_text = clean_markdown_text(text)
    changes: list[dict[str, str]] = []
    for issue in targeted:
        excerpt = str(issue.get("excerpt") or "").strip()
        if not excerpt:
            continue
        repaired_excerpt = clean_markdown_text(excerpt).strip()
        if repaired_excerpt and repaired_excerpt != excerpt and excerpt in repaired_text:
            repaired_text = repaired_text.replace(excerpt, repaired_excerpt, 1)
            changes.append(
                {
                    "location_hint": str(issue.get("location_hint") or ""),
                    "category": str(issue.get("category") or ""),
                    "before": excerpt[:160],
                    "after": repaired_excerpt[:160],
                }
            )

    repaired_text = clean_markdown_text(repaired_text)
    if repaired_text == text:
        return {
            "status": "not_repaired",
            "repaired": False,
            "issue_count": len(targeted),
            "summary": "本地规则未产生文本变化",
        }
    return {
        "status": "repaired",
        "repaired": True,
        "issue_count": len(targeted),
        "changes": changes,
        "repaired_text": repaired_text,
        "summary": f"已按验收失败项执行 {len(targeted)} 个本地定点修复规则",
    }


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


def _split_local_structure_glue(text: str) -> str:
    cleaned = text
    for label in STRUCTURE_LABELS:
        escaped = re.escape(label)
        cleaned = re.sub(rf"(?<!^)(?<!\n)(?<=[\u3400-\u9fffA-Za-z0-9）)])({escaped})", r"\n\1", cleaned)
    cleaned = re.sub(r"(?<=年版)(?=(?:[\u3400-\u9fff]{2,4})?《)", "\n", cleaned)
    return cleaned


def _html_table_tags_to_markdown(text: str) -> str:
    cleaned = re.sub(r"</(?:td|th)>\s*<(?:td|th)[^>]*>", " | ", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<tr[^>]*>\s*<(?:td|th)[^>]*>", "\n| ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</(?:td|th)>\s*</tr>", " |\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?(?:table|thead|tbody|tfoot)[^>]*>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?tr[^>]*>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?(?:td|th)[^>]*>", " | ", cleaned, flags=re.IGNORECASE)
    return cleaned


def _normalize_markdown_tables(lines: list[str]) -> list[str]:
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

    merged = _merge_table_continuation_rows(merged)
    normalized: list[str] = []
    for index, line in enumerate(merged):
        stripped = line.strip()
        if not stripped:
            previous = normalized[-1].strip() if normalized else ""
            following = _next_nonblank(merged, index + 1)
            if _is_table_row(previous) and _is_table_row(following):
                continue
        normalized.append(line)
        if _is_known_table_header(stripped):
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


def _merge_table_continuation_rows(lines: list[str]) -> list[str]:
    normalized: list[str] = []
    active_cols = 0
    for line in lines:
        stripped = line.strip()
        if _is_known_table_header(stripped):
            active_cols = len(_table_cells(stripped))
            normalized.append(line)
            continue
        if not _is_table_row(stripped) or _is_table_separator(stripped) or not active_cols:
            if stripped and not _is_table_row(stripped):
                active_cols = 0
            normalized.append(line)
            continue
        cells = _table_cells(stripped)
        if _has_embedded_table_header(cells):
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


def _is_known_table_header(line: str) -> bool:
    return tuple(_table_cells(line)) in _KNOWN_TABLE_HEADERS


def _has_embedded_table_header(cells: list[str]) -> bool:
    for header in _KNOWN_TABLE_HEADERS:
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


_KNOWN_TABLE_HEADERS = (
    ("分类", "药名", "功效", "主治", "用量"),
    ("类别", "药名", "功效", "主治", "用量"),
    ("方名", "出处", "组成", "功效", "主治", "用法"),
    ("方名", "来源", "药物组成", "功效", "主治", "制用法"),
)


def quality_hints(text: str) -> dict[str, Any]:
    chinese_chars = len(re.findall(r"[\u3400-\u9fff]", text))
    punctuation_count = sum(text.count(char) for char in "，。；：！？、")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    forced = forced_line_break_candidates(text, limit=8)
    low_punctuation_lines = [
        {
            "line_number": index,
            "length": len(line),
            "punctuation_count": sum(line.count(char) for char in "，。；：！？、"),
            "excerpt": line[:180],
        }
        for index, line in enumerate(text.splitlines(), start=1)
        if len(line.strip()) >= 180 and sum(line.count(char) for char in "，。；：！？、") <= 2
    ]
    return {
        "total_chars": len(text),
        "chinese_chars": chinese_chars,
        "line_count": len(lines),
        "punctuation_count": punctuation_count,
        "punctuation_density": round(punctuation_count / max(1, chinese_chars), 4),
        "forced_line_break_candidate_count": len(forced_line_break_candidates(text)),
        "forced_line_break_samples": forced,
        "long_low_punctuation_line_count": len(low_punctuation_lines),
        "long_low_punctuation_samples": low_punctuation_lines[:8],
    }


def quality_errors(text: str) -> list[str]:
    errors: list[str] = []
    if not text.strip():
        return ["empty_text"]
    readable_chars = len(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", text))
    if readable_chars < 20:
        errors.append("low_readable_chars")
    hints = quality_hints(text)
    if hints["punctuation_density"] < 0.006 and hints["chinese_chars"] >= 1000:
        errors.append("low_punctuation_density")
    if hints["forced_line_break_candidate_count"] >= 80:
        errors.append("forced_line_break_residue")
    if BAD_CONTROL_RE.search(text):
        errors.append("bad_control_characters")
    return errors


def forced_line_break_candidates(text: str, *, limit: int | None = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    lines = text.splitlines()
    for index in range(len(lines) - 1):
        previous = lines[index].strip()
        current = lines[index + 1].strip()
        if _looks_like_forced_break(previous, current):
            rows.append({"line_number": index + 1, "previous": previous[:120], "current": current[:120]})
            if limit is not None and len(rows) >= limit:
                break
    return rows


def safe_stem(value: str) -> str:
    stem = re.sub(r"[\\/:*?\"<>|\s]+", "_", value).strip("._")
    return stem or "document"


def _merge_soft_wrapped_lines(lines: list[str]) -> list[str]:
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if result and stripped and _looks_like_forced_break(result[-1].strip(), stripped):
            result[-1] = result[-1].rstrip() + stripped
        else:
            result.append(line)
    return result


def _looks_like_forced_break(previous: str, current: str) -> bool:
    if not previous or not current:
        return False
    if previous.startswith("#") or current.startswith("#"):
        return False
    if current.startswith(STRUCTURE_LABELS) or previous.endswith("年版"):
        return False
    if re.match(r"^([一二三四五六七八九十]+、|\d+[.、])", current):
        return False
    if previous.endswith(tuple("。！？：；”』】)）")):
        return False
    if len(previous) < 8 or len(current) < 8:
        return False
    return bool(re.search(r"[\u3400-\u9fff]$", previous) and re.search(r"^[\u3400-\u9fff]", current))
