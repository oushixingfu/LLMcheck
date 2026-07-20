from __future__ import annotations

import re
from typing import Any

BAD_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
MOJIBAKE_PATTERNS = ("锟斤拷", "Ã", "Â", "â€™", "â€œ", "â€�")
ZERO_WIDTH_RE = re.compile(r"[​‌‍﻿]")
LATEX_ARTIFACT_RE = re.compile(r"(\\[A-Za-z]+|\$)")
FORCED_LINE_BREAK_RE = re.compile(r"(?<![。！？.!?：:；;\n])\n(?!\n|#{1,6}\s|[-*+]\s|\d+[.)、]\s|\|)")
FINAL_BLOCKING_ERROR_CODES = {
    "bad_control_characters",
    "mojibake",
    "replacement_characters",
    "zero_width_characters",
    "latex_artifacts",
    "abnormal_cjk_spaces",
    "forced_line_breaks",
    "duplicate_repeated_lines",
    "headingless_long_document",
    "invalid_heading_syntax",
    "nonstandard_heading_content",
    "heading_level_jump",
    "toc_page_heading_residue",
    "repeated_toc_heading_residue",
    "unstructured_front_matter_prefix",
    "pdf_md_mismatch",
    # Semantic-unit delivery contract: block collapsed whole-book mega lines and
    # long docs whose heading structure was destroyed (few headings left).
    "mega_line",
    "low_heading_density",
}
QUALITY_ERROR_HINTS = {
    "mojibake": "检测到典型编码乱码，请回到原始文本或 OCR 结果修复后再交付。",
    "replacement_characters": "检测到 Unicode 替换字符 �，说明源文本存在无法识别的字符。",
    "zero_width_characters": "检测到零宽字符，建议删除不可见字符后重新验收。",
    "abnormal_cjk_spaces": "检测到中文字符之间的异常连续空格，建议合并为正常中文文本。",
    "forced_line_breaks": "检测到疑似 OCR 物理折行残留，建议合并为自然段落。",
    "duplicate_repeated_lines": "检测到短行重复出现，可能是重复页眉、页脚或扫描噪声。",
    "headingless_long_document": "长文档没有 Markdown 标题层级，不能作为标准 Markdown 交付。",
    "invalid_heading_syntax": "检测到不合法 Markdown 标题语法，例如缺少 # 后空格或标题层级超过 6。",
    "nonstandard_heading_content": "检测到装饰符、药方剂量或封面碎片被错误标成 Markdown 标题。",
    "heading_level_jump": "检测到 Markdown 标题层级跳变，章节结构需要重新规范。",
    "toc_page_heading_residue": "检测到目录页码项仍被保留为 Markdown 标题，应降级为目录列表再交付。",
    "repeated_toc_heading_residue": "检测到重复目录标题，目录应归一为一个 Markdown 章节。",
    "unstructured_front_matter_prefix": "检测到首个 Markdown 标题前残留封面、出版元数据或 OCR 前置块，应先结构化清理。",
    "pdf_md_mismatch": "Markdown 与文字版 PDF 的来源或内容 hash 不一致，需要重新生成交付物。",
    "mega_line": "检测到超长单行正文（整书/大段粘连），无法按语义单元切分，应回到 cleaned 重建段落与标题。",
    "low_heading_density": "长文档标题过少，疑似结构在定稿阶段被压扁，不能作为机器可读语义单元交付。",
}
HEADINGLESS_LONG_DOCUMENT_MIN_READABLE_CHARS = 2_000
HEADINGLESS_LONG_DOCUMENT_MIN_LINES = 80
# Block delivery when any single line is this long (collapsed body signature).
MEGA_LINE_MAX_CHARS = 3_000
# Long documents with fewer than this many Markdown headings are treated as collapsed.
LOW_HEADING_DENSITY_MIN_READABLE_CHARS = 20_000
LOW_HEADING_DENSITY_MIN_HEADINGS = 5
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


