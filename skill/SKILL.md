---
name: llmcheck
description: Use when an agent needs to run or explain LLMcheck: a CLI/GUI workflow that converts Markdown, PDF, image, and Office files into cleaned Markdown, performs conservative full-book LLM correction and LLM acceptance, emits final Markdown/text PDF delivery folders, and keeps process artifacts under process/.
---

# LLMcheck

Use this skill when the user wants to process source documents with LLMcheck, inspect LLMcheck outputs, or prepare a repeatable command for full-book text cleanup and acceptance.

## What It Does

LLMcheck normalizes supported source files to Markdown, then runs deterministic text cleanup, conservative chunked/concurrent LLM correction, chunked/concurrent LLM acceptance, targeted local repair for known failed acceptance items, LLM repair for failed acceptance chunks when needed, and final artifact generation. Passed documents emit final Markdown and text PDF delivery files.

The correction and acceptance prompts are tuned for Chinese medicine books. They require conservative fixes: repair obvious OCR, punctuation, segmentation, abnormal paragraphing, text adhesion, and forced line breaks without rewriting medical substance, prescriptions, doses, diagnoses, case records, or uncertain content.

## Supported Inputs

Pass either a single file or a directory to `--input`. Directories are scanned recursively for supported files.

- Markdown: `.md`
- PDF: `.pdf`
- Images: `.png`, `.jpg`, `.jpeg`, `.jp2`, `.webp`, `.gif`, `.bmp`
- Office: `.doc`, `.docx`, `.ppt`, `.pptx`, `.xls`, `.xlsx`

Input flow:

- Markdown enters LLMcheck cleanup, correction, and acceptance directly.
- PDF starts local PPX audit and MinerU API VLM in parallel. MinerU is the formal text source for cleanup, correction, acceptance, and final output, so LLM correction can start as soon as MinerU is merged. PPX Markdown is retained only for audit/reference and is used by repair when available.
- MinerU splits PDFs into page chunks by `--pdf-page-chunk-size`, default `30`, submits chunks concurrently, and merges MinerU chunk Markdown back in page order.
- Images and Office files go directly to MinerU API VLM without PPX.

LLMcheck does not run Stage1 closure, difference-page rule convergence, or MinerU/PPX voting.

Built-in deterministic repair rules:

- If MinerU emits a table image plus adjacent `flowchart` details, convert the flowchart edges into a Markdown table before stripping generated details.
- Split common local structure glue before LLM correction, including body-region labels such as `肩部：`, `肘部：`, `腕部：`, `腰部：`, `髀部：`, `膝部：`, `踝部：`, and bibliography glue after `年版`.
- When acceptance returns blocking `layout`, `ocr_noise`, `punctuation`, or `missing_text` issues, run a narrow local repair pass on the failed excerpt and re-run acceptance before escalating to chunked LLM repair.

## CLI

Run one file or one directory as a single LLMcheck job:

```bash
llmcheck run \
  --input /path/to/file-or-directory \
  --output-dir /path/to/output \
  --llm-api-url http://127.0.0.1:3022 \
  --llm-api-key "$LLM_API_KEY" \
  --llm-model gpt-5.5 \
  --mineru-api-key "$MINERU_CLOUD_API_TOKEN" \
  --mineru-concurrency 12 \
  --pdf-page-chunk-size 30 \
  --llm-chunk-chars 2000 \
  --concurrency 10
```

Run a source directory one book at a time:

```bash
llmcheck batch \
  --source-dir /mnt/d/pdf \
  --output-dir /mnt/d/pdf/output \
  --start-index 1 \
  --limit 1 \
  --book-concurrency 1 \
  --llm-api-url http://127.0.0.1:3022 \
  --llm-api-key "$LLM_API_KEY" \
  --llm-model gpt-5.5 \
  --mineru-api-key "$MINERU_CLOUD_API_TOKEN" \
  --mineru-concurrency 50 \
  --mineru-timeout-seconds 14400 \
  --pdf-page-chunk-size 30 \
  --llm-chunk-chars 2000 \
  --concurrency 32
```

Common options:

- `--llm-api-url`: OpenAI-compatible base URL or `/chat/completions` endpoint.
- `--llm-api-key`: API key for the LLM endpoint.
- `--llm-model`: model name sent in chat completion payloads.
- `--concurrency`: concurrent LLM correction/acceptance chunk workers, default `4`.
- `--llm-chunk-chars`: target max characters per LLM correction/acceptance chunk, default `2000`.
- `--timeout-seconds`: LLM request timeout, default `600`.
- `--mineru-api-url`: MinerU API base URL, default `https://mineru.net`.
- `--mineru-api-key`: MinerU API token. Required for PDF, image, and Office inputs.
- `--mineru-concurrency`: concurrent MinerU segment/file workers, default `12`.
- `--mineru-batch-size`: files per MinerU batch task, default `50`.
- `--mineru-timeout-seconds`: MinerU polling timeout, default `3600`.
- `--pdf-page-chunk-size`: max pages per MinerU PDF segment, default `30`.
- `--ppx-command`: PPX executable, default `/mnt/d/codex/memect-ppx/ppx`.
- `--ppx-cwd`: PPX working directory, default `/mnt/d/codex/memect-ppx`.
- `--ppx-timeout-seconds`: PPX timeout, default `3600`.
- `--ppx-backend`, `--ppx-ocr`, `--ppx-formula`: local PPX OCR combination flags. Defaults are `default`, `auto`, and `no`.
- `batch --book-concurrency`: book-level concurrency. Use `1` when the user asks to process one book at a time.
- `batch --start-index` and `batch --limit`: resume or bound directory processing by source order. Output directory numbering preserves the original source index; for example `--start-index 6 --limit 1` processes book 6 and reports index `6`.
- `process/llmcheck_batch_summary.json`: merged from the latest state rows for the discovered source directory, so repeated `--limit 1` runs build a cumulative manifest instead of replacing the whole picture with only the latest book.
- `process/llmcheck_batch_state.jsonl`: records an `in_progress` row as soon as each book starts, then records the final `passed`/`failed`/`skipped` row when the book finishes. Use it during long MinerU pending periods instead of assuming the batch is idle.

