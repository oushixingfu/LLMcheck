#!/usr/bin/env python3
"""Deliver complete LLM correction reports as final Markdown and text PDFs.

This is an incremental helper for long LLMcheck batch runs. It only delivers a
document when every correction chunk has non-empty corrected_text, then applies
the standard finalization/final-acceptance gate before writing outputs.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from llmcheck.pdf import write_text_pdf
from llmcheck.quality import (
    clean_markdown_text,
    final_acceptance_report,
    finalize_standard_document,
    safe_stem,
)


DEFAULT_LLM_FULL_DIR = Path("/mnt/d/pdf/output2/llm_full")
DEFAULT_SOURCE_DIR = Path("/mnt/d/pdf/output2/md")
DEFAULT_TARGET_PDF_DIR = Path("/mnt/d/pdf/output2/pdf")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Incrementally deliver LLMcheck documents whose correction reports "
            "contain corrected_text for every chunk."
        )
    )
    parser.add_argument(
        "--llm-full-dir",
        type=Path,
        default=DEFAULT_LLM_FULL_DIR,
        help="LLMcheck output directory containing process/books and md.",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help="Original normalized Markdown source directory, recorded in manifest only.",
    )
    parser.add_argument(
        "--md-dir",
        type=Path,
        default=None,
        help="Final Markdown output directory. Defaults to <llm-full-dir>/md.",
    )
    parser.add_argument(
        "--pdf-dir",
        type=Path,
        default=DEFAULT_TARGET_PDF_DIR,
        help="Final text PDF output directory.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Delivery manifest path. Defaults to <llm-full-dir>/process/llm_full_delivery_manifest.json.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report actions without writing Markdown, PDFs, or manifest.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate files even when both Markdown and PDF already exist and are non-empty.",
    )
    parser.add_argument(
        "--allow-final-acceptance-fail",
        action="store_true",
        help="Write outputs even when deterministic final acceptance has blocking errors.",
    )
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def chunk_index(chunk: dict[str, Any]) -> int:
    value = chunk.get("chunk_index", chunk.get("index", 0))
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def corrected_text_from_chunk(chunk: dict[str, Any]) -> str:
    llm_result = chunk.get("llm_result")
    if isinstance(llm_result, dict):
        value = llm_result.get("corrected_text")
        if isinstance(value, str):
            return value
    value = chunk.get("corrected_text")
    return value if isinstance(value, str) else ""


def chunk_error_text(chunk: dict[str, Any]) -> str:
    pieces: list[str] = []
    for value in (chunk.get("error"), chunk.get("status")):
        if value:
            pieces.append(str(value))
    llm_result = chunk.get("llm_result")
    if isinstance(llm_result, dict):
        for value in (llm_result.get("error"), llm_result.get("status")):
            if value:
                pieces.append(str(value))
    return " ".join(pieces)


def report_document_id(report: dict[str, Any], report_path: Path) -> str:
    value = report.get("document_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return report_path.name.removesuffix(".llm_correction.json")


def existing_manifest_rows(manifest_path: Path) -> dict[str, dict[str, Any]]:
    if not manifest_path.exists():
        return {}
    try:
        payload = load_json(manifest_path)
    except Exception:
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for row in payload.get("documents", []):
        if isinstance(row, dict) and isinstance(row.get("document_id"), str):
            rows[row["document_id"]] = row
    return rows


def deliver_report(
    report_path: Path,
    md_dir: Path,
    pdf_dir: Path,
    *,
    dry_run: bool,
    force: bool,
    allow_final_acceptance_fail: bool,
) -> dict[str, Any]:
    report = load_json(report_path)
    document_id = report_document_id(report, report_path)
    chunks = report.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return {
            "document_id": document_id,
            "status": "skipped",
            "reason": "no_chunks",
            "report": str(report_path),
        }

    texts: list[str] = []
    missing_indexes: list[int] = []
    http503_count = 0
    for chunk in sorted((c for c in chunks if isinstance(c, dict)), key=chunk_index):
        text = corrected_text_from_chunk(chunk)
        error_text = chunk_error_text(chunk)
        if "503" in error_text or "Service Unavailable" in error_text:
            http503_count += 1
        if text.strip():
            texts.append(text.strip())
        else:
            missing_indexes.append(chunk_index(chunk))

    if missing_indexes:
        return {
            "document_id": document_id,
            "status": "skipped",
            "reason": "missing_corrected_text",
            "chunks": len(chunks),
            "missing_count": len(missing_indexes),
            "http503_count": http503_count,
            "missing_indexes_sample": missing_indexes[:20],
            "report": str(report_path),
        }

    finalized = finalize_standard_document(
        clean_markdown_text("\n\n".join(texts).strip() + "\n")
    )
    final_text = finalized.get("text")
    if not isinstance(final_text, str):
        final_text = "\n\n".join(texts).strip() + "\n"
    final_acceptance = final_acceptance_report(final_text)
    accepted = final_acceptance.get("accepted") is True

    stem = safe_stem(document_id)
    md_path = md_dir / f"{stem}.md"
    pdf_path = pdf_dir / f"{stem}.pdf"
    mirror_paths = _mirror_output_paths(report_path=report_path, stem=stem)
    outputs_exist = (
        md_path.exists()
        and pdf_path.exists()
        and md_path.stat().st_size > 0
        and pdf_path.stat().st_size > 0
    )

    if not accepted and not allow_final_acceptance_fail:
        return {
            "document_id": document_id,
            "status": "skipped",
            "reason": "final_acceptance_failed",
            "chunks": len(chunks),
            "char_count": len(final_text),
            "final_acceptance": final_acceptance,
            "md_path": str(md_path),
            "pdf_path": str(pdf_path),
            "report": str(report_path),
        }

    action = "existing" if outputs_exist and not force else "created"
    if dry_run:
        action = "would_create" if not outputs_exist or force else "existing"
    elif not outputs_exist or force:
        _write_outputs(
            md_paths=[md_path, *mirror_paths["md"]],
            pdf_paths=[pdf_path, *mirror_paths["pdf"]],
            title=document_id,
            text=final_text,
        )

    return {
        "document_id": document_id,
        "status": "delivered",
        "action": action,
        "chunks": len(chunks),
        "char_count": len(final_text),
        "final_acceptance": final_acceptance,
        "md_path": str(md_path),
        "pdf_path": str(pdf_path),
        "mirrored_md_paths": [str(path) for path in mirror_paths["md"]],
        "mirrored_pdf_paths": [str(path) for path in mirror_paths["pdf"]],
        "report": str(report_path),
    }


def _mirror_output_paths(*, report_path: Path, stem: str) -> dict[str, list[Path]]:
    try:
        book_dir = report_path.parents[2]
        llm_full_dir = report_path.parents[5]
    except IndexError:
        return {"md": [], "pdf": []}
    if book_dir.name in {"", "books"}:
        return {"md": [], "pdf": []}
    delivery_stem = book_dir.name if book_dir.name[:4].isdigit() and book_dir.name[4:5] == "_" else stem
    return {
        "md": [
            book_dir / "md" / f"{stem}.md",
            llm_full_dir / "md" / f"{delivery_stem}.md",
        ],
        "pdf": [
            book_dir / "pdf" / f"{stem}.pdf",
            llm_full_dir / "pdf" / f"{delivery_stem}.pdf",
        ],
    }


def _write_outputs(*, md_paths: list[Path], pdf_paths: list[Path], title: str, text: str) -> None:
    seen: set[Path] = set()
    for path in md_paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    seen.clear()
    for path in pdf_paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_pdf(path, title=title, text=text)


def main() -> int:
    args = parse_args()
    llm_full_dir = args.llm_full_dir
    books_dir = llm_full_dir / "process" / "books"
    md_dir = args.md_dir or (llm_full_dir / "md")
    pdf_dir = args.pdf_dir
    manifest_path = args.manifest or (
        llm_full_dir / "process" / "llm_full_delivery_manifest.json"
    )

    if not books_dir.exists():
        print(f"ERROR: books directory does not exist: {books_dir}", file=sys.stderr)
        return 2

    rows = existing_manifest_rows(manifest_path)
    reports = sorted(books_dir.glob("*/process/reports/*.llm_correction.json"))
    current_rows: dict[str, dict[str, Any]] = {}
    created: list[str] = []
    existing = 0
    skipped = 0

    for report_path in reports:
        try:
            row = deliver_report(
                report_path,
                md_dir,
                pdf_dir,
                dry_run=args.dry_run,
                force=args.force,
                allow_final_acceptance_fail=args.allow_final_acceptance_fail,
            )
        except Exception as exc:
            row = {
                "document_id": str(report_path),
                "status": "error",
                "reason": repr(exc),
                "report": str(report_path),
            }

        document_id = row["document_id"]
        current_rows[document_id] = row
        rows[document_id] = row

        if row.get("status") == "delivered":
            if row.get("action") in {"created", "would_create"}:
                created.append(document_id)
            elif row.get("action") == "existing":
                existing += 1
        else:
            skipped += 1

    delivered_count = sum(1 for row in current_rows.values() if row.get("status") == "delivered")
    skipped_count = sum(1 for row in current_rows.values() if row.get("status") == "skipped")
    error_count = sum(1 for row in current_rows.values() if row.get("status") == "error")
    missing_chunks = sum(
        int(row.get("missing_count", 0))
        for row in current_rows.values()
        if row.get("reason") == "missing_corrected_text"
    )
    http503_chunks = sum(int(row.get("http503_count", 0)) for row in current_rows.values())

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": args.dry_run,
        "source_dir": str(args.source_dir),
        "llm_full_dir": str(llm_full_dir),
        "output_md": str(md_dir),
        "output_pdf": str(pdf_dir),
        "reports_seen": len(reports),
        "delivered_count": delivered_count,
        "skipped_count": skipped_count,
        "error_count": error_count,
        "created_or_would_create_count": len(created),
        "existing_count": existing,
        "missing_chunks": missing_chunks,
        "http503_chunks": http503_chunks,
        "documents": sorted(rows.values(), key=lambda row: row.get("document_id", "")),
    }

    if not args.dry_run:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(f"reports_seen={len(reports)}")
    print(f"delivered={delivered_count} skipped={skipped_count} errors={error_count}")
    print(f"created_or_would_create={len(created)} existing={existing}")
    print(f"missing_chunks={missing_chunks} http503_chunks={http503_chunks}")
    print(f"pdf_dir={pdf_dir}")
    if not args.dry_run:
        print(f"manifest={manifest_path}")
    if created:
        print("created_or_would_create_docs=" + " | ".join(created[:20]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
