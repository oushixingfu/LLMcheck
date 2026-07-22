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
    without_vertical_echo, removed_vertical_echo_lines = _remove_vertical_heading_ocr_echo(without_repeated)
    without_preface_noise, removed_preface_lines = _remove_preface_ocr_noise(without_vertical_echo)
    without_front_prefix, removed_front_prefix_lines = _remove_unstructured_front_matter_prefix(without_preface_noise)
    without_embedded_title_page, removed_embedded_title_page_lines = _remove_embedded_series_title_page_after_front_matter(without_front_prefix)
    without_front_imprint, removed_front_imprint_lines = _remove_front_section_imprint_noise(without_embedded_title_page)
    without_front_catalog, removed_front_catalog_lines = _remove_series_catalog_before_toc(without_front_imprint)
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
    # Second pass: list-prefixed series headers may appear after TOC rewrites.
    finalized, removed_late_repeated_lines = _remove_repeated_running_lines(finalized)
    if removed_late_repeated_lines:
        removed_lines = sorted(set(removed_lines) | set(removed_late_repeated_lines))
    # Keep one complete semantic unit per body heading: drop empty trailing index
    # headings and rejoin OCR-split formula lines so later QA/claim extraction
    # can take heading + following content as one grounded unit.
    finalized, removed_empty_heading_index_lines = _remove_trailing_empty_heading_index_block(finalized)
    # Formula merge before the last clean is safer only when archive crumbs stay
    # on their own lines; clean must never soft-join SS号 onto formula rows.
    finalized, formula_join_count = _merge_formula_ingredient_lines(finalized)
    finalized = clean_markdown_text(_normalize_heading_spacing(finalized))
    # Second archive/empty-heading pass after clean, in case metadata was exposed.
    finalized, removed_empty_heading_index_lines_2 = _remove_trailing_empty_heading_index_block(finalized)
    if removed_empty_heading_index_lines_2:
        removed_empty_heading_index_lines = [
            *removed_empty_heading_index_lines,
            *removed_empty_heading_index_lines_2,
        ]
        formula_join_count2 = 0
        finalized, formula_join_count2 = _merge_formula_ingredient_lines(finalized)
        formula_join_count += formula_join_count2
    finalized, index_changes = _normalize_index_block(finalized)
    finalized, removed_book_title_headers = _remove_repeated_book_title_headings(finalized)
    finalized, stripped_heading_page_suffixes = _strip_body_heading_toc_page_suffixes(finalized)
    finalized = _normalize_heading_spacing(finalized)
    # Final monotonic pass: ensure first heading is H1 and no level jumps remain,
    # so local-gate cannot fail heading_level_jump after structure cleanup.
    finalized = _normalize_heading_spacing(_normalize_heading_levels(finalized))
    # Drop residual non-heading lines before the first heading (textbook TOC banners
    # that appear after title headings were demoted/removed during cleanup).
    finalized, removed_late_front_prefix_lines = _remove_unstructured_front_matter_prefix(finalized)
    if removed_late_front_prefix_lines:
        finalized = _normalize_heading_spacing(_normalize_heading_levels(finalized))
    # Promote first heading to H1 last so heading_level_jump cannot reject ##-started bodies.
    finalized, first_heading_promoted = _ensure_first_heading_is_h1(finalized)
    if first_heading_promoted:
        finalized = _normalize_heading_spacing(_normalize_heading_levels(finalized))
        # keep H1 if normalize demoted it via _standard_heading_level defaults
        finalized, first_heading_promoted_again = _ensure_first_heading_is_h1(finalized)
        if first_heading_promoted_again:
            first_heading_promoted = first_heading_promoted_again
    # Batch-proven repairs (forced breaks, mis-headings, latex crumbs, long lines).
    # Gate thresholds unchanged; this only improves pass rate on OCR/textbook exports.
    from llmcheck.repair import apply_batch_proven_repairs
    from llmcheck.cleaning import clean_markdown_text as _clean_after_repair
    finalized, batch_repair_labels = apply_batch_proven_repairs(finalized)
    if batch_repair_labels:
        finalized = _clean_after_repair(finalized)
        finalized = _normalize_heading_spacing(_normalize_heading_levels(finalized))
        finalized, first_heading_promoted_again = _ensure_first_heading_is_h1(finalized)
        if first_heading_promoted_again and not first_heading_promoted:
            first_heading_promoted = first_heading_promoted_again
    else:
        batch_repair_labels = []
    changes: list[dict[str, object]] = []
    if removed_lines:
        changes.append({"kind": "removed_repeated_lines", "lines": removed_lines})
    if removed_vertical_echo_lines:
        changes.append({"kind": "removed_vertical_heading_ocr_echo", "lines": removed_vertical_echo_lines})
    if removed_preface_lines:
        changes.append({"kind": "removed_preface_ocr_noise", "lines": removed_preface_lines})
    if removed_front_prefix_lines:
        changes.append({"kind": "removed_unstructured_front_matter_prefix", "lines": removed_front_prefix_lines})
    if first_heading_promoted:
        changes.append({"kind": "promoted_first_heading_to_h1", "from_level": first_heading_promoted})
    if removed_embedded_title_page_lines:
        changes.append({"kind": "removed_embedded_series_title_page_after_front_matter", "lines": removed_embedded_title_page_lines})
    if removed_front_imprint_lines:
        changes.append({"kind": "removed_front_section_imprint_noise", "lines": removed_front_imprint_lines})
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
    if removed_empty_heading_index_lines:
        changes.append({"kind": "removed_trailing_empty_heading_index_block", "lines": removed_empty_heading_index_lines})
    if formula_join_count:
        changes.append({"kind": "merged_formula_ingredient_lines", "count": formula_join_count})
    if index_changes:
        changes.append({"kind": "normalized_index_block", "count": index_changes})
    if removed_book_title_headers:
        changes.append({"kind": "removed_repeated_book_title_headings", "lines": removed_book_title_headers})
    if stripped_heading_page_suffixes:
        changes.append(
            {
                "kind": "stripped_body_heading_toc_page_suffixes",
                "count": len(stripped_heading_page_suffixes),
                "lines": stripped_heading_page_suffixes,
            }
        )
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
    """Drop short running headers/footers that OCR repeats across pages.

    MinerU often emits list-prefixed headers such as ``- - 现代著名老中医名著重刊丛书``
    or broken series markers ``- - 第`` / ``- - 一`` / ``- - 辑``. Those must be
    eligible for removal; only keep genuine structure markers / content labels.
    """
    lines = text.splitlines()
    counts: dict[str, int] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not _is_candidate_running_line(line):
            continue
        counts[line] = counts.get(line, 0) + 1
    repeated = {line for line, count in counts.items() if count >= 3}
    # Always strip pure series running-header titles when they repeat.
    for line, count in counts.items():
        content = _heading_content(line)
        if count >= 2 and _is_series_running_header_content(content):
            repeated.add(line)
    removed: list[str] = sorted(repeated)
    output: list[str] = []
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        stripped = raw_line.strip()
        content = _heading_content(stripped)
        if stripped in repeated:
            index += 1
            continue
        # Drop pure series title headings used as running headers.
        if stripped.startswith("#") and _is_series_running_header_content(content) and counts.get(stripped, 0) >= 2:
            index += 1
            continue
        # Drop OCR-split series volume / short-title fragments, either by global
        # repetition or as a local 第/一/辑 style block.
        if _is_series_volume_fragment_line(content):
            if counts.get(content, 0) >= 3 or content in repeated:
                index += 1
                continue
            block_end = index
            block_chars: list[str] = []
            while block_end < len(lines):
                piece = _heading_content(lines[block_end].strip())
                if not piece:
                    block_end += 1
                    continue
                if not _is_series_volume_fragment_line(piece):
                    break
                block_chars.append(piece)
                block_end += 1
            joined = "".join(block_chars)
            if len(block_chars) >= 2 and (
                re.fullmatch(r"第[一二三四五六七八九十\d]*辑?", joined)
                or joined in {"前言", "凡例", "索引", "目录", "目錄"}
            ):
                removed.extend(block_chars)
                index = block_end
                continue
        cleaned = _strip_series_running_header_prefix(stripped)
        if cleaned != stripped:
            if cleaned:
                if stripped.startswith("#"):
                    match = re.match(r"^(#{1,6})", stripped)
                    level = len(match.group(1)) if match else 1
                    output.append(f"{'#' * level} {cleaned}")
                elif re.match(r"^[-*+]\s+", stripped):
                    output.append(f"- {cleaned}")
                else:
                    output.append(cleaned)
            index += 1
            continue
        output.append(raw_line)
        index += 1
    # Collapse excess blank lines created by removals.
    collapsed: list[str] = []
    for line in output:
        if not line.strip() and collapsed and not collapsed[-1].strip():
            continue
        collapsed.append(line)
    return "\n".join(collapsed) + ("\n" if collapsed else ""), removed


