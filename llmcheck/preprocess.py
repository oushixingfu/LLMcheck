from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable
import hashlib
import http.client
import json
import os
import re
import socket
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from llmcheck.cleaning import safe_stem
from llmcheck.cross import write_cross_artifacts


MARKDOWN_SUFFIXES = {".md"}
PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".jp2", ".webp", ".gif", ".bmp"}
WORD_SUFFIXES = {".doc", ".docx"}
OFFICE_SUFFIXES = {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
SUPPORTED_SUFFIXES = MARKDOWN_SUFFIXES | PDF_SUFFIXES | IMAGE_SUFFIXES | OFFICE_SUFFIXES
DEFAULT_MINERU_BASE_URL = "https://mineru.net"
DEFAULT_MINERU_MODEL = "vlm"
DEFAULT_PDF_PAGE_CHUNK_SIZE = 30
DEFAULT_MINERU_BATCH_SIZE = 50
DEFAULT_MINERU_REQUEST_TIMEOUT_SECONDS = 60
DEFAULT_MINERU_MAX_RETRIES = 3
DEFAULT_MINERU_RETRY_BACKOFF_SECONDS = 2.0
DEFAULT_PPX_COMMAND = "/mnt/d/codex/memect-ppx/ppx"
DEFAULT_PPX_CWD = "/mnt/d/codex/memect-ppx"
RETRYABLE_HTTP_STATUS = {408, 429, 500, 502, 503, 504}
TRANSIENT_NETWORK_ERRORS = (TimeoutError, urllib.error.URLError, socket.timeout)


class SourceKind(StrEnum):
    MARKDOWN = "markdown"
    PDF = "pdf"
    WORD = "word"
    MINERU_ONLY = "mineru_only"


class MinerUTransientError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceFile:
    path: Path
    kind: SourceKind


@dataclass(frozen=True)
class PreprocessSettings:
    mineru_api_url: str = DEFAULT_MINERU_BASE_URL
    mineru_api_key: str = ""
    mineru_model: str = DEFAULT_MINERU_MODEL
    mineru_concurrency: int = 12
    mineru_batch_size: int = DEFAULT_MINERU_BATCH_SIZE
    mineru_poll_interval_seconds: int = 3
    mineru_timeout_seconds: int = 3600
    mineru_request_timeout_seconds: int = DEFAULT_MINERU_REQUEST_TIMEOUT_SECONDS
    mineru_max_retries: int = DEFAULT_MINERU_MAX_RETRIES
    mineru_retry_backoff_seconds: float = DEFAULT_MINERU_RETRY_BACKOFF_SECONDS
    mineru_fallback: str = "ppx"
    pdf_page_chunk_size: int = DEFAULT_PDF_PAGE_CHUNK_SIZE
    ppx_command: str = DEFAULT_PPX_COMMAND
    ppx_cwd: str = DEFAULT_PPX_CWD
    ppx_timeout_seconds: int = 3600
    ppx_backend: str = "default"
    ppx_ocr: str = "auto"
    ppx_formula: str = "no"


@dataclass(frozen=True)
class MinerUFile:
    path: Path
    name: str
    data_id: str


PdfPageCounter = Callable[[Path], int]
PdfSplitter = Callable[[Path], Any]
PpxRunner = Callable[[Path], Any]
MinerURunner = Callable[[list[Path]], Path]


def discover_source_files(input_path: Path) -> list[SourceFile]:
    resolved = input_path.expanduser().resolve()
    if resolved.is_file():
        source = _source_file(resolved)
        return [source] if source is not None else []
    if resolved.is_dir():
        rows = [_source_file(path) for path in sorted(resolved.rglob("*")) if path.is_file()]
        return [row for row in rows if row is not None]
    return []


def prepare_markdown_inputs(
    *,
    input_path: Path,
    output_dir: Path,
    settings: PreprocessSettings,
    page_count_reader: PdfPageCounter | None = None,
    pdf_splitter: Callable[..., list[Path]] | None = None,
    ppx_runner: Callable[..., Path] | None = None,
    mineru_runner: Callable[..., Path] | None = None,
) -> list[Path]:
    sources = discover_source_files(input_path)
    if not sources:
        return []
    markdowns: list[Path] = []
    preprocess_root = output_dir / "preprocess"
    for source in sources:
        work_dir = preprocess_root / safe_stem(source.path.stem)
        if source.kind == SourceKind.MARKDOWN:
            formal_path = _write_existing_markdown_manifest(path=source.path, work_dir=work_dir)
            markdowns.append(formal_path)
            continue
        if source.kind == SourceKind.PDF:
            markdowns.append(
                _prepare_pdf_markdown(
                    source.path,
                    work_dir=work_dir,
                    settings=settings,
                    page_count_reader=page_count_reader or pdf_page_count,
                    pdf_splitter=pdf_splitter or split_pdf_for_mineru,
                    ppx_runner=ppx_runner or run_ppx,
                    mineru_runner=mineru_runner or run_mineru_vlm,
                )
            )
            continue
        if source.kind == SourceKind.WORD:
            markdowns.append(
                _prepare_word_markdown(
                    source.path,
                    work_dir=work_dir,
                    settings=settings,
                    page_count_reader=page_count_reader or pdf_page_count,
                    ppx_runner=ppx_runner or run_ppx,
                    mineru_runner=mineru_runner or run_mineru_vlm,
                )
            )
            continue
        markdowns.append(
            _prepare_mineru_only_markdown(
                source.path,
                work_dir=work_dir,
                settings=settings,
                ppx_runner=ppx_runner or run_ppx,
                mineru_runner=mineru_runner or run_mineru_vlm,
            )
        )
    return markdowns


def page_segments(page_count: int, *, max_pages: int = 200) -> list[tuple[int, int]]:
    if page_count < 1:
        return []
    max_pages = max(1, max_pages)
    return [(start, min(start + max_pages - 1, page_count)) for start in range(1, page_count + 1, max_pages)]


def pdf_page_count(path: Path) -> int:
    try:
        completed = subprocess.run(["pdfinfo", str(path)], text=True, capture_output=True, check=False, timeout=15)
    except FileNotFoundError:
        return _pdf_page_count_with_pypdf(path)
    match = re.search(r"^Pages:\s+(\d+)", completed.stdout, re.MULTILINE)
    if match:
        return int(match.group(1))
    try:
        return _pdf_page_count_with_pypdf(path)
    except Exception as error:
        raise RuntimeError(f"无法读取 PDF 页数：{path}\n{completed.stderr[-500:]}") from error


def split_pdf_for_mineru(path: Path, *, output_dir: Path, max_pages: int = DEFAULT_PDF_PAGE_CHUNK_SIZE) -> list[Path]:
    page_count = pdf_page_count(path)
    if page_count <= max_pages:
        return [path]
    output_dir.mkdir(parents=True, exist_ok=True)
    segments: list[Path] = []
    for index, (start, end) in enumerate(page_segments(page_count, max_pages=max_pages), start=1):
        segment_path = output_dir / f"segment_{start:04d}_{end:04d}.pdf"
        if not segment_path.exists():
            _write_pdf_segment(source_path=path, start=start, end=end, segment_path=segment_path)
        segments.append(segment_path)
    return segments


def _pdf_page_count_with_pypdf(path: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(path)).pages)