The command prints a JSON summary to stdout and progress events to stderr as JSONL. It exits `0` only when every document has `status: "passed"`; otherwise it exits `1`.

Start the GUI server:

```bash
llmcheck gui --host 127.0.0.1 --port 8766
```

Then open `http://127.0.0.1:8766`.

The GUI supports file upload, folder upload, local input path, output directory, LLM API URL/key/model, LLM concurrency, book concurrency, MinerU API settings, MinerU batch size, PDF chunk size, PPX command/cwd/timeout, and local OCR combination flags for PPX backend/OCR/formula. Job status responses include current-book MinerU segment counts, cloud state counts, PPX audit size, and latest status age when segment status files exist.

## Outputs

LLMcheck writes under `--output-dir`:

- `md/*.md`: accepted final Markdown only for passed documents.
- `文字版pdf/*.pdf`: optimized text PDFs for passed documents.
- `process/drafts/*.md`: corrected drafts, including drafts that fail final acceptance.
- `process/preprocess/*`: MinerU formal Markdown, PPX audit Markdown for PDFs, split segments, zips, and preprocessing manifests.
- `process/reports/*.quality.json`: deterministic cleanup quality hints and errors.
- `process/reports/*.llm_correction.json`: correction prompt/result metadata and LLM result.
- `process/reports/*.llm_acceptance.json`: acceptance prompt/result metadata and LLM result.
- `process/reports/*.local_repair.json`: deterministic failed-item repair metadata when acceptance exposed a locally repairable issue.
- `process/reports/*.llm_repair.json` and `process/reports/*.llm_repair_chunks/*.json`: failed-acceptance LLM repair metadata when repair was attempted.
- `process/reports/llmcheck_manifest.jsonl`: one document result per line.
- `process/reports/llmcheck_summary.json`: single-job status, counts, settings, and document rows.
- `process/books/<index>_<book>/*`: per-book process outputs for `llmcheck batch`.
- `process/llmcheck_batch_state.jsonl` and `process/llmcheck_batch_summary.json`: directory-level `llmcheck batch` progress and summary.

Document statuses include `passed`, `correction_failed`, `empty_corrected_text`, `acceptance_failed`, and `error`.

## Agent Workflow

1. Confirm the input path, output directory, LLM endpoint, model, and required API keys.
2. For PDF/image/Office inputs, confirm MinerU credentials are available.
3. For PDF inputs, confirm local PPX exists at the configured path or pass `--ppx-command` and `--ppx-cwd`; set `--ppx-ocr` / `--ppx-formula` only when the user wants a non-default local OCR combination. Do not require PPX for image or Office inputs.
4. For a directory of books, run `llmcheck batch` with `--book-concurrency 1` unless the user explicitly allows multiple books at once. Use at least `--concurrency 10` for LLM chunk correction/acceptance/repair when the endpoint can handle it, and raise `--mineru-concurrency` for MinerU segments separately.
5. Report progress in plain language with book name, stage, progress counts, and current result. Example: "第 6 本《14本草备要讲解》：PPX 审计已完成；MinerU 38 段，2 段 cached，36 段 polling，云端状态 pending，还未进入 LLM。"
6. Read `process/reports/llmcheck_summary.json` first for a single book, or `process/llmcheck_batch_summary.json` for directory runs. If the status is not passed, inspect failed document rows and the corresponding correction/acceptance reports.
7. Treat `process/drafts/*.md` from failed acceptance as candidate text requiring manual review; do not present it as final delivery.
8. Use `md/*.md` and `文字版pdf/*.pdf` only for documents whose status is `passed`.

## Notes And Caveats

- The LLM must return a JSON object; invalid or non-JSON LLM responses are recorded as errors.
- Correction and acceptance are chunked and run concurrently; increase `--concurrency` to raise LLM chunk parallelism when the endpoint can handle it.
- MinerU segment status is written to `process/preprocess/<book>/mineru/segment_*/status.json`. Timeout states are resumable when a MinerU `batch_id` is present or recoverable from an older timeout error message, so reruns should continue polling instead of resubmitting completed uploads.
- `llmcheck batch` writes an in-progress state row before invoking the book runner, so status monitors can report the active book while MinerU is still polling.
- `llmcheck batch` skips an already passed book only after validating that the summary belongs to the same input and the final Markdown/PDF artifacts exist and are non-empty. Skipped passed books count as successful in the batch summary.
- Empty source selections or out-of-range `--start-index` return `review_required`, not `passed`.
- Incremental one-book runs append to `process/llmcheck_batch_state.jsonl`; the summary deduplicates by source path and keeps the latest row per book.
- MinerU upload URLs must be HTTPS.
- PDF page counting requires `pdfinfo`; PDF splitting requires `qpdf` when a PDF exceeds the configured chunk size.
- The test suite includes an end-to-end batch/PDF path covering MinerU formal Markdown, PPX audit, LLM correction, LLM acceptance, final Markdown, and text PDF output.
- LLMcheck is conservative by design. Uncertain textual issues should remain in the text and appear in `unresolved_issues` or acceptance reports.
- API keys and document text may be sent to external services configured by the user. Do not hard-code secrets in commands or saved files.