def _is_candidate_running_line(line: str) -> bool:
    content = _heading_content(line)
    if _is_series_running_header_content(content):
        return True
    if _is_series_volume_fragment_line(content):
        return True
    # Bare repeated section titles used as running headers (e.g. ``一、全身证状``).
    # Only count non-heading lines so the real structured heading is preserved.
    if not line.startswith("#") and re.fullmatch(r"[一二三四五六七八九十百]+、\S{2,16}", content):
        return True
    if not (1 <= len(line) <= 40):
        return False
    # Allow pure series title headings to be counted as running headers.
    if line.startswith("#"):
        return _is_series_running_header_content(content)
    if line.startswith("|"):
        return False
    if _is_standalone_list_marker_line(line):
        return False
    if _is_repeated_content_label_line(line):
        return False
    # Keep very short lines only when they are OCR-split series markers.
    if len(line) < 3:
        return _is_series_volume_fragment_line(line)
    return True


def _is_standalone_list_marker_line(line: str) -> bool:
    return bool(re.fullmatch(r"(?:[-*+]\s*)?[（(]\d{1,3}[）)]", line))


def _is_repeated_content_label_line(line: str) -> bool:
    return bool(re.fullmatch(r"(?:(?:[-*+]\s*)+)?【[^】]{1,12}】", line))


def _is_series_running_header_content(content: str) -> bool:
    compact = re.sub(r"\s+", "", content)
    if not compact:
        return False
    if compact in {"现代著名老中医名著重刊丛书", "中国百年百名中医临床家丛书"}:
        return True
    if re.fullmatch(r"现代著名老中医名著重刊丛书第[一二三四五六七八九十\d]+辑", compact):
        return True
    return bool(re.fullmatch(r"第[一二三四五六七八九十\d]+辑", compact))


def _is_series_volume_fragment_line(content: str) -> bool:
    compact = re.sub(r"\s+", "", content)
    # OCR often splits series volume markers and short section titles into single glyphs.
    return compact in {"第", "一", "二", "三", "四", "五", "辑", "輯", "索", "引", "前", "言", "凡", "例"}


def _strip_series_running_header_prefix(line: str) -> str:
    """Remove series running-header text glued onto TOC/body lines.

    Only strip when the series title is a *prefix* of a longer entry
    (for example ``现代著名老中医名著重刊丛书21. 发白痞 …… 15``). Never delete a
    pure series title heading or an inline catalog heading such as
    ``中国百年百名中医临床家丛书（按姓氏笔画排列）``.
    """
    content = _heading_content(line)
    bullet = re.match(r"^[-*+]\s+(.*)$", content)
    body = bullet.group(1).strip() if bullet else content
    match = re.match(
        r"^(?:现代著名老中医名著重刊丛书|中国百年百名中医临床家丛书|现代著名者中医名著重刊丛书|现代著名塔中医名著重刊丛书)(?:第[一二三四五六七八九十\d]+辑)?(.*)$",
        body,
    )
    if not match:
        return content if not bullet else body
    remainder = match.group(1).strip(" -—–·•")
    # Pure series title / catalog heading — leave untouched so later catalog
    # and front-matter removers can still recognize the anchors.
    if not remainder or re.fullmatch(r"[（(].*[）)]", remainder):
        return content if not bullet else body
    # Only strip when remainder is a glued TOC/body entry (page number or
    # numbered symptom). Book titles after the series name must stay intact so
    # front-matter cleanup can still see the original title-page heading.
    if not (
        _looks_like_toc_entry(remainder)
        or re.match(r"^\d{1,3}[.．、]", remainder)
        or re.search(r"(?:\.{2,}|…{1,}|·{2,}|\s+)\d{1,4}$", remainder)
    ):
        return content if not bullet else body
    if not re.search(r"[㐀-鿿A-Za-z0-9]", remainder):
        return content if not bullet else body
    return remainder


def _remove_vertical_heading_ocr_echo(text: str) -> tuple[str, list[str]]:
    """Drop OCR vertical-title echoes that restate a heading char-by-char.

    Example::

        # 前言

        前
        言
    """
    lines = text.splitlines()
    output: list[str] = []
    removed: list[str] = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        heading_match = re.match(r"^#{1,6}\s+(.+?)\s*$", stripped)
        if not heading_match:
            output.append(raw)
            index += 1
            continue
        output.append(raw)
        title = re.sub(r"\s+", "", heading_match.group(1))
        if not (2 <= len(title) <= 12) or not re.fullmatch(r"[㐀-鿿A-Za-z0-9]+", title):
            index += 1
            continue
        cursor = index + 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        chars: list[str] = []
        probe = cursor
        while probe < len(lines) and len(chars) < len(title):
            piece = lines[probe].strip()
            if not piece:
                break
            compact = re.sub(r"\s+", "", piece)
            if len(compact) != 1 or not re.fullmatch(r"[㐀-鿿A-Za-z0-9]", compact):
                break
            chars.append(compact)
            probe += 1
        if len(chars) == len(title) and "".join(chars) == title:
            removed.extend(chars)
            index = probe
            continue
        index += 1
    return "\n".join(output) + ("\n" if output else ""), removed


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
                bullet_content = bullet_match.group(1).strip()
                if lines and _looks_like_incomplete_toc_output_entry(lines[-1]):
                    lines[-1] = f"{lines[-1].rstrip()}{bullet_content}"
                    changed_entries.append(bullet_content)
                    continue
                lines.append(f"- {bullet_content}")
                continue
            content = _heading_content(stripped)
            # Body section / symptom headings after the real TOC must exit TOC mode
            # BEFORE any incomplete-entry join. Exception: when the next line is
            # a page marker / TOC entry, this is a wrapped TOC title
            # (``二十一、…`` + ``研究 (228)``), not body structure.
            next_content = _next_nonblank_heading_content(source_lines, index + 1)
            if (
                _looks_like_body_structure_heading(content)
                and not _looks_like_toc_entry(content)
                and not _looks_like_toc_page_marker(next_content)
                and not _looks_like_toc_entry(next_content)
            ):
                in_toc = False
                unheaded_toc = False
                lines.append(raw_line)
                continue
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
            # Keep multi-line TOC titles open: join into the previous incomplete
            # bullet, or start a new incomplete bullet when the next line is a
            # page marker / TOC entry. This must run before body-exit so wrapped
            # titles like "万里云大万里路" + "——邓铁涛自传 …… (1)" reconverge.
            # Never join body prose into a TOC bullet.
            if (
                lines
                and _looks_like_incomplete_toc_output_entry(lines[-1])
                and not _looks_like_body_exit_from_toc(content, stripped)
            ):
                lines[-1] = f"{lines[-1].rstrip()}{content}"
                changed_entries.append(content)
                continue
            if _looks_like_toc_page_marker(next_content) or _looks_like_toc_entry(next_content):
                # Wrapped TOC title opener, including ``一、…`` lines that only
                # look like body structure until the next page-marked row arrives.
                lines.append(f"- {content}")
                changed_entries.append(content)
                continue
            # Body / chapter text after the real TOC — leave TOC mode instead of
            # bullet-wrapping every subsequent prose line (which creates false
            # "- - 第/一/辑" headers and fails the repeated-line gate).
            if _looks_like_body_exit_from_toc(content, stripped):
                in_toc = False
                unheaded_toc = False
                lines.append(raw_line)
                continue
            if unheaded_toc:
                if _looks_like_toc_page_marker(next_content):
                    lines.append(f"- {content}")
                    changed_entries.append(content)
                    continue
                in_toc = False
                unheaded_toc = False
                lines.append(raw_line)
                continue
            if re.match(r"^#{1,6}\s+\S", stripped):
                # Body structure / major chapter titles end TOC mode. Only demote
                # true TOC page-heading residue into bullets.
                if _looks_like_toc_entry(content):
                    lines.append(f"- {content}")
                    changed_entries.append(content)
                    continue
                if (
                    _is_major_document_heading(content)
                    or (
                        _looks_like_body_structure_heading(content)
                        and not _looks_like_toc_page_marker(next_content)
                        and not _looks_like_toc_entry(next_content)
                    )
                ):
                    in_toc = False
                    unheaded_toc = False
                    lines.append(raw_line)
                    continue
                in_toc = False
                unheaded_toc = False
                lines.append(raw_line)
                continue
            # Non-TOC residual short lines (broken series headers, page crumbs)
            # stay as plain text; never invent bullets for unknown content.
            in_toc = False
            unheaded_toc = False
            lines.append(raw_line)
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