def final_acceptance_report(text: str) -> dict[str, Any]:
    errors = quality_errors(text)
    blocking = [error for error in errors if error in FINAL_BLOCKING_ERROR_CODES]
    return {
        "status": "passed" if not blocking else "needs_revision",
        "accepted": not blocking,
        "blocking_errors": blocking,
        "warnings": [error for error in errors if error not in blocking],
        "hints": quality_hints(text),
    }



def quality_errors(text: str) -> list[str]:
    errors: list[str] = []
    if not text.strip():
        return ["empty_text"]
    readable_chars = len(re.findall(r"[㐀-鿿A-Za-z0-9]", text))
    if readable_chars < 20:
        errors.append("low_readable_chars")
    hints = quality_hints(text)
    if hints["punctuation_density"] < 0.006 and hints["chinese_chars"] >= 1000:
        errors.append("low_punctuation_density")
    if hints["forced_line_break_candidate_count"] >= 80:
        errors.append("forced_line_break_residue")
    if BAD_CONTROL_RE.search(text):
        errors.append("bad_control_characters")
    errors.extend(error for error in _deterministic_quality_errors(text) if error not in errors)
    errors.extend(error for error in _heading_quality_errors(text) if error not in errors)
    errors.extend(error for error in _segmentation_quality_errors(text) if error not in errors)
    return errors



def quality_hints(text: str) -> dict[str, Any]:
    chinese_chars = len(re.findall(r"[㐀-鿿]", text))
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
    max_line_chars = max((len(line) for line in text.splitlines()), default=0)
    heading_count = len(_markdown_heading_levels(text))
    return {
        "total_chars": len(text),
        "chinese_chars": chinese_chars,
        "line_count": len(lines),
        "punctuation_count": punctuation_count,
        "punctuation_density": round(punctuation_count / max(1, chinese_chars), 4),
        "max_line_chars": max_line_chars,
        "heading_count": heading_count,
        "error_hints": {
            error: QUALITY_ERROR_HINTS[error]
            for error in (
                *_deterministic_quality_errors(text),
                *_segmentation_quality_errors(text),
            )
            if error in QUALITY_ERROR_HINTS
        },
        "forced_line_break_candidate_count": len(forced_line_break_candidates(text)),
        "forced_line_break_samples": forced,
        "long_low_punctuation_line_count": len(low_punctuation_lines),
        "long_low_punctuation_samples": low_punctuation_lines[:8],
    }



