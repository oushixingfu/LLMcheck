from __future__ import annotations

import hashlib
import re
from typing import Any

TOC_HEADING_RE = re.compile(r"^#{1,6}\s+(?:目\s*录|目錄|日\s*录|日錄)\s*$")


def normalize_document_structure(text: str) -> dict[str, Any]:
    return finalize_standard_document(text)



def finalize_standard_document(text: str) -> dict[str, Any]:
    from llmcheck.cleaning import clean_markdown_text

    before = clean_markdown_text(text)
    without_repeated, removed_lines = _remove_repeated_running_lines(before)
    without_preface_noise, removed_preface_lines = _remove_preface_ocr_noise(without_repeated)
    without_front_prefix, removed_front_prefix_lines = _remove_unstructured_front_matter_prefix(without_preface_noise)
    without_embedded_title_page, removed_embedded_title_page_lines = _remove_embedded_series_title_page_after_front_matter(without_front_prefix)
    without_front_catalog, removed_front_catalog_lines = _remove_series_catalog_before_toc(without_embedded_title_page)
    without_embedded_noise, removed_embedded_noise_lines = _remove_embedded_foreign_math_noise(without_front_catalog)
    without_trailing_metadata, removed_trailing_metadata_lines = _remove_trailing_publication_metadata_noise(without_embedded_noise)
    without_trailing_catalog, removed_trailing_catalog_lines = _remove_trailing_series_catalog_noise(without_trailing_metadata)
    without_trailing_outline, removed_trailing_outline_lines = _remove_trailing_short_outline_noise(without_trailing_catalog)
    without_duplicate_toc, removed_duplicate_toc_lines = _remove_trailing_duplicate_toc_outline(without_trailing_outline)
    with_normalized_toc, toc_changes = _normalize_toc_blocks(without_duplicate_toc)
    without_late_front_catalog, removed_late_front_catalog_lines = _remove_series_catalog_before_toc(with_normalized_toc)
    removed_front_catalog_lines = [*removed_front_catalog_lines, *removed_late_front_catalog_lines]
    without_duplicate_toc, removed_late_duplicate_toc_lines = _remove_trailing_duplicate_toc_outline(without_late_front_catalog)
    removed_duplicate_toc_lines = [*removed_duplicate_toc_lines, *removed_late_duplicate_toc_lines]
    finalized = clean_markdown_text(_normalize_heading_spacing(_normalize_heading_levels(without_duplicate_toc)))
    finalized = _normalize_heading_spacing(_normalize_heading_levels(finalized))
    finalized, removed_final_duplicate_toc_lines = _remove_trailing_duplicate_toc_outline(finalized)
    removed_duplicate_toc_lines = [*removed_duplicate_toc_lines, *removed_final_duplicate_toc_lines]
    finalized, final_toc_changes = _normalize_toc_blocks(finalized)
    toc_changes = [*toc_changes, *final_toc_changes]
    finalized = _normalize_heading_spacing(_normalize_heading_levels(finalized))
    finalized, removed_post_toc_duplicate_toc_lines = _remove_trailing_duplicate_toc_outline(finalized)
    removed_duplicate_toc_lines = [*removed_duplicate_toc_lines, *removed_post_toc_duplicate_toc_lines]
    finalized, removed_trailing_cover_ocr_lines = _remove_trailing_cover_ocr_heading_noise(finalized)
    changes: list[dict[str, object]] = []
    if removed_lines:
        changes.append({"kind": "removed_repeated_lines", "lines": removed_lines})
    if removed_preface_lines:
        changes.append({"kind": "removed_preface_ocr_noise", "lines": removed_preface_lines})
    if removed_front_prefix_lines:
        changes.append({"kind": "removed_unstructured_front_matter_prefix", "lines": removed_front_prefix_lines})
    if removed_embedded_title_page_lines:
        changes.append({"kind": "removed_embedded_series_title_page_after_front_matter", "lines": removed_embedded_title_page_lines})
    if removed_front_catalog_lines:
        changes.append({"kind": "removed_series_catalog_before_toc", "lines": removed_front_catalog_lines})
    if removed_embedded_noise_lines:
        changes.append({"kind": "removed_embedded_foreign_math_noise", "lines": removed_embedded_noise_lines})
    if removed_trailing_metadata_lines:
        changes.append({"kind": "removed_trailing_publication_metadata_noise", "lines": removed_trailing_metadata_lines})
    if removed_trailing_catalog_lines:
        changes.append({"kind": "removed_trailing_series_catalog_noise", "lines": removed_trailing_catalog_lines})
    if removed_trailing_outline_lines:
        changes.append({"kind": "removed_trailing_short_outline_noise", "lines": removed_trailing_outline_lines})
    if removed_duplicate_toc_lines:
        changes.append({"kind": "removed_trailing_duplicate_toc_outline", "lines": removed_duplicate_toc_lines})
    if removed_trailing_cover_ocr_lines:
        changes.append({"kind": "removed_trailing_cover_ocr_heading_noise", "lines": removed_trailing_cover_ocr_lines})
    if toc_changes:
        changes.append({"kind": "normalized_table_of_contents", "entries": toc_changes})
    if finalized != before and not changes:
        changes.append({"kind": "normalized_spacing"})
    elif finalized != before and changes:
        changes.append({"kind": "normalized_spacing"})
    return {
        "status": "finalized",
        "finalized": finalized != before,
        "input_sha256": _text_sha256(before),
        "output_sha256": _text_sha256(finalized),
        "changes": changes,
        "text": finalized,
    }