def _looks_like_body_exit_from_toc(content: str, stripped: str) -> bool:
    """True when a non-TOC-entry line clearly belongs to body text after 目录.

    Exit on long prose, major anchors, or body structure headings that do not
    carry a page-number suffix (``一、全身证状`` / ``1. 恶寒`` after the real TOC).
    """
    if not content and not stripped:
        return False
    probe = content or stripped
    # Long prose with Chinese punctuation — body, not a TOC row.
    if len(probe) >= 40 and re.search(r"[。；！？]", probe):
        return True
    # Common front/body anchors that should end TOC mode (no page suffix).
    if _is_major_document_heading(probe) and not _looks_like_toc_entry(probe):
        return True
    # Numbered body structure headings without page markers.
    if _looks_like_body_structure_heading(probe) and not _looks_like_toc_entry(probe):
        return True
    return False


def _looks_like_body_structure_heading(content: str) -> bool:
    """Section / symptom / appendix openers used for machine-readable hierarchy."""
    normalized = re.sub(r"\s+", " ", content).strip()
    if not normalized or _looks_like_toc_entry(normalized):
        return False
    if re.match(r"^附录[：:].+", normalized) and _is_short_heading_text(normalized, limit=48):
        return True
    if re.match(r"^(?:索引|凡例|凡\s*例)$", normalized):
        return True
    # ``一、全身证状`` / ``二、头面证状``
    if re.match(r"^[一二三四五六七八九十百]+、\s*\S+", normalized) and _is_short_heading_text(normalized, limit=40):
        return True
    # ``1. 恶寒`` / ``12、身重``
    if re.match(r"^\d{1,3}[.．、]\s*\S+", normalized) and _is_short_heading_text(normalized, limit=36):
        return True
    # ``例一 施某…`` / ``案三 藏某…`` case openers in medical case collections
    if re.match(
        r"^(?:例|案)\s*[一二三四五六七八九十百千〇零两\d]+(?:\s|[：:、．.]|[㐀-鿿A-Za-z])",
        normalized,
    ):
        return True
    return False


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

    # Generic: drop non-heading prefix before the first Markdown heading when short.
    first_heading = _find_first_line(lines, lambda line: bool(re.match(r"^#{1,6}\s+\S", line.strip())))
    if first_heading is not None and 0 < first_heading <= 80:
        prefix = lines[:first_heading]
        nonblank = [line.strip() for line in prefix if line.strip()]
        if nonblank and all(not re.match(r"^#{1,6}\s+\S", line) and len(line) <= 120 for line in nonblank):
            # avoid stripping real body paragraphs (long prose) before heading
            long_prose = sum(1 for line in nonblank if len(line) >= 60 and re.search(r"[。！？]", line))
            if long_prose == 0:
                output = lines[first_heading:]
                output_text = "\n".join(output)
                return output_text + ("\n" if output_text else ""), nonblank

    anchor_index = _find_first_line(lines, _looks_like_usable_front_matter_anchor)
    if anchor_index is None or anchor_index <= 0 or anchor_index > 120:
        return text, []
    prefix = lines[:anchor_index]
    if not _looks_like_unstructured_front_matter_prefix(prefix):
        nonblank = [line.strip() for line in prefix if line.strip()]
        if nonblank and all(not re.match(r"^#{1,6}\s+\S", line) and len(line) <= 80 for line in nonblank):
            output = lines[anchor_index:]
            output_text = "\n".join(output)
            return output_text + ("\n" if output_text else ""), nonblank
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
        inline_catalog = bool(
            re.fullmatch(
                r"中国百年百名中医临床家丛书[（(]按姓氏笔画排列[）)]",
                heading,
            )
        )
        if heading != "中国百年百名中医临床家丛书" and not inline_catalog:
            continue
        # Prefer late-document catalogs. For short docs / inline catalog headings,
        # allow earlier positions once a name list is present.
        if index < len(lines) // 2 and len(lines) > 40 and not inline_catalog:
            continue
        # Never treat a mid-book catalog that is followed by real TOC/body as trailing.
        following = lines[index + 1 :]
        if any(_is_toc_heading(line.strip()) for line in following[:80]):
            continue
        if any(_looks_like_toc_entry(_heading_content(line.strip())) for line in following[:40]):
            continue
        if any(
            re.match(r"^#{1,6}\s+\S", line.strip())
            and not _looks_like_front_title_page_line(line)
            and "按姓氏笔画排列" not in _heading_content(line)
            and _heading_content(line)
            not in {"中国百年百名中医临床家丛书", "现代著名老中医名著重刊丛书"}
            for line in following[:80]
        ):
            continue
        window = [line.strip() for line in lines[index : index + 12] if line.strip()]
        has_catalog_label = any("按姓氏笔画排列" in _heading_content(line) for line in window) or inline_catalog
        has_name_list = any(
            (
                len(line) >= 80
                and re.search(r"[㐀-鿿]{8,}", line)
                and not re.search(r"[。！？!?]", line)
            )
            or (
                len(re.findall(r"[㐀-鿿]", line)) >= 24
                and not re.search(r"[。！？!?]", line)
            )
            for line in window
        )
        has_name_table = sum(1 for line in lines[index : index + 40] if _looks_like_series_catalog_table_row(line.strip())) >= 5
        has_vertical_name_list = sum(
            1 for line in lines[index : index + 120] if _looks_like_series_catalog_short_name_line(line.strip())
        ) >= 20
        dense_name_hits = sum(
            1
            for line in window
            if len(re.findall(r"[㐀-鿿]", line)) >= 20 and not re.search(r"[。！？!?]", line)
        )
        has_name_list = has_name_list or has_name_table or has_vertical_name_list or dense_name_hits >= 1
        has_short_title_page_tail = _looks_like_trailing_series_title_page_block(lines[index:])
        if not (
            (has_catalog_label and has_name_list)
            or has_short_title_page_tail
            or (inline_catalog and dense_name_hits >= 1)
        ):
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


# Strict line-start / whole-line archive metadata (no mid-body substring hits).
_ARCHIVE_LINE_START_RE = re.compile(
    r"^(?:"
    r"SS\s*号\s*=|"
    r"DX\s*号\s*=|"
    r"\[General\s*Information\]|"
    r"Document\s+generated\s+by\s+Anna's\s+Archive|"
    r"书名\s*=|"
    r"作者\s*=|"
    r"页数\s*=|"
    r"出版日期\s*=|"
    r"filename_decoded|"
    r"pdg_main_pages|"
    r"url\s*=\s*https?://|"
    r"http://book2\.duxiu\.com/"
    r")",
    re.IGNORECASE,
)
# Inline markers that OCR often soft-joins onto the last body line of a book.
_ARCHIVE_INLINE_MARKER_RE = re.compile(
    r"(?:"
    r"\[General\s*Information\]|"
    r"Document\s+generated\s+by\s+Anna's\s+Archive|"
    r"(?<![㐀-鿿A-Za-z0-9])SS\s*号\s*=\s*\d{5,}|"
    r"(?<![㐀-鿿A-Za-z0-9])DX\s*号\s*=\s*\d{5,}|"
    r"url\s*=\s*https?://(?:book2\.)?duxiu\.com/|"
    r"http://book2\.duxiu\.com/bookDetail\.jsp"
    r")",
    re.IGNORECASE,
)
_ARCHIVE_CONTINUATION_RE = re.compile(
    r"^(?:"
    r"SS\s*号\s*=|"
    r"DX\s*号\s*=|"
    r"SSID\s*=|"
    r"\[General\s*Information\]|"
    r"Document\s+generated\s+by\s+Anna's\s+Archive|"
    r"书名\s*[=：:]|"
    r"作者\s*[=：:]|"
    r"页数\s*[=：:]|"
    r"出版日期\s*[=：:]|"
    r"出版社\s*[=：:]|"
    r"filename_decoded|"
    r"pdg_main_pages|"
    r"起止页\s*[=：:]|"
    r"独秀链接|"
    r"url\s*=\s*https?://|"
    r"http://book2\.duxiu\.com/"
    r")",
    re.IGNORECASE,
)


def _looks_like_archive_metadata_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if _ARCHIVE_LINE_START_RE.match(stripped):
        return True
    unpiped = stripped.strip("|").strip()
    if unpiped and _ARCHIVE_LINE_START_RE.match(unpiped):
        return True
    compact = re.sub(r"\s+", "", stripped)
    if re.match(r"^\[?GeneralInformation\]?", compact, flags=re.IGNORECASE):
        return True
    if re.match(r"^SS号=\d+", compact):
        return True
    if re.match(r"^DX号=\d+", compact):
        return True
    if "duxiu.com" in compact.lower() or re.search(r"DX号=\d{5,}", compact):
        return True
    return False


