---
name: llmcheck
description: Use when an agent needs to run or explain LLMcheck: a profile-driven CLI/GUI workflow that converts Markdown, PDF, image, and Office files into cleaned standard Markdown, performs conservative LLM correction and acceptance, runs whole-document finalization/final acceptance, emits final Markdown/text PDF delivery folders, and keeps process artifacts under process/.
---

# LLMcheck

Use this skill when the user wants to process source documents with LLMcheck, inspect LLMcheck outputs, choose a document profile, or prepare a repeatable command for conversion, cleanup, finalization, and standard-document delivery.

## What It Does

LLMcheck normalizes supported source files to Markdown, then runs deterministic cleanup, profile-aware chunked LLM correction, chunked LLM acceptance, targeted local repair, optional LLM repair, whole-document finalization, final acceptance, and final artifact generation.

The default profile is `general_standard_document`. Domain behavior is explicit and selectable with `--profile`; Chinese medicine is available as `chinese_medicine_reference`, not the default system identity.

Built-in profiles:

- `general_standard_document`
- `academic_paper`
- `technical_manual`
- `legal_contract`
- `financial_report`
- `medical_reference`
- `chinese_medicine_reference`

Run:

```bash
llmcheck profiles
```

## Supported Inputs

Pass either a single file or a directory to `--input`. Directories are scanned recursively for supported files.

- Markdown: `.md`
- PDF: `.pdf`
- Images: `.png`, `.jpg`, `.jpeg`, `.jp2`, `.webp`, `.gif`, `.bmp`
- Office: `.doc`, `.docx`, `.ppt`, `.pptx`, `.xls`, `.xlsx`

Input flow:

- Markdown enters cleanup, correction, acceptance, finalization, and final acceptance directly.
- PDF starts local PPX audit and MinerU API VLM in parallel. MinerU is the formal text source for cleanup and final output; PPX Markdown is retained only for audit/reference and repair context.
- MinerU splits PDFs into page chunks by `--pdf-page-chunk-size`, default `30`, submits chunks concurrently, and merges MinerU chunk Markdown back in page order.
- Images and Office files go directly to MinerU API VLM without PPX.

## CLI

Run one file or one directory as a single LLMcheck job:

```bash
llmcheck run \
  --profile general_standard_document \
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
  --profile technical_manual \
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

- `--profile`: document profile, default `general_standard_document`.
- `--llm-api-url`: OpenAI-compatible base URL or `/chat/completions` endpoint.
- `--llm-api-key`: LLM API key.
- `--llm-model`: model name.
- `--mineru-api-key`: MinerU API token, or `MINERU_CLOUD_API_TOKEN`.
- `--concurrency`: LLM chunk correction/acceptance concurrency.
- `--acceptance-repair-rounds`: LLM repair rounds after failed acceptance.

## Outputs

The output directory has exactly three top-level folders:

```text
output/
  md/           # final Markdown for passed documents only
  文字版pdf/    # text PDF for passed documents only
  process/      # drafts, preprocessing artifacts, reports, caches, batch state
```

Only documents whose summary row is `passed` are valid delivery artifacts. Do not use `process/drafts/*.md` as final output.

Important reports under `process/reports/`:

- `*.quality.json`: deterministic cleanup quality errors and hints.
- `*.llm_correction.json` and `*.llm_correction_chunks/*.json`: correction metadata and chunk cache.
- `*.llm_acceptance.json` and `*.llm_acceptance_chunks/*.json`: acceptance metadata and chunk cache.
- `*.local_repair.json`: deterministic local repair metadata.
- `*.llm_repair.json` and `*.llm_repair_chunks/*.json`: LLM repair metadata.
- `*.finalization.json`: whole-document finalization changes.
- `*.final_acceptance.json`: final quality gate before writing `md/` and `文字版pdf/`.
- `llmcheck_manifest.jsonl`: one document result per line.
- `llmcheck_summary.json`: single-job status, counts, settings, and document rows.
- `llmcheck_batch_state.jsonl` and `llmcheck_batch_summary.json`: directory-level batch state and summary.

Final outputs are valid only when:

- `process/reports/llmcheck_summary.json` or `process/llmcheck_batch_summary.json` reports `passed`.
- Each document row has `status == "passed"`.
- The corresponding `*.final_acceptance.json` has `accepted == true`.

## Agent Workflow

1. Ask or infer the right profile. Use `general_standard_document` unless the source clearly fits a more specific profile.
2. Run `llmcheck profiles` if the profile list is needed.
3. Prepare `llmcheck run` for a single input or `llmcheck batch` for a source directory.
4. Keep `--output-dir` outside the source directory when possible.
5. After completion, read the summary first.
6. If status is not `passed`, inspect the failed row and then its correction, acceptance, repair, finalization, and final acceptance reports.
7. Use `md/` and `文字版pdf/` only for passed rows.

## Notes And Caveats

- LLMcheck is conservative: it should repair OCR, punctuation, segmentation, abnormal paragraphing, physical line breaks, mojibake, and obvious layout artifacts without summarizing, modernizing, or inventing facts.
- Profile rules protect domain-specific content such as legal clauses, financial figures, technical commands, medical doses, citations, formulas, tables, and code blocks.
- Chunk caches are profile-aware. Re-running the same output directory with a different profile should not reuse mismatched chunk results.
- Batch resume validates final Markdown/PDF artifacts before skipping already passed books.
- For PDF inputs, MinerU output is the formal text source. PPX is audit/reference material.