def _remove_repeated_running_lines(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    counts: dict[str, int] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if 3 <= len(line) <= 40 and not line.startswith(("#", "|", "-", "*")):
            counts[line] = counts.get(line, 0) + 1
    repeated = {line for line, count in counts.items() if count >= 3}
    output = [raw_line for raw_line in lines if raw_line.strip() not in repeated]
    return "\n".join(output) + ("\n" if output else ""), sorted(repeated)


def _normalize_toc_blocks(text: str) -> tuple[str, list[str]]:
    source_lines = text.splitlines()
    lines: list[str] = []
    changed_entries: list[str] = []
    in_toc = False
    unheaded_toc = False
    for index, raw_line in enumerate(source_lines):
        stripped = raw_line.strip()
        if _is_toc_heading(stripped):
            in_toc = True
            unheaded_toc = False
            if any(_is_toc_heading(line.strip()) for line in lines):
                continue
            lines.append("# 目录")
            continue
        if not in_toc:
            content = _heading_content(stripped)
            if _looks_like_toc_entry(content) and _next_lines_have_toc_entries(source_lines, index + 1):
                in_toc = True
                unheaded_toc = True
                if not any(_is_toc_heading(line.strip()) for line in lines):
                    lines.append("# 目录")
                lines.append(f"- {content}")
                changed_entries.append(content)
                continue
        if in_toc:
            if not stripped:
                if lines and _looks_like_incomplete_toc_output_entry(lines[-1]):
                    continue
                lines.append(raw_line)
                continue
            if _is_markdown_image(stripped):
                in_toc = False
                unheaded_toc = False
                lines.append(raw_line)
                continue
            bullet_match = re.match(r"^[-*]\s+(.+?)\s*$", stripped)
            if bullet_match and _looks_like_toc_entry(bullet_match.group(1).strip()):
                lines.append(f"- {bullet_match.group(1).strip()}")
                continue
            content = _heading_content(stripped)
            if _looks_like_toc_entry(content):
                entry = f"- {content}"
                if lines and _looks_like_incomplete_toc_output_entry(lines[-1]):
                    lines[-1] = f"{lines[-1].rstrip()}{content}"
                    changed_entries.append(content)
                    continue
                lines.append(entry)
                if entry != raw_line:
                    changed_entries.append(content)
                continue
            if unheaded_toc:
                next_content = _next_nonblank_heading_content(source_lines, index + 1)
                if _looks_like_toc_page_marker(next_content):
                    lines.append(f"- {content}")
                    changed_entries.append(content)
                    continue
                in_toc = False
                unheaded_toc = False
                lines.append(raw_line)
                continue
            if re.match(r"^#{1,6}\s+\S", stripped):
                if _is_major_document_heading(content):
                    in_toc = False
                    unheaded_toc = False
                    lines.append(raw_line)
                    continue
                lines.append(f"- {content}")
                changed_entries.append(content)
                continue
            lines.append(f"- {stripped}")
            changed_entries.append(stripped)
            continue
        lines.append(raw_line)
    output = "\n".join(lines)
    return output + ("\n" if output else ""), changed_entries


def _looks_like_toc_entry(content: str) -> bool:
    normalized = re.sub(r"\s+", " ", content).strip()
    return bool(
        re.search(r"[（(]\d+[）)]$", normalized)
        or re.search(r"[／/]\s*\d+$", normalized)
        or re.search(r"[\s.．…·]{1,}\d{1,4}$", normalized)
    )


def _next_lines_have_toc_entries(lines: list[str], start: int) -> bool:
    entries = 0
    inspected = 0
    for raw_line in lines[start:]:
        stripped = raw_line.strip()
        if not stripped:
            continue
        inspected += 1
        if _is_markdown_image(stripped):
            return False
        if _looks_like_toc_entry(_heading_content(stripped)):
            entries += 1
        if entries >= 2:
            return True
        if inspected >= 8:
            return False
    return False


def _next_nonblank_heading_content(lines: list[str], start: int) -> str:
    for raw_line in lines[start:]:
        stripped = raw_line.strip()
        if stripped:
            return _heading_content(stripped)
    return ""


def _is_toc_heading(line: str) -> bool:
    return bool(TOC_HEADING_RE.match(line.strip()))


def _looks_like_incomplete_toc_output_entry(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("- "):
        return False
    return not _looks_like_toc_entry(stripped[2:].strip())


def _looks_like_toc_page_marker(content: str) -> bool:
    normalized = re.sub(r"\s+", " ", content).strip()
    return bool(
        re.fullmatch(r"[（(]\d{1,4}[）)]", normalized)
        or re.fullmatch(r"[／/]\s*\d{1,4}", normalized)
    )


def _remove_preface_ocr_noise(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    lines, cover_removed = _remove_cover_noise_prefix(lines)
    lines, image_removed = _remove_image_noise_before_summary(lines)
    output = "\n".join(lines)
    return output + ("\n" if output else ""), [*cover_removed, *image_removed]


def _remove_embedded_foreign_math_noise(text: str) -> tuple[str, list[str]]:
    source_lines = text.splitlines()
    output: list[str] = []
    removed: list[str] = []
    index = 0
    while index < len(source_lines):
        line = source_lines[index]
        if not _looks_like_embedded_foreign_math_noise(line):
            output.append(line)
            index += 1
            continue

        if output and not output[-1].strip():
            output.pop()
        while index < len(source_lines):
            current = source_lines[index]
            if _looks_like_embedded_foreign_math_noise(current):
                removed.append(current.strip())
                index += 1
                continue
            if not current.strip() and _next_nonblank_line_looks_like_noise(source_lines, index + 1):
                index += 1
                continue
            break
        if index < len(source_lines) and not source_lines[index].strip():
            index += 1
        continue

    output_text = "\n".join(output)
    return output_text + ("\n" if output_text else ""), removed


def _remove_unstructured_front_matter_prefix(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    reliable_anchor_index = _find_first_line(lines, _looks_like_reliable_front_matter_heading)
    if reliable_anchor_index is not None and 0 < reliable_anchor_index <= 120:
        prefix = lines[:reliable_anchor_index]
        if (
            not _has_content_heading_before_front_matter_anchor(prefix)
            and _looks_like_unstructured_front_matter_prefix(prefix)
        ):
            output = lines[reliable_anchor_index:]
            removed = [line.strip() for line in prefix if line.strip()]
            output_text = "\n".join(output)
            return output_text + ("\n" if output_text else ""), removed

    anchor_index = _find_first_line(lines, _looks_like_usable_front_matter_anchor)
    if anchor_index is None or anchor_index <= 0 or anchor_index > 120:
        return text, []
    prefix = lines[:anchor_index]
    if not _looks_like_unstructured_front_matter_prefix(prefix):
        return text, []
    output = lines[anchor_index:]
    removed = [line.strip() for line in prefix if line.strip()]
    output_text = "\n".join(output)
    return output_text + ("\n" if output_text else ""), removed


def _remove_embedded_series_title_page_after_front_matter(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    for index, raw_line in enumerate(lines):
        if _heading_content(raw_line) != "中国百年百名中医临床家丛书":
            continue
        if index == 0 or index > 220:
            continue
        has_previous_front_matter = any(
            _looks_like_reliable_front_matter_heading(line)
            for line in lines[max(0, index - 120) : index]
        )
        if not has_previous_front_matter:
            continue
        next_front_matter = _find_next_line_index(
            lines,
            _looks_like_reliable_front_matter_heading,
            start=index + 1,
            limit=min(len(lines), index + 160),
        )
        if next_front_matter is None:
            continue
        window = [line.strip() for line in lines[index:next_front_matter] if line.strip()]
        has_catalog_label = any("按姓氏笔画排列" in _heading_content(line) for line in window)
        has_name_list = any(len(line) >= 120 and re.search(r"[\u3400-\u9fff]", line) for line in window)
        has_name_table = sum(1 for line in lines[index:next_front_matter] if _looks_like_series_catalog_table_row(line.strip())) >= 3
        if has_catalog_label and (has_name_list or has_name_table):
            continue

        cursor = index
        removed: list[str] = []
        while cursor < next_front_matter:
            stripped = lines[cursor].strip()
            if not stripped:
                cursor += 1
                continue
            if _looks_like_front_title_page_line(stripped) or _is_markdown_image(stripped):
                removed.append(stripped)
                cursor += 1
                continue
            break

        if len(removed) < 2:
            continue
        output = [*lines[:index], *lines[cursor:]]
        while index > 0 and index < len(output) and not output[index - 1].strip() and not output[index].strip():
            output.pop(index)
        output_text = "\n".join(output)
        return output_text + ("\n" if output_text else ""), removed
    return text, []


def _remove_series_catalog_before_toc(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    for index, raw_line in enumerate(lines):
        if _heading_content(raw_line) != "中国百年百名中医临床家丛书":
            continue
        if index > 160:
            continue
        toc_index = _find_next_line_index(lines, _is_toc_heading, start=index + 1, limit=index + 180)
        if toc_index is None:
            continue
        window = [line.strip() for line in lines[index:toc_index] if line.strip()]
        has_catalog_label = any("按姓氏笔画排列" in _heading_content(line) for line in window)
        has_name_list = any(len(line) >= 120 and re.search(r"[\u3400-\u9fff]", line) for line in window)
        has_name_table = sum(1 for line in lines[index:toc_index] if _looks_like_series_catalog_table_row(line.strip())) >= 3
        if not (has_catalog_label and (has_name_list or has_name_table)):
            continue
        output = [*lines[:index], *lines[toc_index:]]
        while index > 0 and index < len(output) and not output[index - 1].strip() and not output[index].strip():
            output.pop(index)
        removed = [line.strip() for line in lines[index:toc_index] if line.strip()]
        output_text = "\n".join(output)
        return output_text + ("\n" if output_text else ""), removed
    return text, []


def _remove_trailing_series_catalog_noise(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    for index, raw_line in enumerate(lines):
        heading = _heading_content(raw_line)
        if heading != "中国百年百名中医临床家丛书" and not re.fullmatch(
            r"中国百年百名中医临床家丛书[（(]按姓氏笔画排列[）)]",
            heading,
        ):
            continue
        if index < len(lines) // 2:
            continue
        window = [line.strip() for line in lines[index : index + 10] if line.strip()]
        has_catalog_label = any("按姓氏笔画排列" in _heading_content(line) for line in window)
        has_name_list = any(len(line) >= 120 and re.search(r"[\u3400-\u9fff]", line) for line in window)
        has_name_table = sum(1 for line in lines[index : index + 40] if _looks_like_series_catalog_table_row(line.strip())) >= 5
        has_vertical_name_list = sum(
            1 for line in lines[index : index + 120] if _looks_like_series_catalog_short_name_line(line.strip())
        ) >= 20
        has_name_list = has_name_list or has_name_table or has_vertical_name_list
        has_short_title_page_tail = _looks_like_trailing_series_title_page_block(lines[index:])
        if not ((has_catalog_label and has_name_list) or has_short_title_page_tail):
            continue
        output = lines[:index]
        while output and not output[-1].strip():
            output.pop()
        removed = [line.strip() for line in lines[index:] if line.strip()]
        output_text = "\n".join(output)
        return output_text + ("\n" if output_text else ""), removed
    return text, []


def _remove_trailing_publication_metadata_noise(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    for index, raw_line in enumerate(lines):
        content = _heading_content(raw_line)
        if not re.fullmatch(r"图书在版编目[（(]CIP[）)]数据", content, flags=re.IGNORECASE):
            continue
        if index < len(lines) // 2:
            continue
        tail = [line.strip() for line in lines[index:] if line.strip()]
        metadata_hits = sum(1 for line in tail[:40] if _looks_like_front_matter_metadata_line(line))
        has_cover_or_title = any(
            _looks_like_cover_ocr_heading_content(_heading_content(line))
            or _looks_like_front_title_page_line(line)
            or _is_markdown_image(line)
            for line in tail[:80]
        )
        has_late_toc = any(TOC_HEADING_RE.match(line) for line in tail[:120])
        if metadata_hits < 4 or not (has_cover_or_title or has_late_toc):
            continue
        output = lines[:index]
        while output and not output[-1].strip():
            output.pop()
        removed = [line.strip() for line in lines[index:] if line.strip()]
        output_text = "\n".join(output)
        return output_text + ("\n" if output_text else ""), removed
    return text, []


def _looks_like_series_catalog_table_row(line: str) -> bool:
    if not line.startswith("|") or not line.endswith("|"):
        return False
    if re.fullmatch(r"\|(?:\s*:?-{3,}:?\s*\|)+", line):
        return False
    cells = [cell.strip() for cell in line.strip("|").split("|")]
    name_cells = [cell for cell in cells if re.search(r"[\u3400-\u9fff]", cell) and len(cell) <= 16]
    return len(name_cells) >= 2


def _looks_like_series_catalog_short_name_line(line: str) -> bool:
    content = _heading_content(line)
    if not content or _is_markdown_image(line):
        return False
    if "中国百年百名中医临床家丛书" in content or "按姓氏笔画排列" in content:
        return False
    if re.search(r"[。！？!?；;：:（）()0-9]", content):
        return False
    compact = re.sub(r"\s+", "", content)
    return bool(2 <= len(compact) <= 12 and re.fullmatch(r"[\u3400-\u9fff·]+", compact))


def _looks_like_trailing_series_title_page_block(lines: list[str]) -> bool:
    nonblank = [line.strip() for line in lines if line.strip()]
    if not 2 <= len(nonblank) <= 12:
        return False
    if _heading_content(nonblank[0]) != "中国百年百名中医临床家丛书":
        return False
    tail = nonblank[1:]
    return any(_looks_like_front_title_page_line(line) for line in tail) and all(
        _looks_like_front_title_page_line(line) or _is_markdown_image(line) for line in tail
    )


def _remove_trailing_short_outline_noise(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    allowed = {"版权页", "前言", "目录", "目錄", "正文", "跋", "后记", "後記", "附录", "附錄"}
    for index, raw_line in enumerate(lines):
        content = _heading_content(raw_line)
        if content != "版权页":
            continue
        if index < len(lines) // 2:
            continue
        tail = [line.strip() for line in lines[index:] if line.strip()]
        if not 3 <= len(tail) <= 8:
            continue
        tail_contents = [_heading_content(line) for line in tail]
        if not any(content in {"目录", "目錄"} for content in tail_contents[1:]):
            continue
        if any(content not in allowed for content in tail_contents):
            continue
        output = lines[:index]
        while output and not output[-1].strip():
            output.pop()
        removed = [line.strip() for line in lines[index:] if line.strip()]
        output_text = "\n".join(output)
        return output_text + ("\n" if output_text else ""), removed
    return text, []


def _remove_trailing_duplicate_toc_outline(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    toc_indexes = [index for index, line in enumerate(lines) if _is_toc_heading(line.strip())]
    if len(toc_indexes) < 2:
        return text, []
    index = toc_indexes[-1]
    if index < len(lines) // 2:
        return text, []
    tail = [line.strip() for line in lines[index:] if line.strip()]
    if len(tail) < 6 or len(tail) > 180:
        return text, []
    heading_count = sum(1 for line in tail if re.match(r"^#{1,6}\s+\S", line))
    page_entry_count = sum(1 for line in tail if _looks_like_toc_entry(_heading_content(line)))
    short_outline_count = sum(1 for line in tail if _looks_like_short_toc_outline_line(_heading_content(line)))
    heading_dense_outline = heading_count >= max(4, int(len(tail) * 0.6))
    if heading_count < 4:
        return text, []
    if not any(_heading_content(line) in {"医家小传", "专病论治"} for line in tail):
        return text, []
    if page_entry_count == 0 and not heading_dense_outline and short_outline_count < max(4, len(tail) // 3):
        return text, []
    output = lines[:index]
    while output and not output[-1].strip():
        output.pop()
    removed = [line.strip() for line in lines[index:] if line.strip()]
    output_text = "\n".join(output)
    return output_text + ("\n" if output_text else ""), removed


def _remove_trailing_cover_ocr_heading_noise(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    start = end
    saw_cover_heading = False
    while start > 0:
        stripped = lines[start - 1].strip()
        if not stripped:
            start -= 1
            continue
        content = _heading_content(stripped)
        if _looks_like_cover_ocr_heading_content(content):
            saw_cover_heading = True
            start -= 1
            continue
        if _looks_like_trailing_cover_tail_line(stripped):
            start -= 1
            continue
        break
    if not saw_cover_heading:
        return text, []
    output = lines[:start]
    while output and not output[-1].strip():
        output.pop()
    removed = [line.strip() for line in lines[start:end] if line.strip()]
    output_text = "\n".join(output)
    return output_text + ("\n" if output_text else ""), removed


def _looks_like_trailing_cover_tail_line(line: str) -> bool:
    stripped = line.strip()
    if _is_markdown_image(stripped):
        return True
    if re.search(r"^(?:定价|书号|ISBN|网址|读者服务部电话|社长热线|如有质量问题)", stripped, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"[￥¥]\s*\d*(?:\.\d+)?", stripped):
        return True
    if re.search(r"^\d+(?:\.\d+)?\s*元$", stripped):
        return True
    if re.search(r"(?:版权专有|侵权必究|出版部调换)", stripped):
        return True
    return False


def _looks_like_short_toc_outline_line(content: str) -> bool:
    if not content or len(content) > 48:
        return False
    if re.search(r"[。！？!?；;，,]", content):
        return False
    return bool(re.search(r"[\u3400-\u9fff]", content))


def _find_next_line_index(lines: list[str], predicate: Any, *, start: int, limit: int) -> int | None:
    for index in range(start, min(len(lines), limit)):
        if predicate(lines[index]):
            return index
    return None


def _next_nonblank_line_looks_like_noise(lines: list[str], start: int) -> bool:
    for index in range(start, len(lines)):
        if not lines[index].strip():
            continue
        return _looks_like_embedded_foreign_math_noise(lines[index])
    return False


def _looks_like_embedded_foreign_math_noise(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < 80:
        return False
    if re.search(r"[\u3400-\u9fff]", stripped):
        return False
    if not re.match(r"^Theorem\s+\d+(?:\.\d+)*\.", stripped):
        return False
    markers = ("finite field", "F(x)", "prime ideal", "\\in")
    return sum(1 for marker in markers if marker in stripped) >= 3


def _remove_cover_noise_prefix(lines: list[str]) -> tuple[list[str], list[str]]:
    first_series_index = _find_first_line(lines, lambda line: line.strip() == "# 中国百年百名中医临床家丛书")
    first_front_matter_index = _find_first_line(lines, _looks_like_reliable_front_matter_heading)
    anchor_candidates = [index for index in (first_series_index, first_front_matter_index) if index is not None]
    anchor_index = min(anchor_candidates) if anchor_candidates else None
    if anchor_index is None or anchor_index <= 0 or anchor_index > 80:
        return lines, []
    prefix = lines[:anchor_index]
    if not _looks_like_cover_noise_prefix(prefix):
        return lines, []
    removed = [line.strip() for line in prefix if line.strip()]
    return lines[anchor_index:], removed


def _looks_like_reliable_front_matter_heading(line: str) -> bool:
    content = _heading_content(line)
    return content in {"出版者的话", "内容提要", "目录", "目錄"}


def _looks_like_usable_front_matter_anchor(line: str) -> bool:
    match = re.match(r"^#{1,6}\s+(.+?)\s*$", line.strip())
    if not match:
        return False
    content = match.group(1).strip()
    if _looks_like_standalone_parenthetical_marker(content):
        return False
    if _looks_like_cover_ocr_heading_content(content):
        return False
    if _looks_like_decorative_heading_content(content) or _looks_like_fragment_heading_content(content):
        return False
    if _looks_like_dosage_heading_content(content):
        return False
    return True


def _has_content_heading_before_front_matter_anchor(lines: list[str]) -> bool:
    for line in lines:
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", line.strip())
        if not match:
            continue
        content = match.group(1).strip()
        if _looks_like_front_title_page_line(content):
            continue
        if _looks_like_cover_ocr_heading_content(content):
            continue
        if _looks_like_decorative_heading_content(content) or _looks_like_fragment_heading_content(content):
            continue
        return True
    return False


def _remove_image_noise_before_summary(lines: list[str]) -> tuple[list[str], list[str]]:
    output: list[str] = []
    removed: list[str] = []
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        stripped = raw_line.strip()
        output.append(raw_line)
        if not _is_markdown_image(stripped):
            index += 1
            continue
        next_major_index = _find_next_major_heading(lines, start=index + 1)
        if next_major_index is None or next_major_index - index > 140:
            index += 1
            continue
        segment = lines[index + 1 : next_major_index]
        filtered, segment_removed = _filter_image_caption_segment(segment)
        output.extend(filtered)
        removed.extend(segment_removed)
        index = next_major_index
    return output, removed


def _filter_image_caption_segment(lines: list[str]) -> tuple[list[str], list[str]]:
    output: list[str] = []
    removed: list[str] = []
    kept_caption = False
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            if output and output[-1].strip():
                output.append(raw_line)
            continue
        if not kept_caption and _looks_like_image_caption(stripped):
            output.append(raw_line)
            kept_caption = True
            continue
        if _looks_like_preface_image_noise(stripped):
            removed.append(stripped)
            continue
        output.append(raw_line)
    while output and not output[-1].strip():
        output.pop()
    if output:
        output.append("")
    return output, removed


def _find_first_line(lines: list[str], predicate: Any) -> int | None:
    for index, line in enumerate(lines):
        if predicate(line):
            return index
    return None


def _find_next_major_heading(lines: list[str], *, start: int) -> int | None:
    for index in range(start, len(lines)):
        stripped = lines[index].strip()
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", stripped)
        if match and _is_major_document_heading(match.group(1).strip()):
            return index
    return None


def _looks_like_cover_noise_prefix(lines: list[str]) -> bool:
    nonblank = [line.strip() for line in lines if line.strip()]
    if len(nonblank) < 3:
        return False
    short_fragments = sum(1 for line in nonblank if len(_heading_content(line)) <= 2)
    known_cover_ocr = any(_looks_like_cover_ocr_heading_content(_heading_content(line)) for line in nonblank)
    return short_fragments >= 3 or known_cover_ocr


def _looks_like_unstructured_front_matter_prefix(lines: list[str]) -> bool:
    nonblank = [line.strip() for line in lines if line.strip()]
    if not nonblank:
        return False
    metadata_hits = sum(1 for line in nonblank if _looks_like_front_matter_metadata_line(line))
    has_image = any(_is_markdown_image(line) for line in nonblank)
    title_page_hits = sum(1 for line in nonblank if _looks_like_front_title_page_line(line))
    short_fragments = sum(1 for line in nonblank if len(_heading_content(line)) <= 2)
    return bool(
        metadata_hits >= 1
        or has_image
        or title_page_hits >= 1
        or short_fragments >= 3
    )


def _looks_like_front_matter_metadata_line(line: str) -> bool:
    content = _heading_content(line)
    if re.search(r"ISBN|CIP|R·\d+|/R[.·]\d+", content, flags=re.IGNORECASE):
        return True
    return bool(
        re.search(
            r"(?:中国版本图书馆|发行者|印刷者|经销者|开本|字数|印张|版次|印次|册数|书号|定价|邮编|电话|邮购|质量问题|出版社|HTTP)",
            content,
            flags=re.IGNORECASE,
        )
    )


def _looks_like_front_title_page_line(line: str) -> bool:
    content = _heading_content(line)
    if _looks_like_cover_ocr_heading_content(content):
        return True
    if re.search(r"(?:主编|执行主编|编委|编著|整理|作者|责任编辑|封面设计|丛编项|图书|网店|购买)", content):
        return True
    if re.search(r"(?:大学|学院|医院|研究所|教研室|出版社|编辑部).{0,12}编$", content):
        return True
    if "中国百年百名中医临床家丛书" in content:
        return True
    if len(content) <= 12 and not re.search(r"[，。；：！？、,.!?;:0-9]", content):
        return True
    return False


def _looks_like_cover_ocr_heading_content(content: str) -> bool:
    return content in {"临中床医", "临中家医", "临中家医床", "临家中床医", "临中", "家医"}


def _is_markdown_image(line: str) -> bool:
    return bool(re.match(r"^!\[[^\]]*]\([^)]+\)\s*$", line))


def _looks_like_image_caption(line: str) -> bool:
    content = _heading_content(line)
    return bool(re.search(r"(?:教授|照片|手迹|题字|像)$", content))


def _looks_like_preface_image_noise(line: str) -> bool:
    content = _heading_content(line)
    if not content or _is_major_document_heading(content):
        return False
    if re.search(r"\\(?:frac|sqrt)|\{[A-Za-z0-9_.]+}", content):
        return True
    if line.strip().startswith("#"):
        compact = re.sub(r"\s+", "", content)
        has_cjk = bool(re.search(r"[\u3400-\u9fff]", compact))
        has_digit = bool(re.search(r"\d", compact))
        has_date_marker = bool(re.search(r"[年月日号期卷页]", compact))
        if re.fullmatch(r"\d{1,3}", compact):
            return True
        if len(compact) <= 40 and re.search(r"\\[A-Za-z]+", compact) and not has_cjk:
            return True
        if len(compact) <= 10 and has_cjk and has_digit and not has_date_marker:
            return True
    if len(content) <= 14 and not re.search(r"[，。；：！？、,.!?;:0-9]", content):
        return True
    return False


def _heading_content(line: str) -> str:
    match = re.match(r"^#{1,6}\s+(.+?)\s*$", line.strip())
    return match.group(1).strip() if match else line.strip()



def _normalize_heading_spacing(text: str) -> str:
    lines = text.split("\n")
    normalized: list[str] = []
    in_fence = False

    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            if not in_fence and normalized and normalized[-1] != "":
                normalized.append("")
            normalized.append(raw_line)
            in_fence = not in_fence
            continue
        if in_fence:
            normalized.append(raw_line)
            continue
        if re.match(r"^#{1,6}\s+\S", stripped):
            if normalized and normalized[-1] != "":
                normalized.append("")
            normalized.append(stripped)
            next_line = lines[index + 1] if index + 1 < len(lines) else None
            if next_line is not None and next_line.strip() != "":
                normalized.append("")
            continue
        normalized.append(raw_line)

    return "\n".join(normalized)



def _normalize_heading_levels(text: str) -> str:
    lines: list[str] = []
    in_fence = False
    previous_heading_level = 0
    for raw_line in text.split("\n"):
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            lines.append(raw_line)
            in_fence = not in_fence
            continue
        if in_fence:
            lines.append(raw_line)
            continue
        if re.fullmatch(r"#{1,6}", stripped):
            continue
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", raw_line)
        if match:
            content = match.group(2).strip()
            if re.fullmatch(r"#{1,6}", content):
                continue
            nested_heading = re.match(r"^#{1,6}\s*(.+?)\s*$", content)
            if nested_heading:
                content = nested_heading.group(1).strip()
            if not content:
                lines.append(raw_line)
                continue
            if _looks_like_standalone_parenthetical_marker(content):
                lines.append(content)
                continue
            if _looks_like_decorative_heading_content(content) or _looks_like_fragment_heading_content(content):
                continue
            if _looks_like_dosage_heading_content(content):
                lines.append(content)
                continue
            level = _standard_heading_level(content, default=len(match.group(1)))
            level = _cap_heading_level_jump(level, previous_heading_level)
            previous_heading_level = level
            lines.append(f"{'#' * level} {content}")
            continue

        content = raw_line.strip()
        missing_space = re.match(r"^#{1,6}(\S.+?)\s*$", content)
        if missing_space:
            repaired_content = missing_space.group(1).strip()
            if _looks_like_inline_heading_without_marker_space(repaired_content):
                level = _standard_heading_level(repaired_content, default=1)
                level = _cap_heading_level_jump(level, previous_heading_level)
                previous_heading_level = level
                lines.append(f"{'#' * level} {repaired_content}")
            else:
                lines.append(repaired_content)
            continue
        level = _bare_heading_level(content)
        if level == 0:
            lines.append(raw_line)
            continue
        level = _cap_heading_level_jump(level, previous_heading_level)
        previous_heading_level = level
        lines.append(f"{'#' * level} {content}")
    return "\n".join(lines)


def _cap_heading_level_jump(level: int, previous_level: int) -> int:
    level = min(max(level, 1), 6)
    if previous_level <= 0:
        return 1
    if level > previous_level + 1:
        return previous_level + 1
    return level



def _bare_heading_level(content: str) -> int:
    if not content or content.startswith(("|", "-", "*", ">")):
        return 0
    normalized = re.sub(r"\s+", " ", content).strip()
    if _looks_like_standalone_parenthetical_marker(normalized):
        return 0
    if _looks_like_dosage_heading_content(normalized):
        return 0
    if _is_major_document_heading(normalized):
        return 1
    if _looks_like_toc_heading(normalized):
        return _standard_heading_level(normalized, default=2)
    if re.match(r"^(?:[一二三四五六七八九十百]+[、.．]\s*.+|附：.+)$", normalized) and _is_short_heading_text(normalized, limit=48):
        return 2
    if re.match(r"^(?:\d+[.、]\s*.+|[（(]\d+[）)]\s*.+|[（(][一二三四五六七八九十]+[）)]\s*.+|【.+】)$", normalized) and _is_short_heading_text(normalized, limit=36):
        return 3
    return 0



def _looks_like_inline_heading_without_marker_space(content: str) -> bool:
    normalized = re.sub(r"\s+", " ", content).strip()
    if not _is_short_heading_text(normalized, limit=36):
        return False
    return bool(
        _is_major_document_heading(normalized)
        or _looks_like_toc_heading(normalized)
        or re.match(
            r"^(?:第[一二三四五六七八九十百0-9]+[章节]|[一二三四五六七八九十百]+[、.．]\s*.+|\d+[.、]\s*.+)",
            normalized,
        )
        or re.match(r"^(?:[（(]\d+[）)]\s*.+|[（(][一二三四五六七八九十]+[）)]\s*.+|【.+】)$", normalized)
    )


def _standard_heading_level(content: str, *, default: int) -> int:
    normalized = re.sub(r"\s+", " ", content).strip()
    if not normalized or normalized in {"-", "※※※※※"}:
        return min(max(default, 1), 6)
    if _is_major_document_heading(normalized):
        return 1
    if re.match(r"^[一二三四五六七八九十百]+[、.．]\s*.+", normalized):
        return 2
    if re.match(r"^(?:\d+[.、]\s*.+|[（(]\d+[）)]\s*.+|[（(][一二三四五六七八九十]+[）)]\s*.+|【.+】)$", normalized):
        return 3
    if default == 1:
        return 1
    if 2 <= len(normalized) <= 28 and not normalized.endswith(tuple("。！？.!?；;：:，,")):
        return 2
    return min(max(default, 1), 6)


def _looks_like_decorative_heading_content(content: str) -> bool:
    normalized = re.sub(r"\s+", "", content)
    return bool(normalized and re.fullmatch(r"[※＊*★☆·•\-—_=]+", normalized))


def _looks_like_fragment_heading_content(content: str) -> bool:
    normalized = re.sub(r"\s+", " ", content).strip()
    if _is_major_document_heading(normalized):
        return False
    return bool(len(normalized) == 1 and re.fullmatch(r"[\u3400-\u9fffA-Za-z]", normalized))


def _looks_like_dosage_heading_content(content: str) -> bool:
    normalized = re.sub(r"\s+", " ", content).strip()
    dosage = r"\d+(?:\.\d+)?\s*(?:g|克|kg|mg|ml|mL|帖|付|枚|片|丸|粒|钱|分)"
    return len(re.findall(dosage, normalized, flags=re.IGNORECASE)) >= 2


def _looks_like_standalone_parenthetical_marker(content: str) -> bool:
    normalized = re.sub(r"\s+", " ", content).strip()
    return bool(re.fullmatch(r"[（(]\d{1,4}[）)]", normalized))



def _looks_like_toc_heading(content: str) -> bool:
    return bool(re.search(r"[.．…·\s]*[（(]\d+[）)]$", content)) and _is_short_heading_text(content, limit=80)



def _is_short_heading_text(content: str, *, limit: int) -> bool:
    if len(content) > limit:
        return False
    if any(token in content for token in ("病历号", "患者", "男，", "女，", "克 ", "毫升")):
        return False
    return not content.endswith(tuple("。！？.!?；;，,"))



def _is_major_document_heading(content: str) -> bool:
    base = re.sub(r"\s*[（(]\d+[）)]\s*$", "", content).strip()
    return base in {
        "目录",
        "出版者的话",
        "内容提要",
        "编写说明",
        "序",
        "前言",
        "医家小传",
        "专病论治",
        "临证特色",
        "临证经验",
        "学术思想",
        "年谱",
        "附录",
        "后记",
        "常见病辨证施针",
        "辨证选穴施针精要",
        "临床中医",
    }



def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
