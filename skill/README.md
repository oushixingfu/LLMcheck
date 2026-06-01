# LLMcheck Agent Skill

This directory contains a publishable agent skill for LLMcheck.

LLMcheck is a document-processing workflow for Chinese medicine source books. It accepts Markdown, PDF, image, and Office files, normalizes them to Markdown, runs deterministic text cleanup, performs conservative chunked/concurrent LLM correction, performs chunked/concurrent LLM acceptance, applies targeted local repairs for known failed acceptance items, repairs failed chunks with LLM when needed, writes final Markdown and optimized text PDFs to delivery folders, and keeps drafts, preprocessing artifacts, and JSON reports under `process/`.

Current input flow:

- Markdown enters LLMcheck directly for cleanup, chunked/concurrent correction, and chunked/concurrent acceptance.
- PDF starts local PPX audit and MinerU API VLM in parallel. PDFs are split into concurrent 30-page MinerU chunks by default; MinerU output becomes the formal text source, and PPX output is retained for audit/reference without blocking LLM correction after MinerU has merged.
- Images (`.png`, `.jpg`, `.jpeg`, `.jp2`, `.webp`, `.gif`, `.bmp`) and Office files (`.doc`, `.docx`, `.ppt`, `.pptx`, `.xls`, `.xlsx`) go directly to MinerU API VLM and do not use PPX.
- Cleanup preserves lessons from the latest batch: table-image `flowchart` transcription, local structure-glue splitting, bibliography glue splitting, and deterministic failed-item repair before LLM repair escalation.

## Skill Contents

- `SKILL.md`: the agent-facing skill definition and operating procedure.
- `README.md`: this packaging note for humans reviewing or publishing the skill.

## When To Use

Use the skill when an agent needs to:

- Explain what LLMcheck does and which file types it accepts.
- Prepare or run `llmcheck run` or `llmcheck gui`.
- Prepare or run `llmcheck batch` for one-book-at-a-time directory processing.
- Interpret `md`, `文字版pdf`, and `process` outputs.
- Diagnose failed correction or acceptance results from JSON reports.
- Document operational caveats around MinerU API VLM, PPX-as-audit for PDFs, local PPX OCR combinations, 30-page PDF chunking, table-image fallback transcription, failed-item local repair, API keys, and chunked/concurrent LLM calls.

## Required Runtime Context

Typical CLI use needs:

- An installed `llmcheck` command.
- An OpenAI-compatible chat completion endpoint, API key, and model name.
- A MinerU API token for PDF, image, and Office inputs.
- Local PPX for PDF audit conversion only, with configurable backend/OCR/formula flags.
- `pdfinfo` for PDF page counts.
- `qpdf` for splitting PDFs larger than the configured page chunk size, default `30`.

## Quick Example

```bash
llmcheck run \
  --input /path/to/file-or-directory \
  --output-dir /path/to/output \
  --llm-api-url http://127.0.0.1:3022 \
  --llm-api-key "$LLM_API_KEY" \
  --llm-model gpt-5.5 \
  --mineru-api-key "$MINERU_CLOUD_API_TOKEN" \
  --mineru-concurrency 12 \
  --mineru-batch-size 50 \
  --pdf-page-chunk-size 30 \
  --ppx-ocr auto \
  --llm-chunk-chars 2000 \
  --concurrency 10
```

For a directory of books, use `llmcheck batch --book-concurrency 1 --start-index <n> --limit 1`; start LLM chunk parallelism at `--concurrency 10` and raise `--mineru-concurrency` separately when the configured endpoints can handle it.

Batch resume keeps the original source index, validates final Markdown/PDF artifacts before skipping an already passed book, merges latest state rows into a cumulative summary, and treats empty selections as `review_required`.

The command prints a JSON summary and progress JSONL, and returns success only when all documents pass final LLM acceptance. The test suite includes an end-to-end batch/PDF path covering MinerU formal Markdown, PPX audit, LLM correction, LLM acceptance, final Markdown, and text PDF output.

## Publication Notes

The skill is intentionally self-contained in `SKILL.md`. Publish the `skill/` directory as the package skeleton, and avoid adding runtime outputs, credentials, virtual environments, or generated reports to the skill package.
