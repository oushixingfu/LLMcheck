"""Post-finalize repair helpers proven on the multi-hundred-book batch.

These run after structure finalize / clean so local-gate fails less often on
textbook plain MD and OCR exports. Gate thresholds themselves are unchanged:
mega_line stays 3000; repairs only make text more likely to pass.
"""

from __future__ import annotations

import re

from llmcheck.final_gate import (
    _looks_like_blank_separated_forced_break,
    _looks_like_forced_break,
    _looks_like_nonstandard_heading_content,
)

HEADING_RE = re.compile(r"^#{1,6}\s+\S")
# Stay under mega_line (3000) while allowing residual forced-break joins.
MAX_JOIN = 2_990
MAX_PACK_LINE = 2_490

LATEX_SAFE_SUBS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\$\$"), ""),
    (re.compile(r"(?<![\\])\$(?!\$)"), ""),
    (re.compile(r"\\mathrm\{([^}]*)\}"), r"\1"),
    (re.compile(r"\\mathbf\{([^}]*)\}"), r"\1"),
    (re.compile(r"\\text\{([^}]*)\}"), r"\1"),
    (re.compile(r"\\circ"), "°"),
    (re.compile(r"\\degree"), "°"),
    (re.compile(r"\\%"), "%"),
    (re.compile(r"\\times"), "×"),
    (re.compile(r"\\cdot"), "·"),
    (re.compile(r"\\sim"), "∼"),
    (re.compile(r"\\leq"), "≤"),
    (re.compile(r"\\geq"), "≥"),
    (re.compile(r"\\neq"), "≠"),
    (re.compile(r"\\approx"), "≈"),
    (re.compile(r"\\pm"), "±"),
    (re.compile(r"\\frac\{([^}]*)\}\{([^}]*)\}"), r"\1/\2"),
    (re.compile(r"\\[A-Za-z]+\{([^}]*)\}"), r"\1"),
    (re.compile(r"\\[A-Za-z]+"), ""),
    (re.compile(r"\{\s*\}"), ""),
    (re.compile(r"\$\s*\$"), ""),
]

TOC_PAGE_HEAD_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*[\.…·•．]{0,12}\s*(\d{1,4})\s*$")
TOC_PAGE_ONLY_RE = re.compile(r"^#{1,6}\s+(\d{1,4})\s*$")


def _safe_join_and_pack(prev: str, nxt: str, *, max_join: int = MAX_JOIN) -> list[str]:
    """Join forced wrap; if over max_join, pack into chunks at sentence ends."""
    joined = prev + nxt
    if len(joined) <= max_join:
        return [joined]
    chunks: list[str] = []
    rest = joined
    while len(rest) > max_join:
        window = rest[:max_join]
        cut = -1
        for punct in ("。", "！", "？", "；", ".", "!", "?", "…"):
            pos = window.rfind(punct)
            if pos > max_join // 3:
                cut = max(cut, pos + 1)
        if cut <= 0:
            cut = max_join
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        chunks.append(rest)
    return chunks