def _write_pdf_segment(*, source_path: Path, start: int, end: int, segment_path: Path) -> None:
    segment_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["qpdf", "--empty", "--pages", str(source_path), f"{start}-{end}", "--", str(segment_path)],
            text=True,
            capture_output=True,
            check=True,
        )
        return
    except FileNotFoundError:
        pass
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(source_path))
    writer = PdfWriter()
    for page_index in range(start - 1, min(end, len(reader.pages))):
        writer.add_page(reader.pages[page_index])
    with segment_path.open("wb") as handle:
        writer.write(handle)


def run_ppx(path: Path, *, output_dir: Path, settings: PreprocessSettings) -> Path:
    clean_path = output_dir / "clean" / "ppx.md"
    if clean_path.exists() and clean_path.stat().st_size > 0:
        return clean_path
    command_path = Path(settings.ppx_command)
    if not command_path.exists():
        raise RuntimeError(f"PPX 命令不存在：{command_path}")
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(command_path),
        "parse",
        str(path),
        "--out-dir",
        str(raw_dir),
        "--md",
        "--json",
        "--backend",
        settings.ppx_backend or "default",
        "--ocr",
        settings.ppx_ocr or "auto",
        "--formula",
        settings.ppx_formula or "no",
    ]
    completed = subprocess.run(
        command,
        cwd=str(Path(settings.ppx_cwd)) if settings.ppx_cwd else None,
        text=True,
        capture_output=True,
        check=False,
        timeout=settings.ppx_timeout_seconds,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "PPX 转换失败")[-2000:])
    markdown = _largest_markdown(raw_dir)
    clean_dir = output_dir / "clean"
    clean_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(markdown, clean_path)
    return clean_path


def run_mineru_vlm(files: list[Path], *, output_dir: Path, settings: PreprocessSettings) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    cached = _cached_mineru_markdown(files, output_dir=output_dir)
    if cached is not None:
        return cached
    if not settings.mineru_api_key:
        raise RuntimeError("需要配置 MinerU API key")
    ordered_results = _run_mineru_file_batches(
        files=files,
        output_dirs=[output_dir / f"segment_{index + 1:03d}" for index in range(len(files))],
        indexes=[index + 1 for index in range(len(files))],
        settings=settings,
        client=_mineru_client(settings),
    )
    return _write_mineru_result(files=files, ordered_results=ordered_results, output_dir=output_dir, settings=settings)


def run_mineru_vlm_for_pdf_chunks(
    path: Path,
    *,
    page_count: int,
    segment_dir: Path,
    output_dir: Path,
    settings: PreprocessSettings,
) -> Path:
    segments = page_segments(page_count, max_pages=settings.pdf_page_chunk_size)
    # Prefer source-stem naming for stable cache keys; fall back to generic segment_* names.
    segment_paths = [segment_dir / f"{safe_stem(path.stem)}__pages_{start:04d}_{end:04d}.pdf" for start, end in segments]
    cached = _cached_mineru_markdown(segment_paths, output_dir=output_dir)
    if cached is not None:
        return cached
    if not settings.mineru_api_key:
        raise RuntimeError("需要配置 MinerU API key")
    output_dir.mkdir(parents=True, exist_ok=True)
    segment_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=max(1, settings.mineru_concurrency)) as executor:
        split_futures = [
            executor.submit(
                _ensure_pdf_segment,
                source_path=path,
                start=start,
                end=end,
                segment_path=segment_path,
            )
            for (start, end), segment_path in zip(segments, segment_paths, strict=True)
        ]
        for future in as_completed(split_futures):
            future.result()
    ordered_results = _run_mineru_file_batches(
        files=segment_paths,
        output_dirs=[output_dir / f"segment_{index:03d}" for index in range(1, len(segment_paths) + 1)],
        indexes=[index for index in range(1, len(segment_paths) + 1)],
        settings=settings,
        client=_mineru_client(settings),
    )
    return _write_mineru_result(files=segment_paths, ordered_results=ordered_results, output_dir=output_dir, settings=settings)


def _write_existing_markdown_manifest(*, path: Path, work_dir: Path) -> Path:
    formal_path, cross_report = _finalize_cross_selection(
        work_dir,
        mineru_path=None,
        ppx_path=None,
        existing_md_path=path,
    )
    path_hash = _sha256_file(path)
    formal_hash = _sha256_file(formal_path) if formal_path.exists() else path_hash
    manifest = {
        "source": str(path),
        "kind": SourceKind.MARKDOWN.value,
        "formal_markdown": str(formal_path),
        "cross_report": str(work_dir / "cross" / "cross_report.json"),
        "acquisition_mode": str(cross_report.get("mode") or "existing_md"),
        "source_sha256": path_hash,
        "formal_markdown_sha256": formal_hash,
    }
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "preprocess_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return formal_path


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mineru_client(settings: PreprocessSettings) -> MinerUClient:
    return MinerUClient(
        token=settings.mineru_api_key,
        base_url=settings.mineru_api_url or DEFAULT_MINERU_BASE_URL,
        timeout_seconds=max(10, settings.mineru_request_timeout_seconds),
        max_retries=max(1, settings.mineru_max_retries),
        retry_backoff_seconds=max(0.0, settings.mineru_retry_backoff_seconds),
    )