def forced_line_break_candidates(text: str, *, limit: int | None = None, structure_labels: tuple[str, ...] | None = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    lines = text.splitlines()
    for index in range(len(lines) - 1):
        previous = lines[index].strip()
        current = lines[index + 1].strip()
        if _looks_like_forced_break(previous, current, structure_labels=structure_labels):
            rows.append({"line_number": index + 1, "previous": previous[:120], "current": current[:120]})
            if limit is not None and len(rows) >= limit:
                break
        if previous and not current:
            next_index = index + 1
            while next_index < len(lines) and not lines[next_index].strip():
                next_index += 1
            if next_index < len(lines):
                next_text = lines[next_index].strip()
                if _looks_like_blank_separated_formula_fragment(
                    previous,
                    next_text,
                ) or _looks_like_blank_separated_forced_break(
                    previous,
                    next_text,
                    structure_labels=structure_labels,
                ):
                    rows.append(
                        {
                            "line_number": index + 1,
                            "next_line_number": next_index + 1,
                            "previous": previous[:120],
                            "current": next_text[:120],
                        }
                    )
                    if limit is not None and len(rows) >= limit:
                        break
    return rows



def _deterministic_quality_errors(text: str) -> list[str]:
    errors: list[str] = []
    if any(pattern in text for pattern in MOJIBAKE_PATTERNS):
        errors.append("mojibake")
    if "�" in text:
        errors.append("replacement_characters")
    if ZERO_WIDTH_RE.search(text):
        errors.append("zero_width_characters")
    if LATEX_ARTIFACT_RE.search(text):
        errors.append("latex_artifacts")
    if re.search(r"[一-鿿] {2,}[一-鿿]", text):
        errors.append("abnormal_cjk_spaces")
    if _has_general_forced_line_break(text):
        errors.append("forced_line_breaks")
    if _has_repeated_short_lines(text):
        errors.append("duplicate_repeated_lines")
    return errors



def _heading_quality_errors(text: str) -> list[str]:
    errors: list[str] = []
    if _has_invalid_heading_syntax(text):
        errors.append("invalid_heading_syntax")
    if _is_headingless_long_document(text):
        errors.append("headingless_long_document")
    if _has_nonstandard_heading_content(text):
        errors.append("nonstandard_heading_content")
    if _has_heading_level_jump(text):
        errors.append("heading_level_jump")
    if _has_toc_page_heading_residue(text):
        errors.append("toc_page_heading_residue")
    if _has_repeated_toc_heading_residue(text):
        errors.append("repeated_toc_heading_residue")
    if _has_unstructured_front_matter_prefix(text):
        errors.append("unstructured_front_matter_prefix")
    return errors


def _segmentation_quality_errors(text: str) -> list[str]:
    """Block delivery shapes that pass heading syntax but destroy semantic units."""
    errors: list[str] = []
    if _has_mega_line(text):
        errors.append("mega_line")
    if _has_low_heading_density(text):
        errors.append("low_heading_density")
    return errors


def _has_mega_line(text: str) -> bool:
    return any(len(line) >= MEGA_LINE_MAX_CHARS for line in text.splitlines())


def _has_low_heading_density(text: str) -> bool:
    readable_chars = len(re.findall(r"[㐀-鿿A-Za-z0-9]", text))
    if readable_chars < LOW_HEADING_DENSITY_MIN_READABLE_CHARS:
        return False
    heading_count = len(_markdown_heading_levels(text))
    return heading_count < LOW_HEADING_DENSITY_MIN_HEADINGS



def _markdown_heading_levels(text: str) -> list[int]:
    levels: list[int] = []
    for line in text.splitlines():
        match = re.match(r"^(#{1,6})\s+\S", line)
        if match:
            levels.append(len(match.group(1)))
    return levels



def _has_invalid_heading_syntax(text: str) -> bool:
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith("#"):
            continue
        if re.match(r"^#{1,6}\s+\S", stripped):
            continue
        return True
    return False


def _has_nonstandard_heading_content(text: str) -> bool:
    for raw_line in text.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", raw_line.strip())
        if match and _looks_like_nonstandard_heading_content(match.group(1).strip()):
            return True
    return False


def _looks_like_nonstandard_heading_content(content: str) -> bool:
    normalized = re.sub(r"\s+", " ", content).strip()
    if not normalized or _is_major_document_heading(normalized):
        return False
    if "中国百年百名中医临床家丛书" in normalized:
        return True
    if normalized in {"临中床医", "临中家医", "临中家医床", "临家中床医", "临中", "家医"}:
        return True
    if re.fullmatch(r"[（(]\d{1,4}[）)]", normalized):
        return True
    if re.fullmatch(r"[※＊*★☆·•\-—_=]+", normalized):
        return True
    if len(normalized) == 1 and re.fullmatch(r"[\u3400-\u9fffA-Za-z]", normalized):
        return True
    dosage = r"\d+(?:\.\d+)?\s*(?:g|克|kg|mg|ml|mL|帖|付|枚|片|丸|粒|钱|分)"
    if len(re.findall(dosage, normalized, flags=re.IGNORECASE)) >= 2:
        return True
    return False


def _has_toc_page_heading_residue(text: str) -> bool:
    in_toc = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if _is_toc_heading(stripped):
            in_toc = True
            continue
        if not in_toc or not stripped:
            continue
        heading_match = re.match(r"^#{1,6}\s+(.+?)\s*$", stripped)
        if not heading_match:
            continue
        content = heading_match.group(1).strip()
        if _looks_like_toc_page_entry(content):
            return True
        # Any non-TOC heading after 目录 means body structure has started.
        # Do not keep scanning later clinical headings for false page suffixes
        # (``住院号 1165`` / incomplete ``第（67）`` cross-refs).
        in_toc = False
    toc_declared_body_titles = _toc_declared_body_title_keys(text)
    for raw_line in text.splitlines():
        heading_match = re.match(r"^#{1,6}\s+(.+?)\s*$", raw_line.strip())
        if not heading_match:
            continue
        content = heading_match.group(1).strip()
        if _toc_body_title_key(content) in toc_declared_body_titles:
            continue
        if _looks_like_toc_page_entry(content):
            return True
    return False


def _toc_declared_body_title_keys(text: str) -> set[str]:
    titles: set[str] = set()
    in_toc = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if _is_toc_heading(stripped):
            in_toc = True
            continue
        if not in_toc or not stripped:
            continue
        heading_match = re.match(r"^#{1,6}\s+(.+?)\s*$", stripped)
        if heading_match and _is_major_document_heading(heading_match.group(1).strip()):
            in_toc = False
            continue
        bullet_match = re.match(r"^[-*]\s+(.+?)\s*$", stripped)
        if not bullet_match:
            continue
        entry = bullet_match.group(1).strip()
        body_title = _strip_toc_page_suffix(entry)
        if body_title != entry:
            titles.add(_toc_body_title_key(body_title))
    return titles


def _strip_toc_page_suffix(content: str) -> str:
    normalized = re.sub(r"\s+", " ", content).strip()
    for pattern in (
        r"(?:\s|\.{2,}|．{2,}|…{1,}|·{2,})[（(]\d{1,4}[）)]$",
        r"[／/]\s*\d{1,4}$",
        r"\s+\d{1,4}$",
        r"(?:\.{2,}|．{2,}|…{1,}|·{2,})\d{1,4}$",
    ):
        match = re.search(pattern, normalized)
        if match:
            return normalized[: match.start()].strip()
    return normalized


def _toc_body_title_key(content: str) -> str:
    normalized = re.sub(r"\s+", " ", content).strip()
    return re.sub(r"\s*[（(]\s*(\d+)\s*[）)]", r"(\1)", normalized)


def _is_toc_heading(line: str) -> bool:
    return bool(re.match(r"^#{1,6}\s+(?:目\s*录|目錄|日\s*录|日錄)\s*$", line.strip()))


def _has_repeated_toc_heading_residue(text: str) -> bool:
    return sum(1 for line in text.splitlines() if _is_toc_heading(line.strip())) > 1


def _has_unstructured_front_matter_prefix(text: str) -> bool:
    lines = text.splitlines()
    first_nonblank_index: int | None = None
    first_heading_index: int | None = None
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if first_nonblank_index is None and stripped:
            first_nonblank_index = index
        if re.match(r"^#{1,6}\s+\S", stripped):
            first_heading_index = index
            break
    if first_nonblank_index is None:
        return False
    if first_heading_index is None:
        return False
    if first_heading_index == first_nonblank_index:
        return _has_title_page_block_before_reliable_front_matter(lines, first_heading_index)
    if first_heading_index > 120:
        return False
    prefix = lines[first_nonblank_index:first_heading_index]
    nonblank = [line.strip() for line in prefix if line.strip()]
    metadata_hits = sum(1 for line in nonblank if _looks_like_front_matter_metadata_line(line))
    has_image = any(_is_markdown_image_line(line) for line in nonblank)
    return bool(nonblank or metadata_hits or has_image)


def _has_title_page_block_before_reliable_front_matter(lines: list[str], first_heading_index: int) -> bool:
    first_heading_match = re.match(r"^#{1,6}\s+(.+?)\s*$", lines[first_heading_index].strip())
    if not first_heading_match:
        return False
    first_heading_content = first_heading_match.group(1).strip()
    if _looks_like_reliable_front_matter_heading(lines[first_heading_index]):
        return False
    if not _looks_like_front_title_page_line(first_heading_content):
        return False
    reliable_index: int | None = None
    for index in range(first_heading_index + 1, min(len(lines), first_heading_index + 121)):
        if _looks_like_reliable_front_matter_heading(lines[index]):
            reliable_index = index
            break
    if reliable_index is None:
        return False
    prefix = lines[first_heading_index:reliable_index]
    nonblank = [line.strip() for line in prefix if line.strip()]
    if not nonblank:
        return False
    for line in nonblank:
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if match and not _looks_like_front_title_page_line(match.group(1).strip()):
            return False
    return any(_looks_like_front_title_page_line(line) or _is_markdown_image_line(line) for line in nonblank)


def _looks_like_reliable_front_matter_heading(line: str) -> bool:
    content = re.sub(r"^#{1,6}\s+", "", line.strip()).strip()
    return content in {"出版者的话", "出版说明", "内容提要", "目录", "目錄"}


def _looks_like_front_matter_metadata_line(line: str) -> bool:
    content = re.sub(r"^#{1,6}\s+", "", line.strip()).strip()
    if re.search(r"ISBN|CIP|R·\d+|/R[.·]\d+", content, flags=re.IGNORECASE):
        return True
    if re.search(r"[/／].{0,40}著[.—\-—–]|[.—\-—–].{0,20}出版社", content):
        return True
    if re.search(r"科技新书目|统一书号|新华书店|发行所|胶印厂|印数\s*[:：]?", content):
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


def _looks_like_front_title_page_line(line: str) -> bool:
    content = re.sub(r"^#{1,6}\s+", "", line.strip()).strip()
    if content in {"临中床医", "临中家医", "临中家医床", "临家中床医", "临中", "家医"}:
        return True
    if re.search(r"(?:主编|执行主编|副主编|编委|编著|合著|整理|作者|责任编辑|封面设计|丛编项|图书|网店|购买)", content):
        return True
    if re.search(r"(?:合著|编著|主编|著者|著)$", content):
        return True
    if "中国百年百名中医临床家丛书" in content:
        return True
    if "现代著名老中医名著重刊丛书" in content:
        return True
    if re.fullmatch(r"第[一二三四五六七八九十\d]+辑", content):
        return True
    if "出版社" in content:
        return True
    if len(content) <= 12 and not re.search(r"[，。；：！？、,.!?;:0-9]", content):
        return True
    return False


def _is_markdown_image_line(line: str) -> bool:
    return bool(re.match(r"^!\[[^\]]*]\([^)]+\)\s*$", line.strip()))


def _looks_like_toc_page_entry(content: str) -> bool:
    normalized = re.sub(r"\s+", " ", content).strip()
    if _looks_like_clinical_case_or_cross_ref_heading(normalized):
        return False
    if _looks_like_figure_or_table_caption_heading(normalized):
        return False
    parenthetical_page = re.search(r"[（(]\d+[）)]$", normalized)
    if parenthetical_page:
        title = normalized[: parenthetical_page.start()].strip()
        if _looks_like_journal_volume_issue_citation(title):
            return False
        if _looks_like_classic_clause_number_citation(title):
            return False
        if _looks_like_year_reading_plan_heading(title):
            return False
        if _looks_like_clinical_case_or_cross_ref_heading(title):
            return False
        # Incomplete cross-ref residue: ``…第（67`` / ``…见 …（12``
        if re.search(r"(?:第|见|見|参|參)\s*$", title):
            return False
        if len(title) >= 24 and re.search(r"[“”‘’《》。，、；：！？\[\]]", title):
            return False
        return True
    if _looks_like_journal_volume_issue_page_citation(normalized):
        return False
    return bool(
        re.search(r"[／/]\s*\d+$", normalized)
        or re.search(r"\s+\d{1,4}$", normalized)
        or re.search(r"(?:\.{2,}|．{2,}|…{1,}|·{2,})\d{1,4}$", normalized)
    )


def _looks_like_figure_or_table_caption_heading(content: str) -> bool:
    """Body figure/table captions such as ``图 3`` / ``表12`` / ``附图 1``."""
    normalized = re.sub(r"\s+", " ", content).strip()
    return bool(
        re.fullmatch(
            r"(?:附)?(?:图|表|圖|錶)\s*[一二三四五六七八九十百千〇零两\d]+(?:[-–—]\d+)?(?:\s*[:：].{0,40})?",
            normalized,
        )
    )


def _looks_like_clinical_case_or_cross_ref_heading(content: str) -> bool:
    """Body clinical headings that only happen to end with digits / parentheses."""
    normalized = re.sub(r"\s+", " ", content).strip()
    if not normalized:
        return False
    if re.search(r"(?:门诊号|住院号|病案号|病历号)\s*[:：]?\s*\d+\s*[。.]?$", normalized):
        return True
    if re.match(r"^(?:例|案)\s*[一二三四五六七八九十百千〇零两\d]+", normalized):
        return True
    if re.search(r"[\[【]\s*见\s*", normalized) or re.search(r"见\s*[“\"《]", normalized):
        return True
    # Clinical stats / outcome headings: ``…痊愈者 80 例，显效者 80``
    if re.search(r"(?:例|人次|穴位|针次)\s*$", normalized) or re.search(
        r"(?:痊愈者|显效者|有效者|治疗)\s*\d+", normalized
    ):
        return True
    # Body syndrome→formula titles that keep an outer page-like ``(n)``:
    # ``湿热郁于血分——滑石白鱼散(行水消瘀) (11)``
    if "——" in normalized and re.search(r"(?:汤|散|丸|饮|膏|丹|煎|方)\s*[（(]", normalized):
        return True
    return False


def _looks_like_journal_volume_issue_citation(title: str) -> bool:
    normalized = re.sub(r"\s+", " ", title).strip()
    return bool(
        re.search(r"(?:杂志|学报|期刊|报)\s*\S{0,12}\d{4}\s*[；;：:，,]\s*\d+$", normalized)
        or re.search(r"\d{4}\s*[；;：:，,]\s*\d+$", normalized)
        and re.search(r"《[^》]{2,40}》", normalized)
    )


def _looks_like_journal_volume_issue_page_citation(title: str) -> bool:
    normalized = re.sub(r"\s+", " ", title).strip()
    return bool(
        re.search(
            r"(?:杂志|学报|期刊|医刊|报)\s*[，,]\s*\d{4}\s*[；;：:，,]\s*\d+\s*[（(]\d+[）)]\s*[：:]\s*\d{1,4}$",
            normalized,
        )
    )


def _looks_like_classic_clause_number_citation(title: str) -> bool:
    normalized = re.sub(r"\s+", " ", title).strip()
    if re.search(r"[，,。].{0,40}主之[。.]?$", normalized):
        return True
    # 《伤寒论》 style source-marked clauses: ``【原文】…`` / ``〔原文〕…``
    if re.match(r"^[【\[〔]\s*原文\s*[】\]〕]", normalized):
        return True
    # OCR-split classic body clauses that still look like continuous classical prose
    # rather than short TOC titles, e.g. ``谷，脾胃气尚弱…损谷则愈。``
    if (
        len(normalized) >= 16
        and re.search(r"[，,。；;]", normalized)
        and re.search(r"[。；;]\s*$", normalized)
        and not re.search(r"(?:目录|頁|页码)", normalized)
    ):
        return True
    return False


def _looks_like_year_reading_plan_heading(title: str) -> bool:
    normalized = re.sub(r"\s+", " ", title).strip()
    return bool(
        re.search(r"《[^》]{1,40}》", normalized)
        and re.search(r"(?:^|\s)\d+(?:\.\d+)?\s*年\s*$", normalized)
    )


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



def _is_headingless_long_document(text: str) -> bool:
    if _markdown_heading_levels(text):
        return False
    if not re.search(r"[㐀-鿿]", text):
        return False
    readable_chars = len(re.findall(r"[㐀-鿿A-Za-z0-9]", text))
    nonblank_lines = sum(1 for line in text.splitlines() if line.strip())
    return (
        readable_chars >= HEADINGLESS_LONG_DOCUMENT_MIN_READABLE_CHARS
        or nonblank_lines >= HEADINGLESS_LONG_DOCUMENT_MIN_LINES
    )



def _has_heading_level_jump(text: str) -> bool:
    previous = 0
    for level in _markdown_heading_levels(text):
        if previous == 0:
            if level > 1:
                return True
        elif level > previous + 1:
            return True
        previous = level
    return False



def _has_repeated_short_lines(text: str) -> bool:
    counts: dict[str, int] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if (
            3 <= len(line) <= 40
            and not line.startswith(("#", "|"))
            and not _is_standalone_list_marker(line)
            and not _is_repeated_content_label(line)
            and not _looks_like_repeated_formula_line(line)
        ):
            counts[line] = counts.get(line, 0) + 1
    return any(count >= 3 for count in counts.values())


def _is_standalone_list_marker(line: str) -> bool:
    return bool(re.fullmatch(r"(?:[-*+]\s*)?[（(]\d{1,3}[）)]", line))


def _is_repeated_content_label(line: str) -> bool:
    return bool(re.fullmatch(r"(?:(?:[-*+]\s*)+)?【[^】]{1,12}】", line))


def _looks_like_repeated_formula_line(line: str) -> bool:
    """True for TCM formula/ingredient rows that legitimately repeat in body text."""
    compact = re.sub(r"\s+", " ", line).strip()
    if not compact or len(compact) > 40:
        return False
    # e.g. ``逍遥散 当归 白芍 柴胡 白术 茯苓 甘草`` / ``金黄散 南星 陈皮 ...``
    return bool(
        re.match(
            r"^[㐀-鿿A-Za-z0-9·•]{1,12}(?:汤|散|丸|饮|膏|丹|煎|方)\s+[㐀-鿿A-Za-z]",
            compact,
        )
    )



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
    # Complete formula units are intentional separate lines for QA/claim extraction.
    if _looks_like_formula_unit_line(previous) or _looks_like_formula_unit_line(current):
        return False
    # Prescription / dose rows and their following usage notes are separate semantic units.
    if _looks_like_prescription_or_dose_line(previous) or _looks_like_prescription_or_dose_line(current):
        return False
    if re.match(r"^(?:按[：:]|此方|处方[：:]|共为|隔两日|嘱仍|古人治|此症|药用)", current):
        return False
    # Incomplete OCR paragraph tails should join later in cleaning; for the gate,
    # only flag ordinary forced breaks, not dose continuations like ``9克共为细末``.
    if re.match(r"^\d+(?:\.\d+)?\s*克", current) and previous.count("克") >= 1:
        return False
    return _looks_like_general_paragraph_fragment(previous, current)


def _looks_like_blank_separated_forced_break(
    previous: str,
    current: str,
    *,
    structure_labels: tuple[str, ...] | None = None,
) -> bool:
    if _looks_like_case_heading_line(previous):
        return False
    if _looks_like_front_matter_line(previous) or _looks_like_front_matter_line(current):
        return False
    if _looks_like_formula_unit_line(previous) or _looks_like_formula_unit_line(current):
        return False
    return _looks_like_forced_break(previous, current, structure_labels=structure_labels)


def _looks_like_blank_separated_formula_fragment(previous: str, current: str) -> bool:
    if not previous or not current:
        return False
    if _looks_like_case_heading_line(previous) or _looks_like_front_matter_line(previous):
        return False
    # A new formula name starts a new unit — not a broken fragment of the previous one.
    if _looks_like_formula_unit_line(current):
        return False
    # Dose continuation / usage note after a prescription row is intentional structure.
    if _looks_like_prescription_or_dose_line(previous) or _looks_like_prescription_or_dose_line(current):
        return False
    dosage = r"\d+(?:\.\d+)?\s*(?:g|克|kg|mg|ml|mL|帖|付|枚|片|丸|粒|钱|分)"
    if len(current) > 240:
        return False
    if len(current) > 48 and not re.match(dosage, current, flags=re.IGNORECASE):
        return False
    if not re.search(dosage, previous, flags=re.IGNORECASE):
        return False
    if not re.search(dosage, current, flags=re.IGNORECASE):
        return False
    return bool(re.search(r"[㐀-鿿A-Za-z0-9]$", previous) and re.search(r"^[㐀-鿿A-Za-z0-9]", current))


def _looks_like_formula_unit_line(value: str) -> bool:
    stripped = value.strip()
    if not stripped or stripped.startswith(("#", "|", "-", "*", "!", ">")):
        return False
    if len(stripped) > 200:
        return False
    # Named formula units, optionally followed by a short usage note:
    # ``苍术白虎汤 即白虎汤加苍术。`` / ``竹叶石膏汤 治肺胃有热，呕渴少气。``
    if re.match(
        r"^[㐀-鿿A-Za-z]{1,12}(?:\s*[㐀-鿿A-Za-z]{0,8})?(?:汤|散|丸|饮|膏|丹|煎|胶)"
        r"(?:\s|$|[㐀-鿿A-Za-z0-9（(])",
        stripped,
    ):
        return True
    return False


def _looks_like_prescription_or_dose_line(value: str) -> bool:
    stripped = value.strip()
    if not stripped or stripped.startswith(("#", "|", "-", "*", "!", ">")):
        return False
    if re.match(r"^(?:处方|药用|方药|治法)[：:]", stripped):
        return True
    # Dense dose rows: multiple ``克`` tokens or leading date-like dose lines.
    if stripped.count("克") >= 2 and len(stripped) <= 240:
        return True
    if re.match(r"^(?:\d+(?:\.\d+)?\s*克\b)", stripped) and re.search(r"[㐀-鿿]", stripped):
        return True
    if re.match(r"^(?:共为|为粗末|每次|午、|早晚各服)", stripped):
        return True
    # Usage / pack count rows: ``（分两次服） 二付`` / ``日三服`` / ``杏仁一升，合皮熟，研用``
    if re.search(r"(?:分两次服|日[二三]服|二付|三付|研用|合皮熟)", stripped) and len(stripped) <= 40:
        return True
    if re.fullmatch(r"[（(][^）)]{1,16}[）)]\s*[一二三四五六七八九十两\d]+付", stripped):
        return True
    # Classical multi-herb dose rows with 两/钱/升/握 etc. (not only 克):
    # ``葱白一握 豆豉一升`` / ``滑石六两 甘草一两`` / ``高良姜酒炒 香附醋炒，等分``
    classical_dose = r"(?:两|钱|分|升|合|握|枚|片|粒|味|贴|帖|付|剂|匙|杯|碗|盅)"
    if (
        len(stripped) <= 80
        and len(re.findall(rf"(?:[一二三四五六七八九十百千万半两]+|\d+(?:\.\d+)?)\s*{classical_dose}", stripped)) >= 2
        and re.search(r"[㐀-鿿]", stripped)
        and not re.search(r"[。！？!?]", stripped)
    ):
        return True
    if (
        len(stripped) <= 60
        and re.search(r"(?:酒炒|醋炒|等分|各等分|盐水炒|姜汁炒)", stripped)
        and re.search(r"[㐀-鿿]{2,}", stripped)
        and not re.search(r"[。！？!?]", stripped)
    ):
        return True
    # Bare multi-herb formula rows without doses, common in classical case books:
    # ``熟地 归身 炙草 人参 肉桂`` / ``安桂 茯苓 於术 甘草``
    if _looks_like_bare_multi_herb_line(stripped):
        return True
    return False


def _looks_like_bare_multi_herb_line(value: str) -> bool:
    stripped = re.sub(r"\s+", " ", value).strip()
    if not stripped or len(stripped) > 80:
        return False
    if re.search(r"[。！？!?；;：:，,、0-9]", stripped):
        return False
    tokens = [tok for tok in re.split(r"[\s　]+", stripped) if tok]
    if len(tokens) < 3:
        return False
    # Each token is a short CJK herb / processed-herb name.
    if not all(re.fullmatch(r"[㐀-鿿A-Za-z]{1,6}", tok) for tok in tokens):
        return False
    return True


def _looks_like_case_heading_line(value: str) -> bool:
    return bool(re.match(r"^(?:案|例)\s*[一二三四五六七八九十百千〇零两\d]+(?:\s|[：:]|[㐀-鿿A-Za-z])", value.strip()))


def _looks_like_front_matter_line(value: str) -> bool:
    return bool(
        re.match(
            r"^(?:主编|编委|审定|编著|整理|作者|发行者|出版者|印刷者|经销者|邮政编码|传真|定价|书号|开本|ISBN|网址|社长热线|读者服务部电话|购书热线|官方微博|淘宝天猫网址|上架建议|中国版本图书馆|图书在版编目)",
            value.strip(),
        )
    )



def _has_general_forced_line_break(text: str) -> bool:
    return bool(forced_line_break_candidates(text, limit=1))



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