def merge_forced_breaks(text: str) -> str:
    """Merge OCR physical line breaks that the gate would flag as forced_line_breaks."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not out:
            out.append(line)
            i += 1
            continue

        prev_s = out[-1].rstrip()
        if HEADING_RE.match(prev_s):
            content = re.sub(r"^#{1,6}\s+", "", prev_s).strip()
            # Only demote dose-like or decorative junk headings mid-join, never book/series titles.
            if re.search(r"(丛书|全书|全集|教材|讲义|导言|出版|目录|内容提要)", content):
                pass
            elif _looks_like_nonstandard_heading_content(content) or re.search(
                r"\d+\s*(?:g|克|mg|ml)|\d+\s*剂", content
            ):
                prev_s = content
                out[-1] = content

        if (
            stripped
            and prev_s
            and not HEADING_RE.match(prev_s)
            and not HEADING_RE.match(stripped)
            and not stripped.startswith(("|", "-", "*", ">"))
            and not prev_s.startswith(("|", "-", "*", ">"))
            and _looks_like_forced_break(prev_s, stripped)
        ):
            packed = _safe_join_and_pack(prev_s, stripped)
            out[-1] = packed[0]
            out.extend(packed[1:])
            i += 1
            continue

        if not stripped and prev_s and not HEADING_RE.match(prev_s):
            j = i
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                nxt = lines[j].strip()
                if HEADING_RE.match(nxt):
                    content = re.sub(r"^#{1,6}\s+", "", nxt).strip()
                    if re.search(r"(丛书|全书|全集|教材|讲义|导言|出版|目录|内容提要)", content):
                        pass
                    elif _looks_like_nonstandard_heading_content(content) or re.search(
                        r"\d+\s*(?:g|克|mg|ml)|\d+\s*剂", content
                    ):
                        nxt = content
                if (
                    nxt
                    and not HEADING_RE.match(nxt)
                    and not nxt.startswith(("|", "-", "*", ">"))
                    and (
                        _looks_like_blank_separated_forced_break(prev_s, nxt)
                        or _looks_like_forced_break(prev_s, nxt)
                    )
                ):
                    packed = _safe_join_and_pack(prev_s, nxt)
                    out[-1] = packed[0]
                    out.extend(packed[1:])
                    i = j + 1
                    continue

        out.append(line)
        i += 1

    # Second pass for residual adjacent wraps after first merges.
    for _ in range(4):
        changed = False
        out2: list[str] = []
        for line in out:
            stripped = line.strip()
            if not out2:
                out2.append(line)
                continue
            prev_s = out2[-1].rstrip()
            if (
                stripped
                and prev_s
                and not HEADING_RE.match(prev_s)
                and not HEADING_RE.match(stripped)
                and not stripped.startswith(("|", "-", "*", ">"))
                and _looks_like_forced_break(prev_s, stripped)
            ):
                packed = _safe_join_and_pack(prev_s, stripped)
                out2[-1] = packed[0]
                out2.extend(packed[1:])
                changed = True
            else:
                out2.append(line)
        out = out2
        if not changed:
            break
    return "\n".join(out)


def split_overlong_lines(text: str, *, max_len: int = MAX_PACK_LINE) -> str:
    """Split body lines longer than max_len at sentence punctuation when possible."""
    out: list[str] = []
    in_fence = False
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            out.append(raw)
            continue
        if in_fence or HEADING_RE.match(s) or s.startswith(("|", "-", "*", ">", "![")):
            out.append(raw)
            continue
        if len(raw) <= max_len:
            out.append(raw)
            continue
        rest = raw
        while len(rest) > max_len:
            window = rest[:max_len]
            cut = -1
            for punct in ("。", "！", "？", "；", ".", "!", "?", "…"):
                pos = window.rfind(punct)
                if pos > max_len // 3:
                    cut = max(cut, pos + 1)
            if cut <= 0:
                for punct in ("，", "、", ",", ":", "："):
                    pos = window.rfind(punct)
                    if pos > max_len // 2:
                        cut = max(cut, pos + 1)
            if cut <= 0:
                cut = max_len
            out.append(rest[:cut])
            rest = rest[cut:]
        if rest:
            out.append(rest)
    return "\n".join(out)


def strip_latex_artifacts(text: str) -> str:
    cleaned = text
    for pat, repl in LATEX_SAFE_SUBS:
        cleaned = pat.sub(repl, cleaned)
    cleaned = re.sub(r"\\(?=[一-鿿\s,，.。])", "", cleaned)
    return cleaned


def demote_nonstandard_headings(text: str) -> str:
    """Demote dose/decorative/garbage headings only; keep real section titles."""
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"^(#{1,6})\s+(.+?)\s*$", s)
        if not m:
            out.append(line)
            continue
        content = m.group(2).strip()
        # Keep book/series titles used as structural anchors.
        if re.search(r"(丛书|全书|全集|教材|讲义|导言|出版|目录|内容提要)", content):
            out.append(line)
            continue
        if _looks_like_nonstandard_heading_content(content):
            out.append(content)
        else:
            out.append(line)
    return "\n".join(out)


def fix_toc_page_headings(text: str) -> str:
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not HEADING_RE.match(s):
            out.append(line)
            continue
        m_only = TOC_PAGE_ONLY_RE.match(s)
        if m_only:
            out.append(f"- {m_only.group(1)}")
            continue
        m = TOC_PAGE_HEAD_RE.match(s)
        if m:
            title = m.group(2).strip()
            page = m.group(3)
            if re.search(r"[\.…·•．]{2,}", s) or len(title) <= 40:
                out.append(f"- {title} {page}".rstrip())
                continue
        out.append(line)
    return "\n".join(out)


def dedupe_short_repeated_lines(text: str, max_keep: int = 2) -> str:
    counts: dict[str, int] = {}
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or len(s) > 80 or HEADING_RE.match(s):
            out.append(line)
            continue
        counts[s] = counts.get(s, 0) + 1
        if counts[s] <= max_keep:
            out.append(line)
    return "\n".join(out)


def apply_batch_proven_repairs(text: str) -> tuple[str, list[str]]:
    """Apply the repair suite once; return text and change labels."""
    applied: list[str] = []
    before = text

    t = demote_nonstandard_headings(text)
    if t != text:
        applied.append("demote_nonstandard_headings")
        text = t

    t = fix_toc_page_headings(text)
    if t != text:
        applied.append("fix_toc_page_headings")
        text = t

    t = strip_latex_artifacts(text)
    if t != text:
        applied.append("strip_latex_artifacts")
        text = t

    t = merge_forced_breaks(text)
    if t != text:
        applied.append("merge_forced_breaks")
        text = t

    t = dedupe_short_repeated_lines(text)
    if t != text:
        applied.append("dedupe_short_repeated_lines")
        text = t

    t = split_overlong_lines(text)
    if t != text:
        applied.append("split_overlong_lines")
        text = t

    if text != before and not applied:
        applied.append("repaired")
    return text, applied
