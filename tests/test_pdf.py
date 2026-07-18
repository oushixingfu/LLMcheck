from __future__ import annotations

from llmcheck.pdf import _paginate_pdf_lines, _plain_text_for_pdf


def test_plain_text_for_pdf_strips_markdown_heading_markers() -> None:
    text = "# Title\n\n###### Deep Heading\n\nBody # marker stays.\n"

    rendered = _plain_text_for_pdf(text)

    assert rendered == "Title\n\nDeep Heading\n\nBody # marker stays.\n"


def test_paginate_pdf_lines_preserves_heading_levels_for_font_selection() -> None:
    lines = _paginate_pdf_lines("# Title\n\n## Section\n\n### Case\n\nBody", chars_per_line=80)

    assert lines == [
        ("Title", 1),
        ("", 0),
        ("Section", 2),
        ("", 0),
        ("Case", 3),
        ("", 0),
        ("Body", 0),
    ]
