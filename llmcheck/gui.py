from __future__ import annotations

from pathlib import Path
from typing import Any
import base64
import copy
import json
import tempfile
import threading
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from llmcheck.batch import run_batch
from llmcheck.diagnostics import read_mineru_segment_status, summarize_book_output
from llmcheck.pipeline import DEFAULT_LLM_CHUNK_CHARS, LlmCheckSettings, process_documents
from llmcheck.preprocess import (
    DEFAULT_MINERU_BATCH_SIZE,
    DEFAULT_MINERU_MAX_RETRIES,
    DEFAULT_MINERU_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_MINERU_RETRY_BACKOFF_SECONDS,
    DEFAULT_PDF_PAGE_CHUNK_SIZE,
    SUPPORTED_SUFFIXES,
)


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def start(
        self,
        *,
        input_path: Path,
        output_dir: Path,
        settings: LlmCheckSettings,
        book_concurrency: int,
        start_index: int,
        limit: int,
        force: bool,
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "status": "running",
            "progress_percent": 5,
            "steps": [{"label": "queued", "status": "running"}],
            "result": None,
            "error": "",
            "current_book": "",
            "current_index": 0,
            "current_output_dir": "",
            "mineru_segments": {},
            "total": 0,
            "completed": 0,
            "passed": 0,
            "skipped": 0,
            "failed": 0,
        }
        with self._lock:
            self._jobs[job_id] = job
        thread = threading.Thread(
            target=self._run,
            kwargs={
                "job": job,
                "input_path": input_path,
                "output_dir": output_dir,
                "settings": settings,
                "book_concurrency": book_concurrency,
                "start_index": start_index,
                "limit": limit,
                "force": force,
            },
            daemon=True,
        )
        thread.start()
        return copy.deepcopy(job)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return copy.deepcopy(job) if job is not None else None

    def _run(
        self,
        *,
        job: dict[str, Any],
        input_path: Path,
        output_dir: Path,
        settings: LlmCheckSettings,
        book_concurrency: int,
        start_index: int,
        limit: int,
        force: bool,
    ) -> None:
        try:
            self._update_job(job, progress_percent=20)
            if input_path.is_dir():
                self._append_step(job, {"label": "batch", "status": "running", "message": "逐本处理目录输入"})
                report = run_batch(
                    source_dir=input_path,
                    output_dir=output_dir,
                    settings=settings,
                    book_concurrency=book_concurrency,
                    start_index=start_index,
                    limit=limit,
                    force=force,
                    progress_callback=lambda event: self._record_batch_progress(job, event),
                )
            else:
                self._append_step(job, {"label": "llmcheck", "status": "running", "message": "清洗文本、LLM 纠错并验收"})
                self._update_job(job, current_book=input_path.name, current_index=1, current_output_dir=str(output_dir), total=1)
                report = process_documents(input_path=input_path, output_dir=output_dir, settings=settings)
            status = "passed" if report["status"] == "passed" else "review_required"
            self._update_job(job, result=report, status=status, progress_percent=100)
            self._append_step(job, {"label": "done", "status": status})
        except Exception as error:  # noqa: BLE001 - GUI job should surface the error.
            self._update_job(job, status="failed", error=str(error), progress_percent=100)
            self._append_step(job, {"label": "failed", "status": "failed", "message": str(error)})

    def _record_batch_progress(self, job: dict[str, Any], event: dict[str, Any]) -> None:
        with self._lock:
            if event.get("event") == "batch_started":
                job["total"] = int(event.get("selected_total") or 0)
                job["progress_percent"] = 20
                return
            if event.get("event") == "book_started":
                job["current_book"] = str(event.get("book_name") or Path(str(event.get("source_path") or "")).name)
                job["current_index"] = int(event.get("index") or 0)
                job["total"] = int(event.get("total") or job.get("total") or 0)
                job["current_output_dir"] = str(event.get("output_dir") or "")
                return
            if event.get("event") == "book_finished":
                status = str(event.get("status") or "")
                job["completed"] = int(job.get("completed") or 0) + 1
                if status == "passed":
                    job["passed"] = int(job.get("passed") or 0) + 1
                elif status == "skipped":
                    job["skipped"] = int(job.get("skipped") or 0) + 1
                elif status == "failed":
                    job["failed"] = int(job.get("failed") or 0) + 1
                total = max(1, int(job.get("total") or 1))
                job["progress_percent"] = min(95, 20 + int(75 * int(job["completed"]) / total))

    def _update_job(self, job: dict[str, Any], **fields: Any) -> None:
        with self._lock:
            job.update(fields)

    def _append_step(self, job: dict[str, Any], step: dict[str, Any]) -> None:
        with self._lock:
            job["steps"].append(step)


