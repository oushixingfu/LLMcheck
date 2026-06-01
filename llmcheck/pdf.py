from __future__ import annotations

from pathlib import Path
import re
import unicodedata


def write_text_pdf(
    path: Path,
    *,
    title: str,
    text: str,
    chars_per_line: int = 62,
    lines_per_page: int = 44,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = _paginate_lines(text, chars_per_line=chars_per_line)
    pages = [lines[index : index + lines_per_page] for index in range(0, len(lines), lines_per_page)] or [[]]
    objects: list[bytes] = []

    def add(obj: str | bytes) -> int:
        objects.append(obj.encode("utf-8") if isinstance(obj, str) else obj)
        return len(objects)

    add("<< /Type /Catalog /Pages 2 0 R >>")
    add("<< /Type /Pages /Kids [] /Count 0 >>")
    font_obj = add(
        "<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light "
        "/Encoding /UniGB-UCS2-H /DescendantFonts [4 0 R] >>"
    )
    add(
        "<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light "
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 2 >> "
        "/FontDescriptor 5 0 R /DW 1000 >>"
    )
    add(
        "<< /Type /FontDescriptor /FontName /STSong-Light /Flags 6 "
        "/FontBBox [0 -200 1000 900] /ItalicAngle 0 /Ascent 880 "
        "/Descent -120 /CapHeight 700 /StemV 80 >>"
    )

    page_object_ids: list[int] = []
    for page_number, page_lines in enumerate(pages, start=1):
        content = _page_content(title=title, page_number=page_number, page_count=len(pages), lines=page_lines)
        content_obj = add(
            b"<< /Length "
            + str(len(content)).encode("ascii")
            + b" >>\nstream\n"
            + content
            + b"\nendstream"
        )
        page_object_ids.append(
            add(
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                f"/Resources << /Font << /F1 {font_obj} 0 R >> >> /Contents {content_obj} 0 R >>"
            )
        )

    objects[1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{obj_id} 0 R' for obj_id in page_object_ids)}] "
        f"/Count {len(page_object_ids)} >>"
    ).encode("utf-8")
    _write_pdf_objects(path, objects)


def _paginate_lines(text: str, *, chars_per_line: int) -> list[str]:
    normalized = re.sub(r"\r\n?", "\n", text).strip()
    result: list[str] = []
    for raw_line in normalized.split("\n"):
        line = raw_line.rstrip()
        if not line:
            result.append("")
            continue
        while _display_width(line) > chars_per_line:
            head = _take_display_width(line, chars_per_line)
            result.append(head.rstrip())
            line = line[len(head) :]
        result.append(line)
    return result or [""]


def _page_content(*, title: str, page_number: int, page_count: int, lines: list[str]) -> bytes:
    commands = ["BT", "/F1 11 Tf", "15 TL"]
    header = f"{title}  第 {page_number}/{page_count} 页"
    commands.append(f"1 0 0 1 54 796 Tm {_pdf_hex_text(header)} Tj")
    y = 764
    for line in lines:
        commands.append(f"1 0 0 1 54 {y} Tm {_pdf_hex_text(line)} Tj")
        y -= 16
    commands.append("ET")
    return "\n".join(commands).encode("ascii")


def _write_pdf_objects(path: Path, objects: list[bytes]) -> None:
    chunks = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(sum(len(chunk) for chunk in chunks))
        chunks.append(f"{index} 0 obj\n".encode("ascii"))
        chunks.append(obj)
        chunks.append(b"\nendobj\n")
    xref_offset = sum(len(chunk) for chunk in chunks)
    chunks.append(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        chunks.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    chunks.append(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode(
            "ascii"
        )
    )
    path.write_bytes(b"".join(chunks))


def _pdf_hex_text(value: str) -> str:
    return "<" + value.encode("utf-16-be", errors="ignore").hex().upper() + ">"


def _display_width(value: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1 for char in value)


def _take_display_width(value: str, width: int) -> str:
    total = 0
    chars: list[str] = []
    for char in value:
        next_width = 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        if chars and total + next_width > width:
            break
        chars.append(char)
        total += next_width
    return "".join(chars)