def _write_mineru_result(*, files: list[Path], ordered_results: list[Path | None], output_dir: Path, settings: PreprocessSettings) -> Path:
    markdown_paths = [path for path in ordered_results if path is not None]
    merged_path = output_dir / "mineru_vlm.md"
    merged_path.write_text(
        "\n\n".join(path.read_text(encoding="utf-8", errors="replace").strip() for path in markdown_paths if path.exists()).strip() + "\n",
        encoding="utf-8",
    )
    manifest = {
        "status": "converted",
        "model": settings.mineru_model,
        "source_files": [str(path) for path in files],
        "segment_markdowns": [str(path) for path in markdown_paths],
        "merged_markdown": str(merged_path),
    }
    (output_dir / "mineru_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return merged_path


def _cached_mineru_markdown(files: list[Path], *, output_dir: Path) -> Path | None:
    manifest_path = output_dir / "mineru_manifest.json"
    merged_path = output_dir / "mineru_vlm.md"
    if not manifest_path.exists() or not merged_path.exists() or merged_path.stat().st_size == 0:
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    expected_sources = [str(path) for path in files]
    if manifest.get("status") != "converted" or manifest.get("source_files") != expected_sources:
        return None
    segment_markdowns = manifest.get("segment_markdowns")
    if not isinstance(segment_markdowns, list) or not all(Path(str(path)).exists() for path in segment_markdowns):
        return None
    return merged_path


class MinerUClient:
    def __init__(
        self,
        *,
        token: str,
        base_url: str = DEFAULT_MINERU_BASE_URL,
        timeout_seconds: int = DEFAULT_MINERU_REQUEST_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MINERU_MAX_RETRIES,
        retry_backoff_seconds: float = DEFAULT_MINERU_RETRY_BACKOFF_SECONDS,
    ) -> None:
        self.token = token.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(1, max_retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        if not self.token:
            raise ValueError("MinerU API token 不能为空")

    def create_upload_batch(self, *, files: list[MinerUFile], model_version: str = DEFAULT_MINERU_MODEL) -> tuple[str, list[str]]:
        payload = {
            "files": [{"name": file.name, "data_id": file.data_id, "is_ocr": True} for file in files],
            "model_version": model_version,
            "language": "ch",
            "enable_formula": True,
            "enable_table": True,
        }
        data = _successful_data(self._request_json("POST", "/api/v4/file-urls/batch", payload))
        return str(data["batch_id"]), [str(url) for url in data["file_urls"]]

    def upload_files(self, *, files: list[MinerUFile], upload_urls: list[str]) -> None:
        if len(files) != len(upload_urls):
            raise ValueError("上传文件数与 MinerU 返回 URL 数不一致")
        for file, upload_url in zip(files, upload_urls, strict=True):
            data = file.path.read_bytes()
            status, reason, body = self._with_retries(
                lambda: _put_file_without_content_type(upload_url, data, timeout_seconds=self.timeout_seconds),
                description=f"上传 {file.name}",
            )
            if status in RETRYABLE_HTTP_STATUS:
                raise RuntimeError(f"上传 {file.name} 失败，HTTP {status} {reason}: {body[:200]}")
            if status >= 400:
                raise RuntimeError(f"上传 {file.name} 失败，HTTP {status} {reason}: {body[:200]}")

    def poll_batch_results(
        self,
        *,
        batch_id: str,
        poll_interval_seconds: int,
        timeout_seconds: int,
        on_poll: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout_seconds
        last_results: list[dict[str, Any]] = []
        last_error = ""
        while time.monotonic() < deadline:
            try:
                data = _successful_data(self._request_json("GET", f"/api/v4/extract-results/batch/{batch_id}"))
            except MinerUTransientError as error:
                last_error = str(error)
                time.sleep(poll_interval_seconds)
                continue
            results = _extract_result_rows(data)
            last_results = results
            if on_poll is not None:
                on_poll(results)
            states = {str(row.get("state", "")).lower() for row in results}
            if states and states <= {"done"}:
                return results
            if states & {"failed"}:
                raise RuntimeError(f"MinerU 解析失败：{results}")
            time.sleep(poll_interval_seconds)
        suffix = f"，最后网络错误：{last_error}" if last_error else ""
        raise TimeoutError(f"MinerU batch {batch_id} 超时，最后状态：{last_results}{suffix}")

    def download_result_zip(self, *, full_zip_url: str, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return self._with_retries(lambda: self._download_once(full_zip_url=full_zip_url, output_path=output_path), description="下载 MinerU 结果")

    def _request_json(self, method: str, api_path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{api_path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "*/*",
            },
        )
        for attempt in range(1, self.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                body = error.read().decode("utf-8", errors="replace")
                if error.code in RETRYABLE_HTTP_STATUS and attempt < self.max_retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise RuntimeError(f"MinerU API HTTP {error.code}: {body}") from error
            except TRANSIENT_NETWORK_ERRORS as error:
                if attempt < self.max_retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise MinerUTransientError(f"MinerU API 请求网络失败：{error}") from error
        raise AssertionError("unreachable MinerU request retry state")

    def _with_retries(self, operation: Callable[[], Any], *, description: str) -> Any:
        for attempt in range(1, self.max_retries + 1):
            try:
                result = operation()
            except TRANSIENT_NETWORK_ERRORS as error:
                if attempt < self.max_retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise MinerUTransientError(f"{description}网络失败：{error}") from error
            if isinstance(result, tuple) and result and isinstance(result[0], int) and result[0] in RETRYABLE_HTTP_STATUS and attempt < self.max_retries:
                self._sleep_before_retry(attempt)
                continue
            return result
        raise AssertionError("unreachable MinerU retry state")

    def _download_once(self, *, full_zip_url: str, output_path: Path) -> Path:
        with urllib.request.urlopen(full_zip_url, timeout=self.timeout_seconds) as response:
            output_path.write_bytes(response.read())
        return output_path

    def _sleep_before_retry(self, attempt: int) -> None:
        time.sleep(self.retry_backoff_seconds * attempt)


def _prepare_pdf_markdown(
    path: Path,
    *,
    work_dir: Path,
    settings: PreprocessSettings,
    page_count_reader: PdfPageCounter,
    pdf_splitter: Callable[..., list[Path]],
    ppx_runner: Callable[..., Path],
    mineru_runner: Callable[..., Path],
) -> Path:
    page_count = page_count_reader(path)
    if not settings.mineru_api_key:
        cached_mineru = _cached_pdf_mineru_markdown(
            path,
            page_count=page_count,
            work_dir=work_dir,
            settings=settings,
            pdf_splitter=pdf_splitter,
            mineru_runner=mineru_runner,
        )
        if cached_mineru is None:
            if not _ppx_fallback_enabled(settings):
                raise RuntimeError("missing MinerU API key and PPX fallback is disabled")
            ppx_path = _run_ppx_audit_foreground(path, work_dir=work_dir, settings=settings, ppx_runner=ppx_runner)
            formal_path, cross_report = _finalize_cross_selection(
                work_dir,
                mineru_path=None,
                ppx_path=ppx_path,
            )
            _write_pdf_preprocess_manifest(
                path=path,
                work_dir=work_dir,
                page_count=page_count,
                ppx_path=ppx_path,
                mineru_path=None,
                formal_path=formal_path,
                settings=settings,
                acquisition_mode=str(cross_report.get("mode") or "ppx_fallback"),
                mineru_error="missing MinerU API key",
            )
            return formal_path
        ppx_path = work_dir / "ppx" / "clean" / "ppx.md"
        formal_path, cross_report = _finalize_cross_selection(
            work_dir,
            mineru_path=cached_mineru,
            ppx_path=ppx_path if ppx_path.exists() else None,
        )
        _write_pdf_preprocess_manifest(
            path=path,
            work_dir=work_dir,
            page_count=page_count,
            ppx_path=ppx_path,
            mineru_path=cached_mineru,
            formal_path=formal_path,
            settings=settings,
            acquisition_mode=str(cross_report.get("mode") or "mineru_only"),
        )
        return formal_path
    ppx_path = _start_ppx_audit_background(path, work_dir=work_dir, settings=settings, ppx_runner=ppx_runner)
    with ThreadPoolExecutor(max_workers=1) as executor:
        if page_count > settings.pdf_page_chunk_size:
            if pdf_splitter is split_pdf_for_mineru and mineru_runner is run_mineru_vlm:
                segment_paths = []
                mineru_future = executor.submit(
                    run_mineru_vlm_for_pdf_chunks,
                    path,
                    page_count=page_count,
                    segment_dir=work_dir / "mineru_segments",
                    output_dir=work_dir / "mineru",
                    settings=settings,
                )
            else:
                segment_paths = pdf_splitter(path, output_dir=work_dir / "mineru_segments", max_pages=settings.pdf_page_chunk_size)
                mineru_future = executor.submit(mineru_runner, segment_paths, output_dir=work_dir / "mineru", settings=settings)
        else:
            segment_paths = [path]
            mineru_future = executor.submit(mineru_runner, segment_paths, output_dir=work_dir / "mineru", settings=settings)
        try:
            mineru_path = mineru_future.result()
        except Exception as error:
            if not _ppx_fallback_enabled(settings):
                raise
            ppx_path = _run_ppx_audit_foreground(path, work_dir=work_dir, settings=settings, ppx_runner=ppx_runner)
            formal_path, cross_report = _finalize_cross_selection(
                work_dir,
                mineru_path=None,
                ppx_path=ppx_path,
            )
            _write_pdf_preprocess_manifest(
                path=path,
                work_dir=work_dir,
                page_count=page_count,
                ppx_path=ppx_path,
                mineru_path=None,
                formal_path=formal_path,
                settings=settings,
                acquisition_mode=str(cross_report.get("mode") or "ppx_fallback"),
                mineru_error=str(error),
            )
            return formal_path
    # Pass the expected PPX path even if the background audit is still running;
    # missing/empty PPX is scored as an unusable dual candidate (selected_mineru).
    formal_path, cross_report = _finalize_cross_selection(
        work_dir,
        mineru_path=mineru_path,
        ppx_path=_resolve_ppx_candidate(work_dir, preferred=ppx_path) or ppx_path,
    )
    _write_pdf_preprocess_manifest(
        path=path,
        work_dir=work_dir,
        page_count=page_count,
        ppx_path=ppx_path,
        mineru_path=mineru_path,
        formal_path=formal_path,
        settings=settings,
        acquisition_mode=str(cross_report.get("mode") or "selected_mineru"),
    )
    return formal_path


def _cached_pdf_mineru_markdown(
    path: Path,
    *,
    page_count: int,
    work_dir: Path,
    settings: PreprocessSettings,
    pdf_splitter: Callable[..., list[Path]],
    mineru_runner: Callable[..., Path],
) -> Path | None:
    if mineru_runner is not run_mineru_vlm:
        return None
    if page_count > settings.pdf_page_chunk_size:
        if pdf_splitter is not split_pdf_for_mineru:
            return None
        segments = page_segments(page_count, max_pages=settings.pdf_page_chunk_size)
        segment_dir = work_dir / "mineru_segments"
        segment_paths = [segment_dir / f"{safe_stem(path.stem)}__pages_{start:04d}_{end:04d}.pdf" for start, end in segments]
        return _cached_mineru_markdown(segment_paths, output_dir=work_dir / "mineru")
    return _cached_mineru_markdown([path], output_dir=work_dir / "mineru")


def _write_pdf_preprocess_manifest(
    *,
    path: Path,
    work_dir: Path,
    page_count: int,
    ppx_path: Path,
    mineru_path: Path | None,
    formal_path: Path,
    settings: PreprocessSettings,
    acquisition_mode: str,
    mineru_error: str = "",
) -> None:
    manifest = {
        "source": str(path),
        "kind": SourceKind.PDF.value,
        "page_count": page_count,
        "ppx_markdown": str(ppx_path),
        "ppx_audit_status": str(work_dir / "ppx" / "ppx_audit_status.json"),
        "mineru_markdown": str(mineru_path) if mineru_path is not None else "",
        "formal_markdown": str(formal_path),
        "cross_report": str(work_dir / "cross" / "cross_report.json"),
        "acquisition_mode": acquisition_mode,
        "mineru_error": mineru_error,
        "mineru_model": settings.mineru_model,
    }
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "preprocess_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _run_ppx_audit_foreground(
    path: Path,
    *,
    work_dir: Path,
    settings: PreprocessSettings,
    ppx_runner: Callable[..., Path],
) -> Path:
    ppx_dir = work_dir / "ppx"
    expected_path = ppx_dir / "clean" / "ppx.md"
    status_path = ppx_dir / "ppx_audit_status.json"
    if expected_path.exists() and expected_path.stat().st_size > 0:
        _write_ppx_audit_status(status_path, source=path, status="cached", ppx_markdown=expected_path)
        return expected_path
    _write_ppx_audit_status(status_path, source=path, status="running", ppx_markdown=expected_path)
    try:
        actual_path = ppx_runner(path, output_dir=ppx_dir, settings=settings)
    except Exception as error:
        _write_ppx_audit_status(status_path, source=path, status="failed", ppx_markdown=expected_path, error=str(error))
        raise
    _write_ppx_audit_status(status_path, source=path, status="completed", ppx_markdown=actual_path)
    return actual_path


def _start_ppx_audit_background(
    path: Path,
    *,
    work_dir: Path,
    settings: PreprocessSettings,
    ppx_runner: Callable[..., Path],
) -> Path:
    ppx_dir = work_dir / "ppx"
    expected_path = ppx_dir / "clean" / "ppx.md"
    status_path = ppx_dir / "ppx_audit_status.json"
    if expected_path.exists() and expected_path.stat().st_size > 0:
        _write_ppx_audit_status(status_path, source=path, status="cached", ppx_markdown=expected_path)
        return expected_path

    def run_audit() -> None:
        _write_ppx_audit_status(status_path, source=path, status="running", ppx_markdown=expected_path, thread_name=threading.current_thread().name)
        try:
            actual_path = ppx_runner(path, output_dir=ppx_dir, settings=settings)
        except Exception as error:  # noqa: BLE001 - PPX is an audit path; formal text can proceed from MinerU.
            _write_ppx_audit_status(status_path, source=path, status="failed", ppx_markdown=expected_path, error=str(error))
            return
        _write_ppx_audit_status(status_path, source=path, status="completed", ppx_markdown=actual_path)

    thread = threading.Thread(target=run_audit, name=f"llmcheck-ppx-audit-{safe_stem(path.stem)}", daemon=True)
    thread.start()
    return expected_path


def _ppx_fallback_enabled(settings: PreprocessSettings) -> bool:
    return str(getattr(settings, "mineru_fallback", "ppx") or "ppx").strip().lower() == "ppx"


def _write_ppx_audit_status(
    path: Path,
    *,
    source: Path,
    status: str,
    ppx_markdown: Path,
    error: str = "",
    thread_name: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "source": str(source),
                "status": status,
                "ppx_markdown": str(ppx_markdown),
                "error": error,
                "pid": os.getpid(),
                "thread_name": thread_name,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

def _write_mineru_segment_status(path: Path, **payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _read_mineru_segment_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _resumable_mineru_batch_id(status_path: Path, *, file_path: Path) -> str:
    status = _read_mineru_segment_status(status_path)
    if str(status.get("source") or "") != str(file_path):
        return ""
    batch_id = str(status.get("batch_id") or "")
    status_value = str(status.get("status") or "").lower()
    recovered_from_timeout_error = False
    if not batch_id:
        error_text = str(status.get("error") or "")
        match = re.search(r"MinerU batch ([0-9a-fA-F-]{36})", error_text)
        batch_id = match.group(1) if match else ""
        recovered_from_timeout_error = bool(batch_id and ("超时" in error_text or "timeout" in error_text.lower() or status_value == "failed"))
    if not batch_id:
        return ""
    previous_status = str(status.get("previous_status") or "").lower()
    if status_value in {"polling", "downloading"}:
        return batch_id
    if status_value == "failed" and recovered_from_timeout_error:
        return batch_id
    if status_value == "failed" and previous_status in {"polling", "downloading"}:
        return batch_id
    return ""


def _mineru_state_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in results:
        state = str(row.get("state") or row.get("status") or "unknown").lower()
        counts[state] = counts.get(state, 0) + 1
    return counts


def _prepare_mineru_only_markdown(
    path: Path,
    *,
    work_dir: Path,
    settings: PreprocessSettings,
    ppx_runner: Callable[..., Path],
    mineru_runner: Callable[..., Path],
) -> Path:
    mineru_error = ""
    ppx_path: Path | None = None
    try:
        mineru_path = mineru_runner([path], output_dir=work_dir / "mineru", settings=settings)
    except Exception as error:
        if not _ppx_fallback_enabled(settings):
            raise
        mineru_path = None
        mineru_error = str(error)
        ppx_path = _run_ppx_audit_foreground(path, work_dir=work_dir, settings=settings, ppx_runner=ppx_runner)
    formal_path, cross_report = _finalize_cross_selection(
        work_dir,
        mineru_path=mineru_path,
        ppx_path=ppx_path,
    )
    acquisition_mode = str(cross_report.get("mode") or ("ppx_fallback" if mineru_path is None else "mineru_only"))
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "preprocess_manifest.json").write_text(
        json.dumps(
            {
                "source": str(path),
                "kind": SourceKind.MINERU_ONLY.value,
                "mineru_markdown": str(mineru_path) if mineru_path is not None else "",
                "ppx_markdown": str(ppx_path) if ppx_path is not None else "",
                "formal_markdown": str(formal_path),
                "cross_report": str(work_dir / "cross" / "cross_report.json"),
                "acquisition_mode": acquisition_mode,
                "mineru_error": mineru_error,
                "mineru_model": settings.mineru_model,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return formal_path


def _prepare_word_markdown(
    path: Path,
    *,
    work_dir: Path,
    settings: PreprocessSettings,
    page_count_reader: PdfPageCounter,
    ppx_runner: Callable[..., Path],
    mineru_runner: Callable[..., Path],
) -> Path:
    # Preferred path: Word → PDF → MinerU page chunks.
    # If conversion/page-count is unavailable, fall back to whole-file MinerU.
    mineru_error = ""
    ppx_path: Path | None = None
    converted_pdf: Path | None = None
    mineru_path: Path | None = None
    try:
        converted_pdf = _convert_word_to_pdf(path, output_dir=work_dir / "converted_pdf")
        page_count = page_count_reader(converted_pdf)
        if page_count > settings.pdf_page_chunk_size:
            mineru_path = run_mineru_vlm_for_pdf_chunks(
                converted_pdf,
                page_count=page_count,
                segment_dir=work_dir / "mineru_segments",
                output_dir=work_dir / "mineru",
                settings=settings,
            )
        else:
            mineru_path = mineru_runner([converted_pdf], output_dir=work_dir / "mineru", settings=settings)
    except Exception as conversion_error:
        converted_pdf = None
        try:
            mineru_path = mineru_runner([path], output_dir=work_dir / "mineru", settings=settings)
            mineru_error = ""
        except Exception as mineru_error_exc:
            if not _ppx_fallback_enabled(settings):
                raise
            mineru_path = None
            mineru_error = f"{conversion_error}; {mineru_error_exc}"
            ppx_path = _run_ppx_audit_foreground(path, work_dir=work_dir, settings=settings, ppx_runner=ppx_runner)
    formal_path, cross_report = _finalize_cross_selection(
        work_dir,
        mineru_path=mineru_path,
        ppx_path=ppx_path,
    )
    acquisition_mode = str(cross_report.get("mode") or ("ppx_fallback" if mineru_path is None else "mineru_only"))
    work_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "source": str(path),
        "kind": SourceKind.WORD.value,
        "mineru_markdown": str(mineru_path) if mineru_path is not None else "",
        "ppx_markdown": str(ppx_path) if ppx_path is not None else "",
        "formal_markdown": str(formal_path),
        "cross_report": str(work_dir / "cross" / "cross_report.json"),
        "acquisition_mode": acquisition_mode,
        "mineru_error": mineru_error,
        "mineru_model": settings.mineru_model,
    }
    if converted_pdf is not None:
        manifest["converted_pdf"] = str(converted_pdf)
    (work_dir / "preprocess_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return formal_path


def _finalize_cross_selection(
    work_dir: Path,
    *,
    mineru_path: Path | None,
    ppx_path: Path | None,
    existing_md_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    initial_path = write_cross_artifacts(
        work_dir,
        mineru_path=mineru_path,
        ppx_path=ppx_path,
        existing_md_path=existing_md_path,
    )
    report_path = work_dir / "cross" / "cross_report.json"
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    return initial_path, payload if isinstance(payload, dict) else {}


def _resolve_ppx_candidate(work_dir: Path, *, preferred: Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if preferred is not None:
        candidates.append(preferred)
    candidates.append(work_dir / "ppx" / "clean" / "ppx.md")
    status_path = work_dir / "ppx" / "ppx_audit_status.json"
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            status = {}
        if isinstance(status, dict):
            status_md = str(status.get("ppx_markdown") or "").strip()
            if status_md:
                candidates.append(Path(status_md))
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return preferred if preferred is not None else None


def _convert_word_to_pdf(path: Path, *, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    expected_path = output_dir / f"{safe_stem(path.stem)}.pdf"
    if expected_path.exists() and expected_path.stat().st_size > 0:
        return expected_path
    command = shutil.which("soffice") or shutil.which("libreoffice")
    if not command:
        raise RuntimeError("LibreOffice/soffice 命令不存在，无法将 Word 文档转为 PDF")
    existing = {candidate.resolve() for candidate in output_dir.glob("*.pdf")}
    completed = subprocess.run(
        [
            command,
            "--headless",
            "--invisible",
            "--nodefault",
            "--norestore",
            "--nolockcheck",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=3600,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "LibreOffice Word 转 PDF 失败")[-2000:])
    if expected_path.exists() and expected_path.stat().st_size > 0:
        return expected_path
    new_pdfs = [
        candidate
        for candidate in output_dir.glob("*.pdf")
        if candidate.resolve() not in existing and candidate.stat().st_size > 0
    ]
    if len(new_pdfs) == 1:
        new_pdfs[0].replace(expected_path)
        return expected_path
    raise RuntimeError(f"LibreOffice 未生成预期 PDF：{expected_path}")


def _source_file(path: Path) -> SourceFile | None:
    suffix = path.suffix.lower()
    if suffix in MARKDOWN_SUFFIXES:
        return SourceFile(path=path, kind=SourceKind.MARKDOWN)
    if suffix in PDF_SUFFIXES:
        return SourceFile(path=path, kind=SourceKind.PDF)
    if suffix in WORD_SUFFIXES:
        return SourceFile(path=path, kind=SourceKind.WORD)
    if suffix in IMAGE_SUFFIXES or suffix in OFFICE_SUFFIXES:
        return SourceFile(path=path, kind=SourceKind.MINERU_ONLY)
    return None


def _run_mineru_file_batches(
    *,
    files: list[Path],
    output_dirs: list[Path],
    indexes: list[int],
    settings: PreprocessSettings,
    client: MinerUClient,
) -> list[Path | None]:
    if not (len(files) == len(output_dirs) == len(indexes)):
        raise ValueError("MinerU file, output, and index counts must match")
    ordered_results: list[Path | None] = [None] * len(files)
    new_items: list[tuple[int, Path, Path, int]] = []
    resumable_groups: dict[str, list[tuple[int, Path, Path, int, int]]] = {}
    for position, (file_path, output_dir, index) in enumerate(zip(files, output_dirs, indexes, strict=True)):
        cached_markdown = output_dir / "full.md"
        status_path = output_dir / "status.json"
        if cached_markdown.exists() and cached_markdown.stat().st_size > 0:
            _write_mineru_segment_status(
                status_path,
                status="cached",
                source=str(file_path),
                index=index,
                markdown=str(cached_markdown),
            )
            ordered_results[position] = cached_markdown
            continue
        batch_id = _resumable_mineru_batch_id(status_path, file_path=file_path)
        if batch_id:
            status = _read_mineru_segment_status(status_path)
            batch_size = _positive_int(status.get("batch_size"), default=1)
            resumable_groups.setdefault(batch_id, []).append((position, file_path, output_dir, index, batch_size))
        else:
            new_items.append((position, file_path, output_dir, index))

    with ThreadPoolExecutor(max_workers=max(1, settings.mineru_concurrency)) as executor:
        futures = {}
        for batch_id, group in resumable_groups.items():
            if len(group) == 1 and group[0][4] <= 1:
                position, file_path, output_dir, index, _ = group[0]
                futures[
                    executor.submit(_run_one_mineru_file, file_path, output_dir=output_dir, settings=settings, client=client, index=index)
                ] = position
                continue
            batch = [(position, file_path, output_dir, index) for position, file_path, output_dir, index, _ in group]
            futures[
                executor.submit(
                    _resume_mineru_file_batch,
                    batch_id=batch_id,
                    batch=batch,
                    settings=settings,
                    client=client,
                )
            ] = -1
        batch_size = max(1, settings.mineru_batch_size)
        for offset in range(0, len(new_items), batch_size):
            batch = new_items[offset : offset + batch_size]
            futures[
                executor.submit(
                    _run_new_mineru_file_batch,
                    batch=batch,
                    settings=settings,
                    client=client,
                )
            ] = -1
        for future in as_completed(futures):
            position = futures[future]
            result = future.result()
            if position >= 0:
                ordered_results[position] = result
                continue
            for item_position, markdown_path in result.items():
                ordered_results[item_position] = markdown_path
    return ordered_results


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _resume_mineru_file_batch(
    *,
    batch_id: str,
    batch: list[tuple[int, Path, Path, int]],
    settings: PreprocessSettings,
    client: MinerUClient,
) -> dict[int, Path]:
    mineru_files: list[MinerUFile] = []
    for _, file_path, output_dir, index in batch:
        status = _read_mineru_segment_status(output_dir / "status.json")
        mineru_files.append(
            MinerUFile(
                path=file_path,
                name=str(status.get("file_name") or file_path.name),
                data_id=str(status.get("data_id") or f"llmcheck_{safe_stem(file_path.stem)}_{index:03d}"),
            )
        )
    for (_, file_path, output_dir, index), mineru_file in zip(batch, mineru_files, strict=True):
        _write_mineru_segment_status(
            output_dir / "status.json",
            status="polling",
            source=str(file_path),
            index=index,
            file_name=mineru_file.name,
            data_id=mineru_file.data_id,
            batch_id=batch_id,
            model=settings.mineru_model,
            resumed=True,
            batch_size=len(batch),
        )

    def record_poll(results: list[dict[str, Any]]) -> None:
        for (_, file_path, output_dir, index), mineru_file in zip(batch, mineru_files, strict=True):
            result = _mineru_result_for_file(results, file=mineru_file)
            state = str((result or {}).get("state") or (result or {}).get("status") or "unknown")
            _write_mineru_segment_status(
                output_dir / "status.json",
                status="polling",
                source=str(file_path),
                index=index,
                file_name=mineru_file.name,
                data_id=mineru_file.data_id,
                batch_id=batch_id,
                model=settings.mineru_model,
                result_count=len(results),
                state_counts={state: 1},
                resumed=True,
                batch_size=len(batch),
            )

    try:
        results = client.poll_batch_results(
            batch_id=batch_id,
            poll_interval_seconds=settings.mineru_poll_interval_seconds,
            timeout_seconds=settings.mineru_timeout_seconds,
            on_poll=record_poll,
        )
        markdowns: dict[int, Path] = {}
        for (position, file_path, output_dir, index), mineru_file in zip(batch, mineru_files, strict=True):
            result = _mineru_result_for_file(results, file=mineru_file)
            if result is None and len(batch) == 1 and len(results) == 1:
                result = results[0]
            if result is None:
                raise RuntimeError(f"MinerU batch {batch_id} 缺少文件结果：{mineru_file.name}")
            zip_url = str(result.get("full_zip_url") or "")
            if not zip_url:
                raise RuntimeError(f"MinerU 结果缺少 full_zip_url：{result}")
            zip_path = output_dir / "result.zip"
            _write_mineru_segment_status(
                output_dir / "status.json",
                status="downloading",
                source=str(file_path),
                index=index,
                file_name=mineru_file.name,
                data_id=mineru_file.data_id,
                batch_id=batch_id,
                model=settings.mineru_model,
                result_state=str(result.get("state") or ""),
                resumed=True,
                batch_size=len(batch),
            )
            client.download_result_zip(full_zip_url=zip_url, output_path=zip_path)
            markdown_path = extract_full_markdown_from_zip(zip_path, output_dir=output_dir)
            _write_mineru_segment_status(
                output_dir / "status.json",
                status="done",
                source=str(file_path),
                index=index,
                file_name=mineru_file.name,
                data_id=mineru_file.data_id,
                batch_id=batch_id,
                model=settings.mineru_model,
                markdown=str(markdown_path),
                result_zip=str(zip_path),
                resumed=True,
                batch_size=len(batch),
            )
            markdowns[position] = markdown_path
        return markdowns
    except Exception as error:
        for (_, file_path, output_dir, index), mineru_file in zip(batch, mineru_files, strict=True):
            status_path = output_dir / "status.json"
            status = _read_mineru_segment_status(status_path)
            if status.get("status") == "done":
                continue
            _write_mineru_segment_status(
                status_path,
                status="failed",
                source=str(file_path),
                index=index,
                file_name=mineru_file.name,
                data_id=mineru_file.data_id,
                batch_id=batch_id,
                model=settings.mineru_model,
                previous_status="polling",
                error=str(error),
                resumed=True,
                batch_size=len(batch),
            )
        raise


def _run_new_mineru_file_batch(
    *,
    batch: list[tuple[int, Path, Path, int]],
    settings: PreprocessSettings,
    client: MinerUClient,
) -> dict[int, Path]:
    mineru_files = [
        MinerUFile(path=file_path, name=file_path.name, data_id=f"llmcheck_{safe_stem(file_path.stem)}_{index:03d}")
        for _, file_path, _, index in batch
    ]
    batch_id = ""
    current_status = "creating_task"
    for (_, file_path, output_dir, index), mineru_file in zip(batch, mineru_files, strict=True):
        _write_mineru_segment_status(
            output_dir / "status.json",
            status="creating_task",
            source=str(file_path),
            index=index,
            file_name=file_path.name,
            data_id=mineru_file.data_id,
            model=settings.mineru_model,
            batch_size=len(batch),
        )
    try:
        batch_id, upload_urls = client.create_upload_batch(files=mineru_files, model_version=settings.mineru_model)
        current_status = "uploading"
        for (_, file_path, output_dir, index), mineru_file in zip(batch, mineru_files, strict=True):
            _write_mineru_segment_status(
                output_dir / "status.json",
                status="uploading",
                source=str(file_path),
                index=index,
                file_name=file_path.name,
                data_id=mineru_file.data_id,
                batch_id=batch_id,
                model=settings.mineru_model,
                batch_size=len(batch),
            )
        client.upload_files(files=mineru_files, upload_urls=upload_urls)
        current_status = "polling"
        for (_, file_path, output_dir, index), mineru_file in zip(batch, mineru_files, strict=True):
            _write_mineru_segment_status(
                output_dir / "status.json",
                status="polling",
                source=str(file_path),
                index=index,
                file_name=file_path.name,
                data_id=mineru_file.data_id,
                batch_id=batch_id,
                model=settings.mineru_model,
                batch_size=len(batch),
            )

        def record_poll(results: list[dict[str, Any]]) -> None:
            for (_, file_path, output_dir, index), mineru_file in zip(batch, mineru_files, strict=True):
                result = _mineru_result_for_file(results, file=mineru_file)
                state = str((result or {}).get("state") or (result or {}).get("status") or "unknown")
                _write_mineru_segment_status(
                    output_dir / "status.json",
                    status="polling",
                    source=str(file_path),
                    index=index,
                    file_name=file_path.name,
                    data_id=mineru_file.data_id,
                    batch_id=batch_id,
                    model=settings.mineru_model,
                    result_count=len(results),
                    state_counts={state: 1},
                    batch_size=len(batch),
                )

        results = client.poll_batch_results(
            batch_id=batch_id,
            poll_interval_seconds=settings.mineru_poll_interval_seconds,
            timeout_seconds=settings.mineru_timeout_seconds,
            on_poll=record_poll,
        )
        markdowns: dict[int, Path] = {}
        for (position, file_path, output_dir, index), mineru_file in zip(batch, mineru_files, strict=True):
            result = _mineru_result_for_file(results, file=mineru_file) or {}
            zip_url = str(result.get("full_zip_url") or "")
            if not zip_url:
                raise RuntimeError(f"MinerU 结果缺少 full_zip_url：{result}")
            zip_path = output_dir / "result.zip"
            current_status = "downloading"
            _write_mineru_segment_status(
                output_dir / "status.json",
                status="downloading",
                source=str(file_path),
                index=index,
                file_name=file_path.name,
                data_id=mineru_file.data_id,
                batch_id=batch_id,
                model=settings.mineru_model,
                result_state=str(result.get("state") or ""),
                batch_size=len(batch),
            )
            client.download_result_zip(full_zip_url=zip_url, output_path=zip_path)
            markdown_path = extract_full_markdown_from_zip(zip_path, output_dir=output_dir)
            _write_mineru_segment_status(
                output_dir / "status.json",
                status="done",
                source=str(file_path),
                index=index,
                file_name=file_path.name,
                data_id=mineru_file.data_id,
                batch_id=batch_id,
                model=settings.mineru_model,
                markdown=str(markdown_path),
                result_zip=str(zip_path),
                batch_size=len(batch),
            )
            markdowns[position] = markdown_path
        return markdowns
    except Exception as error:
        for (_, file_path, output_dir, index), mineru_file in zip(batch, mineru_files, strict=True):
            status_path = output_dir / "status.json"
            status = _read_mineru_segment_status(status_path)
            if status.get("status") == "done":
                continue
            _write_mineru_segment_status(
                status_path,
                status="failed",
                source=str(file_path),
                index=index,
                file_name=file_path.name,
                data_id=mineru_file.data_id,
                batch_id=batch_id,
                model=settings.mineru_model,
                previous_status=current_status,
                error=str(error),
                batch_size=len(batch),
            )
        raise


def _mineru_result_for_file(results: list[dict[str, Any]], *, file: MinerUFile) -> dict[str, Any] | None:
    for result in results:
        if str(result.get("data_id") or "") == file.data_id:
            return result
    for result in results:
        if str(result.get("file_name") or result.get("name") or "") == file.name:
            return result
    return None


def _run_one_mineru_file(
    file_path: Path,
    *,
    output_dir: Path,
    settings: PreprocessSettings,
    client: MinerUClient,
    index: int,
) -> Path:
    cached_markdown = output_dir / "full.md"
    status_path = output_dir / "status.json"
    if cached_markdown.exists() and cached_markdown.stat().st_size > 0:
        _write_mineru_segment_status(
            status_path,
            status="cached",
            source=str(file_path),
            index=index,
            markdown=str(cached_markdown),
        )
        return cached_markdown
    mineru_file = MinerUFile(path=file_path, name=file_path.name, data_id=f"llmcheck_{safe_stem(file_path.stem)}_{index:03d}")
    batch_id = _resumable_mineru_batch_id(status_path, file_path=file_path)
    current_status = "polling" if batch_id else "creating_task"
    if batch_id:
        _write_mineru_segment_status(
            status_path,
            status="polling",
            source=str(file_path),
            index=index,
            file_name=file_path.name,
            data_id=mineru_file.data_id,
            batch_id=batch_id,
            model=settings.mineru_model,
            resumed=True,
        )
    else:
        _write_mineru_segment_status(
            status_path,
            status="creating_task",
            source=str(file_path),
            index=index,
            file_name=file_path.name,
            data_id=mineru_file.data_id,
            model=settings.mineru_model,
        )
    try:
        if not batch_id:
            batch_id, upload_urls = client.create_upload_batch(files=[mineru_file], model_version=settings.mineru_model)
            current_status = "uploading"
            _write_mineru_segment_status(
                status_path,
                status="uploading",
                source=str(file_path),
                index=index,
                file_name=file_path.name,
                data_id=mineru_file.data_id,
                batch_id=batch_id,
                model=settings.mineru_model,
            )
            client.upload_files(files=[mineru_file], upload_urls=upload_urls)
        current_status = "polling"
        _write_mineru_segment_status(
            status_path,
            status="polling",
            source=str(file_path),
            index=index,
            file_name=file_path.name,
            data_id=mineru_file.data_id,
            batch_id=batch_id,
            model=settings.mineru_model,
        )

        def record_poll(results: list[dict[str, Any]]) -> None:
            _write_mineru_segment_status(
                status_path,
                status="polling",
                source=str(file_path),
                index=index,
                file_name=file_path.name,
                data_id=mineru_file.data_id,
                batch_id=batch_id,
                model=settings.mineru_model,
                result_count=len(results),
                state_counts=_mineru_state_counts(results),
            )

        results = client.poll_batch_results(
            batch_id=batch_id,
            poll_interval_seconds=settings.mineru_poll_interval_seconds,
            timeout_seconds=settings.mineru_timeout_seconds,
            on_poll=record_poll,
        )
        result = results[0] if results else {}
        zip_url = str(result.get("full_zip_url") or "")
        if not zip_url:
            raise RuntimeError(f"MinerU 结果缺少 full_zip_url：{result}")
        zip_path = output_dir / "result.zip"
        current_status = "downloading"
        _write_mineru_segment_status(
            status_path,
            status="downloading",
            source=str(file_path),
            index=index,
            file_name=file_path.name,
            data_id=mineru_file.data_id,
            batch_id=batch_id,
            model=settings.mineru_model,
            result_state=str(result.get("state") or ""),
        )
        client.download_result_zip(full_zip_url=zip_url, output_path=zip_path)
        markdown_path = extract_full_markdown_from_zip(zip_path, output_dir=output_dir)
        current_status = "done"
        _write_mineru_segment_status(
            status_path,
            status="done",
            source=str(file_path),
            index=index,
            file_name=file_path.name,
            data_id=mineru_file.data_id,
            batch_id=batch_id,
            model=settings.mineru_model,
            markdown=str(markdown_path),
            result_zip=str(zip_path),
        )
        return markdown_path
    except Exception as error:
        _write_mineru_segment_status(
            status_path,
            status="failed",
            source=str(file_path),
            index=index,
            file_name=file_path.name,
            data_id=mineru_file.data_id,
            batch_id=batch_id,
            model=settings.mineru_model,
            previous_status=current_status,
            error=str(error),
        )
        raise


def _run_one_mineru_pdf_segment(
    *,
    source_path: Path,
    start: int,
    end: int,
    segment_path: Path,
    output_dir: Path,
    settings: PreprocessSettings,
    client: MinerUClient,
    index: int,
) -> Path:
    _ensure_pdf_segment(source_path=source_path, start=start, end=end, segment_path=segment_path)
    return _run_one_mineru_file(segment_path, output_dir=output_dir, settings=settings, client=client, index=index)


def _ensure_pdf_segment(*, source_path: Path, start: int, end: int, segment_path: Path) -> Path:
    if not segment_path.exists():
        _write_pdf_segment(source_path=source_path, start=start, end=end, segment_path=segment_path)
    return segment_path


def extract_full_markdown_from_zip(zip_path: Path, *, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        full_md_name = next((name for name in archive.namelist() if name.endswith("full.md")), "")
        if not full_md_name:
            raise ValueError(f"{zip_path} 中没有 full.md")
        markdown = archive.read(full_md_name).decode("utf-8")
    output_path = output_dir / "full.md"
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def _largest_markdown(root: Path) -> Path:
    paths = sorted(root.rglob("*.md"), key=lambda path: path.stat().st_size, reverse=True)
    if not paths:
        raise RuntimeError(f"PPX 未生成 Markdown：{root}")
    return paths[0]


def _successful_data(response: dict[str, Any]) -> dict[str, Any]:
    if response.get("code") != 0:
        raise RuntimeError(f"MinerU API 返回失败：{response}")
    data = response.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"MinerU API 响应缺少 data：{response}")
    return data


def _extract_result_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    result = data.get("extract_result")
    if isinstance(result, dict):
        return [result]
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    results = data.get("extract_results")
    if isinstance(results, list):
        return [row for row in results if isinstance(row, dict)]
    if isinstance(data.get("results"), list):
        return [row for row in data["results"] if isinstance(row, dict)]
    return []


def _put_file_without_content_type(upload_url: str, data: bytes, *, timeout_seconds: int) -> tuple[int, str, str]:
    parsed = urllib.parse.urlsplit(upload_url)
    if parsed.scheme != "https":
        raise ValueError("MinerU 上传 URL 必须是 https")
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    connection = http.client.HTTPSConnection(parsed.netloc, timeout=timeout_seconds)
    try:
        connection.request("PUT", path, body=data, headers={})
        response = connection.getresponse()
        body = response.read().decode("utf-8", errors="replace")
        return response.status, response.reason, body
    finally:
        connection.close()