def create_app() -> FastAPI:
    app = FastAPI(title="LLMcheck")
    jobs = JobStore()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return render_index_html()

    @app.post("/api/jobs")
    async def create_job(request: Request) -> JSONResponse:
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"status": "failed", "error": "请求体必须是 JSON 对象"}, status_code=400)
        output_dir_value = str(payload.get("output_dir") or "").strip()
        if not output_dir_value:
            return JSONResponse({"status": "failed", "error": "缺少输出目录"}, status_code=400)
        output_dir = Path(output_dir_value).expanduser()
        input_path = _save_uploaded_files(payload, output_dir=output_dir) or Path(str(payload.get("input_path") or "")).expanduser()
        settings = LlmCheckSettings(
            llm_api_url=str(payload.get("llm_api_url") or ""),
            llm_api_key=str(payload.get("llm_api_key") or ""),
            llm_model=str(payload.get("llm_model") or ""),
            concurrency=max(1, int(payload.get("concurrency") or 4)),
            llm_chunk_chars=max(1000, int(payload.get("llm_chunk_chars") or DEFAULT_LLM_CHUNK_CHARS)),
            timeout_seconds=max(10, int(payload.get("timeout_seconds") or 600)),
            mineru_api_url=str(payload.get("mineru_api_url") or "https://mineru.net"),
            mineru_api_key=str(payload.get("mineru_api_key") or ""),
            mineru_model="vlm",
            mineru_concurrency=max(1, int(payload.get("mineru_concurrency") or 12)),
            mineru_batch_size=max(1, int(payload.get("mineru_batch_size") or DEFAULT_MINERU_BATCH_SIZE)),
            mineru_timeout_seconds=max(10, int(payload.get("mineru_timeout_seconds") or 3600)),
            mineru_request_timeout_seconds=max(10, int(payload.get("mineru_request_timeout_seconds") or DEFAULT_MINERU_REQUEST_TIMEOUT_SECONDS)),
            mineru_max_retries=max(1, int(payload.get("mineru_max_retries") or DEFAULT_MINERU_MAX_RETRIES)),
            mineru_retry_backoff_seconds=max(0.0, float(payload.get("mineru_retry_backoff_seconds") or DEFAULT_MINERU_RETRY_BACKOFF_SECONDS)),
            pdf_page_chunk_size=max(1, int(payload.get("pdf_page_chunk_size") or DEFAULT_PDF_PAGE_CHUNK_SIZE)),
            ppx_command=str(payload.get("ppx_command") or "/mnt/d/codex/memect-ppx/ppx"),
            ppx_cwd=str(payload.get("ppx_cwd") or "/mnt/d/codex/memect-ppx"),
            ppx_timeout_seconds=max(10, int(payload.get("ppx_timeout_seconds") or 3600)),
            ppx_backend=str(payload.get("ppx_backend") or "default"),
            ppx_ocr=str(payload.get("ppx_ocr") or "auto"),
            ppx_formula=str(payload.get("ppx_formula") or "no"),
        )
        if not settings.llm_api_url or not settings.llm_api_key or not settings.llm_model:
            return JSONResponse({"status": "failed", "error": "缺少 LLM API url/key/model"}, status_code=400)
        job = jobs.start(
            input_path=input_path,
            output_dir=output_dir,
            settings=settings,
            book_concurrency=max(1, int(payload.get("book_concurrency") or 1)),
            start_index=max(1, int(payload.get("start_index") or 1)),
            limit=max(0, int(payload.get("limit") or 0)),
            force=bool(payload.get("force") or False),
        )
        return JSONResponse(job)

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> JSONResponse:
        job = jobs.get(job_id)
        if job is None:
            return JSONResponse({"status": "failed", "error": "job not found"}, status_code=404)
        return JSONResponse(_with_live_progress(job))

    return app


def _save_uploaded_files(payload: dict[str, Any], *, output_dir: Path) -> Path | None:
    files = payload.get("uploaded_files")
    if not isinstance(files, list) or not files:
        return None
    upload_dir = Path(tempfile.mkdtemp(prefix="llmcheck_uploads_"))
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for item in files:
        if not isinstance(item, dict):
            continue
        relative_path = _safe_upload_path(str(item.get("name") or "document.md"))
        if relative_path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        data = base64.b64decode(str(item.get("data_base64") or ""))
        target = upload_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        saved += 1
    if not saved:
        return None
    if saved == 1:
        return next(path for path in upload_dir.rglob("*") if path.is_file())
    return upload_dir


def _safe_upload_path(name: str) -> Path:
    parts = [part for part in Path(name.replace("\\", "/")).parts if part not in {"", ".", "..", "/"}]
    return Path(*parts) if parts else Path("document.md")