def _is_rear_document_position(index: int, lines: list[str], text: str) -> bool:
    """True when *index* is in the rear portion of the document.

    Rear means any of:
    - line index >= 70% of lines
    - char offset before the line is already past 80% of the document
    - within the last max(30, 20% of lines) lines
    """
    n = len(lines)
    if n <= 0 or index < 0:
        return False
    if index >= int(n * 0.7):
        return True
    rear_window = max(30, n // 5)
    if index >= max(0, n - rear_window):
        return True
    char_before = sum(len(line) + 1 for line in lines[:index])
    if len(text) > 0 and char_before >= int(len(text) * 0.8):
        return True
    return False


def _content_retention_ok(
    original: str,
    kept: str,
    *,
    clear_trailing_archive: bool,
) -> bool:
    """Reject catastrophic cuts unless they are a clear end-of-doc archive block."""
    if not original:
        return True
    if clear_trailing_archive:
        # Still refuse total wipe; short fixtures may keep only a few lines.
        return len(kept) >= min(20, max(1, len(original) // 20)) or kept.count("\n") >= 3
    if len(kept) < 0.5 * len(original):
        return False
    original_lines = max(1, original.count("\n") + 1)
    kept_lines = max(1, kept.count("\n") + 1) if kept else 0
    if len(original) >= 500 and kept_lines < 0.5 * original_lines:
        return False
    return True


def _strip_inline_archive_metadata(lines: list[str]) -> tuple[list[str], list[str]] | None:
    """Truncate archive metadata soft-joined onto a body line; keep the body prefix.

    OCR often emits one mega-line of body text ending in
    ``...正文[General Information]书名=...SS号=...``. Whole-line cuts would
    delete the book; only the marker suffix must go.
    """
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped:
            continue
        # Pure metadata lines are handled by the whole-line rear cut path.
        if _looks_like_archive_metadata_line(stripped):
            continue
        match = _ARCHIVE_INLINE_MARKER_RE.search(raw_line)
        if match is None:
            continue
        # Require the marker to sit late in this line so mid-sentence false
        # positives (if any) do not truncate real body.
        if match.start() < max(20, int(len(raw_line) * 0.5)):
            # Allow short body+metadata fixtures (e.g. "正文。SS号=12345").
            if match.start() < 8 and len(raw_line) > 40:
                continue
            if match.start() < int(len(raw_line) * 0.85) and len(raw_line) > 200:
                continue
        prefix = raw_line[: match.start()].rstrip()
        if not prefix.strip():
            continue
        removed: list[str] = [raw_line[match.start() :].strip()]
        end = index + 1
        # Drop only pure archive-continuation lines after the truncated line.
        while end < len(lines):
            nxt = lines[end].strip()
            if not nxt:
                end += 1
                continue
            unpiped = nxt.strip("|").strip()
            if _ARCHIVE_CONTINUATION_RE.match(nxt) or _ARCHIVE_CONTINUATION_RE.match(unpiped):
                removed.append(nxt)
                end += 1
                continue
            if _looks_like_archive_metadata_line(nxt):
                removed.append(nxt)
                end += 1
                continue
            break
        output = [*lines[:index], prefix, *lines[end:]]
        while output and not output[-1].strip():
            output.pop()
        return output, removed
    return None


def _remove_trailing_empty_heading_index_block(text: str) -> tuple[str, list[str]]:
    """Drop late empty heading runs that are TOC/index residue, not body units.

    After real body content, OCR often re-emits a bare numbered heading list
    (``### 1. 恶寒`` with no following prose) plus archive metadata. Those empty
    headings poison later semantic-unit / QA extraction because each heading
    looks like a locator without evidence.

    Safety rules:
    - Prefer *inline* truncation when archive markers are soft-joined onto a
      body line (mega-line OCR), so body text is kept.
    - Whole-line archive cuts only in the rear of the document.
    - Abort any cut that would discard >=50% of chars/lines unless it is a
      clear trailing archive block.
    """
    lines = text.splitlines()
    if len(lines) < 8:
        return text, []

    # 1) Inline strip first — never whole-line-cut a mega body line.
    inline = _strip_inline_archive_metadata(lines)
    if inline is not None:
        output_lines, removed = inline
        output_text = "\n".join(output_lines)
        output_text = output_text + ("\n" if output_text else "")
        if _content_retention_ok(text, output_text, clear_trailing_archive=True):
            return output_text, removed
        # Fall through if retention failed (should be rare for inline).

    # 2) Whole-line archive cut — rear only, strict line-start metadata.
    archive_index: int | None = None
    for index, raw_line in enumerate(lines):
        if not _looks_like_archive_metadata_line(raw_line):
            continue
        if not _is_rear_document_position(index, lines, text):
            continue
        archive_index = index
        break

    cut = archive_index
    cut_kind = "archive" if cut is not None else None

    # 3) Fall back: dense late run of empty numbered headings near EOF/archive.
    if cut is None:
        run_start: int | None = None
        empty_run = 0
        for index, raw_line in enumerate(lines):
            stripped = raw_line.strip()
            if not stripped:
                continue
            if re.match(r"^#{1,6}\s+\d{1,3}[.．、]\s+\S+", stripped):
                next_index = index + 1
                while next_index < len(lines) and not lines[next_index].strip():
                    next_index += 1
                next_line = lines[next_index].strip() if next_index < len(lines) else ""
                next_is_archive = bool(next_line) and _looks_like_archive_metadata_line(next_line)
                if (
                    not next_line
                    or next_line.startswith("#")
                    or next_is_archive
                    or _ARCHIVE_LINE_START_RE.match(next_line)
                ):
                    if run_start is None:
                        run_start = index
                    empty_run += 1
                    continue
            if (
                empty_run >= 8
                and run_start is not None
                and _is_rear_document_position(run_start, lines, text)
            ):
                # Require the run to reach EOF or only empty headings / archive.
                probe = index
                while probe < len(lines) and not lines[probe].strip():
                    probe += 1
                rest = [ln.strip() for ln in lines[probe:] if ln.strip()]
                rest_ok = (
                    not rest
                    or all(
                        re.match(r"^#{1,6}\s+\d{1,3}[.．、]\s+\S+", ln)
                        or _looks_like_archive_metadata_line(ln)
                        for ln in rest
                    )
                )
                if rest_ok:
                    cut = run_start
                    cut_kind = "empty_heading"
                    break
            run_start = None
            empty_run = 0
        if (
            cut is None
            and empty_run >= 8
            and run_start is not None
            and _is_rear_document_position(run_start, lines, text)
        ):
            rest = [ln.strip() for ln in lines[run_start:] if ln.strip()]
            rest_ok = all(
                re.match(r"^#{1,6}\s+\d{1,3}[.．、]\s+\S+", ln)
                or _looks_like_archive_metadata_line(ln)
                for ln in rest
            )
            if rest_ok:
                cut = run_start
                cut_kind = "empty_heading"

    if cut is None:
        return text, []

    # Walk back over immediately preceding empty headings / blank lines so we
    # do not leave a dangling empty heading just before the cut.
    while cut > 0:
        prev = lines[cut - 1].strip()
        if not prev:
            cut -= 1
            continue
        if re.match(r"^#{1,6}\s+\d{1,3}[.．、]\s+\S+", prev):
            probe = cut
            while probe < len(lines) and not lines[probe].strip():
                probe += 1
            next_line = lines[probe].strip() if probe < len(lines) else ""
            if (
                not next_line
                or next_line.startswith("#")
                or _looks_like_archive_metadata_line(next_line)
                or _ARCHIVE_LINE_START_RE.match(next_line)
            ):
                cut -= 1
                continue
        break

    # Remember the original archive line before walkback — walkback may land on a
    # blank/empty-heading line, but the cut is still a clear trailing archive cut.
    original_archive_index = archive_index if cut_kind == "archive" else None

    output = lines[:cut]
    while output and not output[-1].strip():
        output.pop()
    output_text = "\n".join(output)
    output_text = output_text + ("\n" if output_text else "")
    clear_archive = (
        original_archive_index is not None
        and _is_rear_document_position(original_archive_index, lines, text)
        and _looks_like_archive_metadata_line(lines[original_archive_index])
    )
    if not _content_retention_ok(text, output_text, clear_trailing_archive=clear_archive):
        return text, []
    # Keep a small absolute floor so tiny accidental cuts still fail closed.
    # Short fixtures with a real trailing SS号 line may keep fewer than 20 lines.
    min_keep = 5 if clear_archive else max(20, len(lines) // 5)
    if len(output) < min_keep:
        return text, []
    removed = [line.strip() for line in lines[cut:] if line.strip()]
    return output_text, removed


def _merge_formula_ingredient_lines(text: str) -> tuple[str, int]:
    """Join OCR-split formula/ingredient rows into complete formula units.

    Goal for downstream QA/claim extraction:
    - one formula stays one line: ``方名 + 药味``
    - different formulas are separated, not glued
    - series crumbs like ``第一辑`` are stripped from formula rows
    """
    lines = text.splitlines()
    if not lines:
        return text, 0
    output: list[str] = []
    joins = 0
    # Note: do NOT treat bare 「方」 as a formula suffix — it matches common words
    # like 「处方」. 「煎」 is included; 「胶」 only as a line-start formula unit.
    formula_name_re = re.compile(r"[㐀-鿿A-Za-z]{1,12}(?:汤|散|丸|饮|膏|丹|煎)")
    herb_token_re = re.compile(r"[㐀-鿿A-Za-z]{1,8}")
    series_crumb_re = re.compile(r"(?:现代著名老中医名著重刊丛书)?第[一二三四五六七八九十\d]+辑")

    def _clean_formula_noise(value: str) -> str:
        cleaned = series_crumb_re.sub(" ", value)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def _is_formula_or_ingredient_line(value: str) -> bool:
        stripped = _clean_formula_noise(value)
        if not stripped or stripped.startswith(("#", "|", "!", "-", "*", ">")):
            return False
        if re.search(r"[。！？!?；;]", stripped):
            return False
        # Archive / imprint metadata must never be absorbed into formula blocks.
        if re.search(
            r"(?:SS\s*号|出版日期|书名\s*=|作者\s*=|页数\s*=|General Information|ISBN)",
            stripped,
            flags=re.IGNORECASE,
        ):
            return False
        if len(stripped) > 240:
            return False
        # Drop trailing archive crumbs that OCR soft-joined onto a formula line.
        stripped = re.sub(
            r"(?:SS\s*号\s*=?\s*\d+|出版日期\s*=.*)$",
            "",
            stripped,
            flags=re.IGNORECASE,
        ).strip()
        if not stripped:
            return False
        has_formula = bool(formula_name_re.search(stripped))
        herb_hits = len(herb_token_re.findall(stripped))
        has_dose = bool(re.search(r"\d", stripped))
        pure_herbs = (
            herb_hits >= 2
            and len(stripped) <= 48
            and not re.search(r"[，,。！？!?：:；;]", stripped)
            and bool(re.fullmatch(r"[㐀-鿿A-Za-z0-9\s]+", stripped))
        )
        return has_formula or pure_herbs or (has_dose and herb_hits >= 2 and len(stripped) <= 100)

    def _split_multi_formulas(value: str) -> list[str]:
        cleaned = _clean_formula_noise(value)
        if not cleaned:
            return []
        # Locate formula names by 剂型 suffix, then take the shortest valid
        # rightmost name ending there (``枣真武汤`` → ``真武汤``).
        ends = [match.end() for match in re.finditer(r"(?:汤|散|丸|饮|膏|丹|煎)", cleaned)]
        if not ends:
            return [cleaned]
        matches: list[tuple[int, int]] = []
        for end in ends:
            chosen: tuple[int, int] | None = None
            suffix = cleaned[end - 1 : end]
            # For 汤/散/丸/饮/膏/丹 prefer shorter rightmost names (枣真武汤→真武汤).
            # For 煎 prefer longer names (大补元煎 over 补元煎).
            name_lens = (4, 3, 2) if suffix == "煎" else (2, 3, 4)
            for name_len in name_lens:
                start = end - (name_len + 1)
                if start < 0:
                    continue
                name = cleaned[start:end]
                if not re.fullmatch(r"[㐀-鿿]{2,4}(?:汤|散|丸|饮|膏|丹|煎)", name):
                    continue
                if suffix == "煎":
                    # first valid (longest) wins
                    chosen = (start, end)
                    break
                # Prefer shorter/rightmost: overwrite with larger start.
                if chosen is None or start > chosen[0]:
                    chosen = (start, end)
            if chosen is not None:
                if matches and chosen[0] < matches[-1][1]:
                    # Overlap with previous: keep the later (rightmost) name.
                    if chosen[0] >= matches[-1][0]:
                        matches[-1] = chosen
                    continue
                matches.append(chosen)
        if not matches:
            return [cleaned]
        if len(matches) == 1 and matches[0][0] == 0:
            return [cleaned]

        parts: list[str] = []
        cursor = 0
        for index, (start, end) in enumerate(matches):
            next_start = matches[index + 1][0] if index + 1 < len(matches) else len(cleaned)
            head = cleaned[cursor:start].strip()
            body = cleaned[start:next_start].strip()
            if index == 0:
                chunk = f"{head} {body}".strip() if head else body
                if chunk:
                    parts.append(re.sub(r"\s+", " ", chunk))
            else:
                if head and parts:
                    parts[-1] = re.sub(r"\s+", " ", f"{parts[-1]} {head}".strip())
                if body:
                    parts.append(re.sub(r"\s+", " ", body))
            cursor = next_start
        return [part for part in parts if part]

    index = 0
    while index < len(lines):
        current = lines[index]
        stripped = current.strip()
        if not _is_formula_or_ingredient_line(stripped):
            output.append(current)
            index += 1
            continue

        block: list[str] = []
        while index < len(lines):
            candidate = lines[index].strip()
            if not candidate:
                peek = index + 1
                while peek < len(lines) and not lines[peek].strip():
                    peek += 1
                if peek < len(lines) and _is_formula_or_ingredient_line(lines[peek].strip()):
                    # If next nonblank starts a new formula name, stop current block.
                    next_clean = _clean_formula_noise(lines[peek].strip())
                    if block and formula_name_re.match(next_clean) and formula_name_re.search(block[0]):
                        break
                    index = peek
                    continue
                break
            if not _is_formula_or_ingredient_line(candidate):
                break
            candidate_clean = _clean_formula_noise(candidate)
            candidate_clean = re.sub(
                r"(?:SS\s*号\s*=?\s*\d+|出版日期\s*=.*|General Information.*)$",
                "",
                candidate_clean,
                flags=re.IGNORECASE,
            ).strip()
            if not candidate_clean:
                # metadata-only remainder: stop and let archive cutter handle it
                break
            if block and formula_name_re.match(candidate_clean) and formula_name_re.search(block[0]):
                break
            block.append(candidate_clean)
            index += 1
            if len(block) > 1:
                joins += 1

        joined = " ".join(block)
        joined = re.sub(
            r"(?:SS\s*号\s*=?\s*\d+|出版日期\s*=.*|General Information.*)$",
            "",
            joined,
            flags=re.IGNORECASE,
        ).strip()
        for part in _split_multi_formulas(joined):
            output.append(_normalize_formula_herb_spacing(part))

        if index < len(lines) and lines[index].strip() and not lines[index].startswith("#"):
            if output and output[-1].strip():
                output.append("")

    collapsed: list[str] = []
    for line in output:
        if not line.strip() and collapsed and not collapsed[-1].strip():
            continue
        # Final spacing pass for any formula line, including long single-line rows
        # that never entered multi-fragment joining.
        if _is_formula_or_ingredient_line(line.strip()) or re.match(
            r"^[㐀-鿿A-Za-z]{1,12}(?:汤|散|丸|饮|膏|丹|煎)",
            line.strip(),
        ):
            line = _normalize_formula_herb_spacing(line)
        collapsed.append(line)
    return "\n".join(collapsed) + ("\n" if collapsed else ""), joins


def _normalize_formula_herb_spacing(line: str) -> str:
    """Insert spaces between glued CJK herb tokens inside a pure formula line.

    Only rewrite lines that look like formula + ingredient lists, never prose
    sentences that merely begin with a formula name (e.g. ``资生丸治疗脾虚…``).
    """
    stripped = line.strip()
    if not stripped:
        return line
    if re.search(r"[。！？!?；;]", stripped):
        return stripped
    # prose / commentary markers — leave untouched
    if re.search(r"(?:治疗|患者|疗效|辨证|嘱|观察|原工作|巩固|转入|收到|关键)", stripped):
        return stripped
    # Strip trailing archive crumbs glued by OCR soft-join.
    stripped = re.sub(
        r"(?:SS\s*号\s*=?\s*\d+|出版日期\s*=.*|General Information.*)$",
        "",
        stripped,
        flags=re.IGNORECASE,
    ).strip()
    match = re.match(
        r"^((?:[㐀-鿿A-Za-z]+\s*){1,4}(?:汤|散|丸|饮|膏|丹|煎|胶))(.*)$",
        stripped,
    )
    if not match:
        return stripped
    name = re.sub(r"\s+", "", match.group(1))
    rest = match.group(2).strip()
    if not rest:
        return name
    # If rest still contains another formula name, leave to multi-formula splitter.
    if re.search(r"[㐀-鿿]{2,4}(?:汤|散|丸|饮|膏|丹|煎)", rest):
        return stripped
    # Rest should be mostly herbs/doses, not long narrative.
    if len(rest) > 180:
        return stripped

    def split_herbs(chunk: str) -> str:
        out: list[str] = []
        i = 0
        while i < len(chunk):
            ch = chunk[i]
            if re.match(r"[㐀-鿿]", ch):
                j = i
                while j < len(chunk) and re.match(r"[㐀-鿿]", chunk[j]):
                    j += 1
                run = chunk[i:j]
                if len(run) >= 4 and len(run) % 2 == 0:
                    out.extend(run[k : k + 2] for k in range(0, len(run), 2))
                elif len(run) >= 5:
                    k = 0
                    while k + 2 <= len(run):
                        if len(run) - k == 3:
                            out.append(run[k : k + 3])
                            k += 3
                        else:
                            out.append(run[k : k + 2])
                            k += 2
                else:
                    out.append(run)
                i = j
            else:
                out.append(ch)
                i += 1
        tokens: list[str] = []
        buf = ""
        for item in out:
            if re.fullmatch(r"[㐀-鿿]+", item):
                if buf:
                    tokens.append(buf)
                    buf = ""
                tokens.append(item)
            else:
                buf += item
        if buf:
            tokens.append(buf)
        return " ".join(token.strip() for token in tokens if token.strip())

    rest_norm = split_herbs(rest)
    rest_norm = re.sub(r"\s+", " ", rest_norm).strip()
    return f"{name} {rest_norm}".strip()


def _remove_repeated_book_title_headings(text: str) -> tuple[str, list[str]]:
    """Drop repeated bare book-title headings used as running headers.

    Any identical short Markdown heading that repeats >=2 times and looks like a
    book/series title (no numbering, no section markers) is removed.
    """
    lines = text.splitlines()
    counts: dict[str, int] = {}
    for raw in lines:
        stripped = raw.strip()
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", stripped)
        if not match:
            continue
        content = match.group(2).strip()
        if not _looks_like_repeated_book_title_heading(content):
            continue
        counts[stripped] = counts.get(stripped, 0) + 1
    victims = {key for key, count in counts.items() if count >= 2}
    # Always drop pure series running-header headings, even if they appear once.
    for raw in lines:
        stripped = raw.strip()
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", stripped)
        if not match:
            continue
        content = match.group(2).strip()
        compact = re.sub(r"\s+", "", content)
        if re.fullmatch(r"现代著名[者塔老]*中医名著重[刊利]丛书", compact) or (
            re.search(r"名著重[刊利]丛书", content)
            and not re.search(r"(?:医案|经验|备要|论医|选编|选集)", content)
        ):
            victims.add(stripped)
    if not victims:
        return text, []
    removed: list[str] = []
    output: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped in victims:
            removed.append(stripped)
            continue
        output.append(raw)
    collapsed: list[str] = []
    for line in output:
        if not line.strip() and collapsed and not collapsed[-1].strip():
            continue
        collapsed.append(line)
    return "\n".join(collapsed) + ("\n" if collapsed else ""), removed


def _looks_like_repeated_book_title_heading(content: str) -> bool:
    if not content or len(content) > 40:
        return False
    if re.match(r"^(?:[一二三四五六七八九十百]+、|\d+[.．、]|附录|索引|目录|前言|凡例|内容提要|出版说明|结语)", content):
        return False
    if re.search(r"[。！？!?：:；;，,]", content):
        return False
    # Pure series running headers (including OCR-garbled variants).
    if re.search(r"名著重[刊利]丛书", content) and not re.search(r"(?:医案|经验|备要|论医|选编|选集)", content):
        return True
    if re.fullmatch(r"现代著名[者塔老]*中医名著重[刊利]丛书", re.sub(r"\s+", "", content)):
        return True
    # Book titles in this corpus are short CJK phrases, often the work name.
    return bool(re.fullmatch(r"[㐀-鿿A-Za-z0-9·•\-—–（）()《》〈〉\s]{2,24}", content))


def _normalize_index_block(text: str) -> tuple[str, int]:
    """Normalize the trailing 索引 into machine-readable stroke sections + bullets.

    Target shape::

        # 索引

        ## 二画

        - 七日风 74
        - 子晕 242
    """
    lines = text.splitlines()
    start = None
    for index, raw in enumerate(lines):
        if re.match(r"^#{1,6}\s*索引\s*$", raw.strip()) or raw.strip() == "索引":
            start = index
            break
    if start is None:
        return text, 0

    head = lines[:start]
    body = lines[start:]
    # Keep only the first index section if duplicated.
    second = None
    for index, raw in enumerate(body[1:], start=1):
        if re.match(r"^#{1,6}\s*索引\s*$", raw.strip()):
            second = index
            break
    if second is not None:
        body = body[:second]

    changes = 0
    out: list[str] = ["# 索引", ""]
    current_stroke = ""
    pending_see = ""
    stroke_re = re.compile(
        r"^(?:#{1,6}\s*)?(?:[-*+]\s+)*(?:[-*+]\s+)*"
        r"([一二三四五六七八九十百]+)\s*画\s*(.*)$"
    )
    entry_re = re.compile(r"^(?:[-*+]\s+)?(?:[-*+]\s+)?(.+?)\s+(\d{1,4})\s*$")
    see_re = re.compile(r"^[（(]?\s*参见\s*(\d+)\s*[）)]?\s*(.*)$")
    see_inline_re = re.compile(r"^[（(]?\s*参见\s*(\d+)\s*[）)]?\s*(.+?)\s+(\d{1,4})\s*$")

    def _ensure_stroke(stroke: str) -> None:
        nonlocal current_stroke, changes
        heading = f"## {stroke}画"
        if current_stroke == heading:
            return
        if out and out[-1] != "":
            out.append("")
        out.append(heading)
        out.append("")
        current_stroke = heading
        changes += 1

    def _flush_entry(term: str, page: str) -> None:
        nonlocal changes, pending_see
        term = re.sub(r"\s+", "", term).strip(" -—–·•")
        # Strip a leading stroke label that was glued into the term.
        stroke_prefix = re.match(r"^([一二三四五六七八九十百]+)画(.+)$", term)
        if stroke_prefix:
            _ensure_stroke(stroke_prefix.group(1))
            term = stroke_prefix.group(2)
        # Normalize leading see-also crumbs glued into the term.
        inline = re.match(r"^[（(]?参见(\d+)[）)]?(.+)$", term)
        if inline:
            pending_see = inline.group(1)
            term = inline.group(2)
        if not term or not page:
            return
        if pending_see:
            term = f"{term}（参见 {pending_see}）"
            pending_see = ""
        out.append(f"- {term} {page}")
        changes += 1

    for raw in body[1:]:
        stripped = raw.strip()
        if not stripped:
            continue
        if re.match(r"^#{1,6}\s*中医临证备要\s*$", stripped) or stripped == "中医临证备要":
            changes += 1
            continue
        if re.fullmatch(r"[第辑一二三四五六七八九十\d\s]+", stripped) and "画" not in stripped:
            # series crumbs
            changes += 1
            continue

        stroke_match = stroke_re.match(stripped)
        if stroke_match:
            stroke = stroke_match.group(1)
            rest = stroke_match.group(2).strip()
            _ensure_stroke(stroke)
            if rest:
                # ``十九画癣疮 11`` / ``三 画子悬 240``
                entry_match = entry_re.match(rest) or entry_re.match(f"- {rest}")
                if entry_match:
                    _flush_entry(entry_match.group(1), entry_match.group(2))
                else:
                    see_match = see_inline_re.match(rest) or see_re.match(rest)
                    if see_match and see_match.lastindex and see_match.lastindex >= 2:
                        if see_inline_re.match(rest):
                            pending_see = see_match.group(1)
                            _flush_entry(see_match.group(2), see_match.group(3))
                        else:
                            pending_see = see_match.group(1)
                            leftover = see_match.group(2).strip()
                            if leftover:
                                em = entry_re.match(leftover)
                                if em:
                                    _flush_entry(em.group(1), em.group(2))
            continue

        see_inline = see_inline_re.match(stripped) or see_inline_re.match(re.sub(r"^[-*+]+\s*", "", stripped))
        if see_inline:
            pending_see = see_inline.group(1)
            _flush_entry(see_inline.group(2), see_inline.group(3))
            continue

        see_match = see_re.match(stripped) or see_re.match(re.sub(r"^[-*+]+\s*", "", stripped))
        if see_match:
            pending_see = see_match.group(1)
            leftover = see_match.group(2).strip()
            if leftover:
                em = entry_re.match(leftover) or entry_re.match(f"- {leftover}")
                if em:
                    _flush_entry(em.group(1), em.group(2))
            changes += 1
            continue

        # ``- - 七日风 74`` / ``七日风 74`` / ``- 子晕 242``
        cleaned = re.sub(r"^[-*+]+\s*", "", stripped)
        cleaned = re.sub(r"^[-*+]+\s*", "", cleaned)
        # Glued stroke label inside entry text: ``三画子悬 240``
        glued_stroke = re.match(r"^([一二三四五六七八九十百]+)\s*画\s*(.+)$", cleaned)
        if glued_stroke:
            _ensure_stroke(glued_stroke.group(1))
            cleaned = glued_stroke.group(2).strip()
        entry_match = entry_re.match(cleaned) or entry_re.match(f"- {cleaned}")
        if entry_match:
            _flush_entry(entry_match.group(1), entry_match.group(2))
            continue

        # Ignore leftover noise in index.
        changes += 1

    while out and not out[-1].strip():
        out.pop()
    out.append("")
    return "\n".join(head + out), changes


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
    return content in {"出版者的话", "出版说明", "内容提要", "目录", "目錄"}


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
    # Bibliographic / imprint lines from scanned title pages (人民卫生/中医药出版社等).
    if re.search(r"[/／].{0,40}著[.—\-—–]|[.—\-—–].{0,20}出版社", content):
        return True
    if re.search(r"科技新书目|统一书号|新华书店|发行所|胶印厂|印数\s*[:：]?", content):
        return True
    # Glued OCR imprint blobs often pack 印刷/开本/印张/定价 without spacing.
    imprint_hits = sum(
        1
        for marker in (
            "印刷",
            "开本",
            "印张",
            "印次",
            "版次",
            "定价",
            "书号",
            "发行",
            "出版社",
            "毫米",
        )
        if marker in content
    )
    if imprint_hits >= 3 and len(content) >= 24:
        return True
    return bool(
        re.search(
            r"(?:中国版本图书馆|发行者|印刷者|经销者|开\s*本|字\s*数|印\s*张|版\s*次|印\s*次|册数|书号|标准书号|定价|邮编|电话|邮购|质量问题|出版社|出版发行|著者|网址|著作权|HTTP|www\.|pmph|E\s*-\s*mail|印刷厂|经销|中继线)",
            content,
            flags=re.IGNORECASE,
        )
    )


def _remove_front_section_imprint_noise(text: str) -> tuple[str, list[str]]:
    """Drop glued imprint / colophon lines that survive after 内容提要 etc.

    Older scans often paste ``出版社…印刷…开本…印张…定价`` as one paragraph
    between the summary and 自序/目录. These are not body text.

    Scope is intentionally narrow: only the early front-matter window. A late
    ``# 目录`` residual after CIP/cover noise must not pull the whole colophon
    into this pass — trailing CIP blocks belong to
    ``_remove_trailing_publication_metadata_noise``.
    """
    lines = text.splitlines()
    toc_index = _find_first_line(lines, _is_toc_heading)
    # Prefer a true early TOC; otherwise only scan the first ~48 lines so mid /
    # trailing CIP + cover OCR remain intact for dedicated trailing removers.
    if toc_index is not None and toc_index <= 48:
        limit = toc_index
    else:
        limit = min(len(lines), 48)
    if limit <= 0 and toc_index is None:
        return text, []
    output: list[str] = []
    removed: list[str] = []
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if index < limit and stripped and _looks_like_front_imprint_blob_line(stripped):
            removed.append(stripped)
            continue
        output.append(raw_line)

    # CIP / imprint often lands *after* an early ``# 目录`` because OCR glued the
    # colophon into the TOC region (``- 人民卫生出版社，2006.12`` / ISBN lines).
    # Strip a short post-TOC imprint run until real body prose or catalog lists.
    if toc_index is not None and toc_index <= 48:
        rebuilt: list[str] = []
        i = 0
        while i < len(output):
            rebuilt.append(output[i])
            if _is_toc_heading(output[i].strip()):
                i += 1
                # consume following imprint / false TOC-metadata lines
                while i < len(output):
                    stripped = output[i].strip()
                    if not stripped:
                        i += 1
                        continue
                    if _looks_like_front_imprint_blob_line(stripped) or _looks_like_post_toc_imprint_line(
                        stripped
                    ):
                        removed.append(stripped)
                        i += 1
                        continue
                    break
                continue
            i += 1
        output = rebuilt

    if not removed:
        return text, []
    collapsed: list[str] = []
    for line in output:
        if not line.strip() and collapsed and not collapsed[-1].strip():
            continue
        collapsed.append(line)
    output_text = "\n".join(collapsed)
    return output_text + ("\n" if output_text else ""), removed


def _looks_like_post_toc_imprint_line(line: str) -> bool:
    """CIP / publisher rows that OCR wrongly places under an early 目录 heading."""
    content = _heading_content(line)
    if not content:
        return False
    # Real TOC entries are short clinical titles with page numbers — keep those.
    if _looks_like_toc_entry(content) and not re.search(
        r"(?:出版社|ISBN|书号|定价|邮编|购书|经销|地址|版权|印装质量|R\s*\d)",
        content,
        flags=re.IGNORECASE,
    ):
        return False
    if re.search(
        r"(?:"
        r"人民卫生出版社|中国中医药出版社"
        r"|ISBN\s*[\d\-Xx]+"
        r"|标准书号|统一书号"
        r"|购书热线|经销\s*[:：]|邮编\s*[:：]?|地址\s*[:：]"
        r"|定\s*价|版权所有|印装质量"
        r"|现代著名老中医名著重刊丛书"
        r"|R\s*\d{2,4}(?:\.\d+)?"
        r"|[IⅠ]\s*[㐀-鿿…]"
        r"|[ⅡⅢⅣⅤⅥⅦⅧⅨ]\s*"
        r")",
        content,
        flags=re.IGNORECASE,
    ):
        return True
    return False


def _looks_like_front_imprint_blob_line(line: str) -> bool:
    """True for glued OCR imprint/colophon rows, not normal prose or CIP titles."""
    content = _heading_content(line)
    if not content:
        return False
    # Structured CIP headings are handled by trailing publication metadata cleanup.
    if re.fullmatch(r"图书在版编目[（(]CIP[）)]数据", content, flags=re.IGNORECASE):
        return False
    # Keep real prose that merely mentions 出版社 once (e.g. 印刷术 / 多次印刷 in body).
    if (
        len(content) >= 80
        and re.search(r"[。！？]", content)
        and not re.search(
            r"(?:印张|印次|统一书号|标准书号|定价\s*[:：]?|开本|印数|胶印厂|印刷\s*[:：]|www\.|pmph|ISBN)",
            content,
            flags=re.IGNORECASE,
        )
    ):
        return False
    # Standalone imprint / colophon fields from true title pages.
    if re.search(
        r"(?:"
        r"ISBN\s*[\d\-Xx]+"
        r"|标准书号"
        r"|www\.pmph\.com"
        r"|https?://www\."
        r"|E\s*-\s*mail\s*:"
        r"|印刷\s*[:：]"
        r"|印刷厂"
        r"|三河市.{0,12}印刷"
        r"|开\s*本\s*[:：]"
        r"|印\s*张\s*[:：]"
        r"|版\s*次\s*[:：]"
        r"|印\s*次\s*[:：]"
        r"|定\s*价\s*[:：]"
        r"|网址\s*[:：]"
        r"|出版发行\s*[:：]"
        r"|著作权所有"
        r"|印装质量问题"
        r"|中国版本图书馆\s*CIP"
        r"|People'?s\s+Medical\s+Publishing"
        r"|购书热线"
        r"|经销\s*[:：]"
        r"|邮编\s*[:：]?\s*\d{5,}"
        r")",
        content,
        flags=re.IGNORECASE,
    ):
        return True
    if re.fullmatch(r"(?:人民卫生出版社|中国中医药出版社)", content):
        return True
    if re.fullmatch(r"人民卫生出版社[，,]\s*\d{4}\.\d{1,2}", content):
        return True
    if re.fullmatch(r"定价\s*[:：]?\s*\d+(?:\.\d+)?\s*元", content):
        return True
    # Spaced pricing OCR: ``定 价：12.00元…``
    if re.search(r"定\s*价\s*[:：]\s*\d+(?:\.\d+)?\s*元", content):
        return True
    # Bibliographic CIP one-liners: ``书名/编者. —北京:出版社,年.``
    if re.search(r"[/／].{0,40}(?:编|著|整理)\s*[.。]?", content) and re.search(
        r"(?:出版社|ISBN|CIP|R\s*[·.]\s*\d+)",
        content,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(r"[—–\-]\s*北京\s*[:：]\s*[^，,]{0,20}出版社", content):
        return True
    imprint_hits = sum(
        1
        for marker in (
            "印刷",
            "开本",
            "印张",
            "印次",
            "版次",
            "定价",
            "书号",
            "发行",
            "出版社",
            "毫米",
            "胶印",
            "统一书号",
            "印数",
            "新华书店",
            "标准书号",
            "邮购",
            "中继线",
        )
        if marker in content
    )
    if imprint_hits >= 3 and len(content) >= 24:
        return True
    # Compact multi-field imprint rows without sentence punctuation.
    if imprint_hits >= 2 and len(content) >= 40 and not re.search(r"[。！？]", content):
        return True
    if re.search(r"科技新书目|统一书号|胶印厂印刷", content):
        return True
    return False


def _looks_like_front_title_page_line(line: str) -> bool:
    content = _heading_content(line)
    if _looks_like_cover_ocr_heading_content(content):
        return True
    # Cover credit name lists: ``奚达孙树椿马德水 孙呈祥武春发康瑞廷``
    if _looks_like_cover_credit_name_list(content):
        return True
    if re.search(r"(?:主编|执行主编|副主编|编委|编著|合著|整理|作者|责任编辑|封面设计|丛编项|图书|网店|购买)", content):
        return True
    if re.search(r"(?:大学|学院|医院|研究所|教研室|出版社|编辑部).{0,12}编$", content):
        return True
    if re.search(r"(?:合著|编著|主编|著者|著)$", content):
        return True
    if "中国百年百名中医临床家丛书" in content:
        return True
    # Series title pages for 人民卫生出版社重刊丛书.
    # Include 老 (canonical) plus OCR variants 者/塔; allow trailing volume/title.
    if re.search(r"现代著名[者塔老]*中医名著重[刊利]丛书", content):
        return True
    if re.fullmatch(r"第[一二三四五六七八九十\d]+辑(?:\s*.+)?", content) and (
        "医案" in content or "经验" in content or "备要" in content or len(content) <= 24
    ):
        return True
    # Compact imprint/title headings that OCR glues with 著者/出版发行.
    if re.search(r"(?:著者|编者|出版发行|中继线)", content):
        return True
    if len(content) <= 12 and not re.search(r"[，。；：！？、,.!?;:0-9]", content):
        return True
    return False


def _looks_like_cover_ocr_heading_content(content: str) -> bool:
    return content in {"临中床医", "临中家医", "临中家医床", "临家中床医", "临中", "家医"}


def _looks_like_cover_credit_name_list(content: str) -> bool:
    """True for OCR cover credit rows of glued personal names without punctuation."""
    normalized = re.sub(r"\s+", "", content)
    if not normalized or len(normalized) < 8 or len(normalized) > 40:
        return False
    if re.search(r"[，。；：！？、,.!?;:0-9A-Za-z/／:：]", content):
        return False
    if re.search(
        r"(?:经验|医案|医话|医论|备要|选集|选编|论著|正骨|针灸|妇科|儿科|眼科|临床|丛书|出版社|医院|大学)",
        normalized,
    ):
        return False
    # 3+ CJK name-like chunks of 2–3 chars each, optionally space-separated.
    chunks = re.findall(r"[㐀-鿿]{2,3}", normalized)
    return len(chunks) >= 3 and sum(len(chunk) for chunk in chunks) >= len(normalized) - 1


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


def _strip_body_heading_toc_page_suffixes(text: str) -> tuple[str, list[str]]:
    """Strip OCR page-number / leader residues stuck on body headings.

    MinerU often leaves ``暑 温(1)`` or ``原病篇 ^(1)`` as markdown headings.
    Those trip ``toc_page_heading_residue`` even though they are body structure,
    not TOC entries. Keep classic clause citations and TOC-declared titles.
    """
    from llmcheck.final_gate import (
        _looks_like_classic_clause_number_citation,
        _looks_like_clinical_case_or_cross_ref_heading,
        _looks_like_figure_or_table_caption_heading,
        _looks_like_journal_volume_issue_citation,
        _looks_like_journal_volume_issue_page_citation,
        _looks_like_toc_page_entry,
        _looks_like_year_reading_plan_heading,
        _toc_body_title_key,
        _toc_declared_body_title_keys,
    )

    toc_titles = _toc_declared_body_title_keys(text)
    changed: list[str] = []
    output: list[str] = []
    for raw_line in text.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", raw_line.strip())
        if not match:
            output.append(raw_line)
            continue
        level, content = match.group(1), match.group(2).strip()
        if _toc_body_title_key(content) in toc_titles:
            output.append(f"{level} {content}")
            continue
        if not _looks_like_toc_page_entry(content):
            # Also strip caret-footnote page markers: ``原病篇 ^(1)``
            caret = re.sub(r"\s*\^\(\d{1,4}\)\s*$", "", content)
            if caret != content and caret:
                changed.append(raw_line.strip())
                output.append(f"{level} {caret}")
                continue
            output.append(f"{level} {content}")
            continue
        if (
            _looks_like_classic_clause_number_citation(content)
            or _looks_like_clinical_case_or_cross_ref_heading(content)
            or _looks_like_figure_or_table_caption_heading(content)
            or _looks_like_journal_volume_issue_citation(content)
            or _looks_like_journal_volume_issue_page_citation(content)
            or _looks_like_year_reading_plan_heading(content)
        ):
            output.append(f"{level} {content}")
            continue
        cleaned = content
        for pattern in (
            r"\s*\^\(\d{1,4}\)\s*$",
            r"\s*[（(]\d{1,4}[）)]\s*$",
            r"[／/]\s*\d{1,4}$",
            r"(?:\.{2,}|．{2,}|…{1,}|·{2,})\d{1,4}$",
            r"\s+\d{1,4}$",
        ):
            cleaned2 = re.sub(pattern, "", cleaned).strip()
            if cleaned2 and cleaned2 != cleaned:
                cleaned = cleaned2
                break
        if cleaned and cleaned != content:
            changed.append(raw_line.strip())
            output.append(f"{level} {cleaned}")
        else:
            output.append(f"{level} {content}")
    result = "\n".join(output)
    if text.endswith("\n"):
        result += "\n"
    return result, changed


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
            marker_level = len(match.group(1))
            level = _standard_heading_level(content, default=marker_level)
            # Preserve an explicit H1 marker even when content looks like a section (一、...).
            if marker_level == 1:
                level = 1
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



def _ensure_first_heading_is_h1(text: str) -> tuple[str, int]:
    """Promote the first Markdown heading to H1 when it starts deeper than H1."""
    lines = text.splitlines()
    for index, raw_line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)$", raw_line.strip())
        if not match:
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        if level <= 1:
            return text, 0
        lines[index] = f"# {title}"
        # After promoting first heading to H1, re-cap subsequent jumps from previous=1
        # by a second _normalize_heading_levels call at the caller.
        output = "\n".join(lines)
        return output + ("\n" if text.endswith("\n") or output else ""), level
    return text, 0


def _cap_heading_level_jump(level: int, previous_level: int) -> int:
    level = min(max(level, 1), 6)
    if previous_level <= 0:
        # Documents may legitimately start at ## (or deeper) after cover stripping;
        # only cap relative jumps, do not force the first heading to H1 here.
        # final_gate still requires the overall heading sequence not to jump more
        # than +1 from the previous emitted heading.
        return level
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
    if re.match(r"^(?:[一二三四五六七八九十百]+[、.．]\s*.+|附：.+|附录[：:].+)$", normalized) and _is_short_heading_text(normalized, limit=48):
        return 2
    if re.match(r"^(?:\d+[.．、]\s*.+|[（(]\d+[）)]\s*.+|[（(][一二三四五六七八九十]+[）)]\s*.+|【.+】)$", normalized) and _is_short_heading_text(normalized, limit=36):
        return 3
    if re.match(
        r"^(?:例|案)\s*[一二三四五六七八九十百千〇零两\d]+(?:\s|[：:、．.]|[㐀-鿿A-Za-z])",
        normalized,
    ):
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
    if re.match(r"^(?:\d+[.．、]\s*.+|[（(]\d+[）)]\s*.+|[（(][一二三四五六七八九十]+[）)]\s*.+|【.+】)$", normalized):
        return 3
    if re.match(
        r"^(?:例|案)\s*[一二三四五六七八九十百千〇零两\d]+(?:\s|[：:、．.]|[㐀-鿿A-Za-z])",
        normalized,
    ):
        return 3
    if re.match(r"^附录[：:].+", normalized):
        return 1
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
    base = re.sub(r"^附录[：:]\s*", "附录", base)
    return base in {
        "目录",
        "目錄",
        "日录",
        "日錄",
        "出版者的话",
        "出版说明",
        "内容提要",
        "编写说明",
        "序",
        "自序",
        "前言",
        "凡例",
        "凡 例",
        "医家小传",
        "专病论治",
        "临证特色",
        "临证经验",
        "学术思想",
        "年谱",
        "附录",
        "索引",
        "结语",
        "后记",
        "常见病辨证施针",
        "辨证选穴施针精要",
        "临床中医",
    } or base.startswith("附录：") or base.startswith("附录:")



def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