def _with_live_progress(job: dict[str, Any]) -> dict[str, Any]:
    output_dir = str(job.get("current_output_dir") or "")
    book_name = str(job.get("current_book") or "")
    if output_dir and book_name:
        diagnostics = summarize_book_output(Path(output_dir), book_name=book_name)
        job["diagnostics"] = diagnostics
        mineru_segments = _read_mineru_segment_status(Path(output_dir), book_name=book_name)
        if mineru_segments["total"]:
            job["mineru_segments"] = mineru_segments
    return job


def _read_mineru_segment_status(book_output_dir: Path, *, book_name: str) -> dict[str, Any]:
    return read_mineru_segment_status(book_output_dir, book_name=book_name)


def render_index_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>LLMcheck</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 0; background: #f6f7f9; color: #111827; }
    main { max-width: 920px; margin: 32px auto; padding: 0 20px; }
    section { background: white; border: 1px solid #d9dee7; border-radius: 8px; padding: 20px; margin-bottom: 16px; }
    label { display: block; font-size: 13px; font-weight: 600; margin: 12px 0 6px; }
    input { width: 100%; box-sizing: border-box; padding: 10px; border: 1px solid #c8cfda; border-radius: 6px; }
    button { margin-top: 16px; padding: 10px 14px; border: 0; border-radius: 6px; background: #155eef; color: white; font-weight: 700; cursor: pointer; }
    progress { width: 100%; height: 18px; }
    pre { white-space: pre-wrap; background: #111827; color: #f9fafb; padding: 12px; border-radius: 6px; min-height: 120px; }
  </style>
</head>
<body>
<main>
  <section>
    <h1>LLMcheck</h1>
    <label for="uploaded_files">导入 Markdown/PDF/图片/Office 文件</label>
    <input id="uploaded_files" type="file" accept=".md,.pdf,.png,.jpg,.jpeg,.jp2,.webp,.gif,.bmp,.doc,.docx,.ppt,.pptx,.xls,.xlsx" multiple />
    <label for="uploaded_directory">导入 Markdown/PDF/图片/Office 文件夹</label>
    <input id="uploaded_directory" type="file" accept=".md,.pdf,.png,.jpg,.jpeg,.jp2,.webp,.gif,.bmp,.doc,.docx,.ppt,.pptx,.xls,.xlsx" multiple webkitdirectory />
    <label for="input_path">或输入本机文件/目录路径</label>
    <input id="input_path" placeholder="/path/to/file-or-directory" />
    <label for="output_dir">输出目录</label>
    <input id="output_dir" placeholder="/path/to/output" />
  </section>
  <section>
    <label for="llm_api_url">LLM API URL</label>
    <input id="llm_api_url" value="http://127.0.0.1:3022" />
    <label for="llm_api_key">LLM API Key</label>
    <input id="llm_api_key" type="text" value="123" />
    <label for="llm_model">Model</label>
    <input id="llm_model" value="gpt-5.5" />
    <label for="concurrency">并发上限</label>
    <input id="concurrency" type="number" min="1" value="4" />
    <label for="book_concurrency">逐本并发上限</label>
    <input id="book_concurrency" type="number" min="1" value="1" />
    <label for="start_index">起始书号</label>
    <input id="start_index" type="number" min="1" value="1" />
    <label for="limit">最多处理本数</label>
    <input id="limit" type="number" min="0" value="0" />
    <label for="force">重新处理已通过书目</label>
    <input id="force" type="checkbox" />
    <label for="llm_chunk_chars">LLM 片段字数上限</label>
    <input id="llm_chunk_chars" type="number" min="1000" value="2000" />
    <label for="mineru_api_url">MinerU API URL</label>
    <input id="mineru_api_url" value="https://mineru.net" />
    <label for="mineru_api_key">MinerU API Key</label>
    <input id="mineru_api_key" type="text" />
    <label for="mineru_concurrency">MinerU 并发上限</label>
    <input id="mineru_concurrency" type="number" min="1" value="12" />
    <label for="mineru_batch_size">MinerU 批量文件数</label>
    <input id="mineru_batch_size" type="number" min="1" value="50" />
    <label for="mineru_timeout_seconds">MinerU 单段总等待秒数</label>
    <input id="mineru_timeout_seconds" type="number" min="10" value="3600" />
    <label for="mineru_request_timeout_seconds">MinerU 单次网络超时秒数</label>
    <input id="mineru_request_timeout_seconds" type="number" min="10" value="60" />
    <label for="mineru_max_retries">MinerU 网络重试次数</label>
    <input id="mineru_max_retries" type="number" min="1" value="3" />
    <label for="mineru_retry_backoff_seconds">MinerU 重试退避秒数</label>
    <input id="mineru_retry_backoff_seconds" type="number" min="0" step="0.5" value="2" />
    <label for="pdf_page_chunk_size">PDF 拆分页数上限</label>
    <input id="pdf_page_chunk_size" type="number" min="1" value="30" />
    <label for="ppx_command">PPX 命令</label>
    <input id="ppx_command" value="/mnt/d/codex/memect-ppx/ppx" />
    <label for="ppx_cwd">PPX 工作目录</label>
    <input id="ppx_cwd" value="/mnt/d/codex/memect-ppx" />
    <label for="ppx_timeout_seconds">PPX 超时秒数</label>
    <input id="ppx_timeout_seconds" type="number" min="10" value="3600" />
    <label for="ppx_backend">本地 OCR 组合</label>
    <select id="ppx_backend">
      <option value="default">单 PPX / 默认后端</option>
    </select>
    <label for="ppx_ocr">PPX OCR 策略</label>
    <select id="ppx_ocr">
      <option value="auto">自动</option>
      <option value="yes">强制 OCR</option>
      <option value="no">不启用 OCR</option>
    </select>
    <label for="ppx_formula">PPX 公式识别</label>
    <select id="ppx_formula">
      <option value="no">关闭</option>
      <option value="auto">自动</option>
      <option value="yes">开启</option>
    </select>
    <button id="start">开始检查</button>
  </section>
  <section>
    <progress id="progress" max="100" value="0"></progress>
    <pre id="log"></pre>
  </section>
</main>
<script>
async function filesToPayload(files) {
  const supportedSuffixes = ['.md', '.pdf', '.png', '.jpg', '.jpeg', '.jp2', '.webp', '.gif', '.bmp', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx'];
  const rows = [];
  for (const file of files) {
    const lowerName = file.name.toLowerCase();
    if (!supportedSuffixes.some((suffix) => lowerName.endsWith(suffix))) continue;
    const dataUrl = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
    rows.push({name: file.webkitRelativePath || file.name, data_base64: String(dataUrl).split(',')[1] || ''});
  }
  return rows;
}
document.getElementById('start').onclick = async () => {
  const payload = {
    input_path: document.getElementById('input_path').value,
    output_dir: document.getElementById('output_dir').value,
    llm_api_url: document.getElementById('llm_api_url').value,
    llm_api_key: document.getElementById('llm_api_key').value,
    llm_model: document.getElementById('llm_model').value,
    concurrency: document.getElementById('concurrency').value,
    llm_chunk_chars: document.getElementById('llm_chunk_chars').value,
    mineru_api_url: document.getElementById('mineru_api_url').value,
    mineru_api_key: document.getElementById('mineru_api_key').value,
    mineru_concurrency: document.getElementById('mineru_concurrency').value,
    mineru_batch_size: document.getElementById('mineru_batch_size').value,
    mineru_timeout_seconds: document.getElementById('mineru_timeout_seconds').value,
    mineru_request_timeout_seconds: document.getElementById('mineru_request_timeout_seconds').value,
    mineru_max_retries: document.getElementById('mineru_max_retries').value,
    mineru_retry_backoff_seconds: document.getElementById('mineru_retry_backoff_seconds').value,
    pdf_page_chunk_size: document.getElementById('pdf_page_chunk_size').value,
    ppx_command: document.getElementById('ppx_command').value,
    ppx_cwd: document.getElementById('ppx_cwd').value,
    ppx_timeout_seconds: document.getElementById('ppx_timeout_seconds').value,
    ppx_backend: document.getElementById('ppx_backend').value,
    ppx_ocr: document.getElementById('ppx_ocr').value,
    ppx_formula: document.getElementById('ppx_formula').value,
    book_concurrency: document.getElementById('book_concurrency').value,
    start_index: document.getElementById('start_index').value,
    limit: document.getElementById('limit').value,
    force: document.getElementById('force').checked,
    uploaded_files: [
      ...await filesToPayload(document.getElementById('uploaded_files').files),
      ...await filesToPayload(document.getElementById('uploaded_directory').files)
    ]
  };
  const res = await fetch('/api/jobs', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
  const job = await res.json();
  if (!res.ok) { document.getElementById('log').textContent = JSON.stringify(job, null, 2); return; }
  const timer = setInterval(async () => {
    const current = await (await fetch('/api/jobs/' + job.job_id)).json();
    document.getElementById('progress').value = current.progress_percent || 0;
    document.getElementById('log').textContent = JSON.stringify(current, null, 2);
    if (['passed', 'review_required', 'failed'].includes(current.status)) clearInterval(timer);
  }, 1000);
};
</script>
</body>
</html>"""
