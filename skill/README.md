# LLMcheck Agent Skill

This directory contains the publishable agent skill for LLMcheck.

LLMcheck is a profile-driven document processing workflow. It accepts Markdown, PDF, image, and Office files, normalizes them to Markdown, runs deterministic cleanup, performs conservative profile-aware LLM correction and acceptance, applies targeted repairs when needed, runs whole-document finalization and final acceptance, writes final Markdown and text PDFs to delivery folders, and keeps drafts, preprocessing artifacts, caches, and JSON reports under `process/`.

## Skill Contents

- `SKILL.md`: agent-facing operating procedure.
- `README.md`: this packaging note for humans reviewing or publishing the skill.

## Profiles

The default profile is `general_standard_document`. Agents can inspect built-ins with:

```bash
llmcheck profiles
```

Built-ins include `academic_paper`, `technical_manual`, `legal_contract`, `financial_report`, `medical_reference`, and `chinese_medicine_reference`.

## When To Use

Use the skill when an agent needs to:

- Explain what LLMcheck does and which file types it accepts.
- Choose or explain a document profile.
- Prepare or run `llmcheck run`, `llmcheck batch`, or `llmcheck gui`.
- Interpret `md`, `文字版pdf`, and `process` outputs.
- Diagnose failed correction, acceptance, repair, finalization, or final acceptance reports.

## Required Runtime Context

Typical CLI use needs:

- An installed `llmcheck` command.
- An OpenAI-compatible chat completion endpoint, API key, and model name.
- A MinerU API token for PDF, image, and Office inputs.
- Local PPX for PDF audit conversion only.
- `pdfinfo` for PDF page counts.
- `qpdf` for splitting PDFs larger than the configured page chunk size.

## Quick Example

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
  --mineru-batch-size 50 \
  --pdf-page-chunk-size 30 \
  --ppx-ocr auto \
  --llm-chunk-chars 2000 \
  --concurrency 10
```

For a directory of books:

```bash
llmcheck batch \
  --profile technical_manual \
  --source-dir /path/to/books \
  --output-dir /path/to/output \
  --book-concurrency 1 \
  --start-index 1 \
  --limit 1 \
  --llm-api-url http://127.0.0.1:3022 \
  --llm-api-key "$LLM_API_KEY" \
  --llm-model gpt-5.5
```

The command returns success only when all documents pass final acceptance. Delivery artifacts are valid only when the summary row is `passed` and the matching `*.final_acceptance.json` has `accepted=true`.

## Publication Notes

Publish the `skill/` directory as the package skeleton. Do not include runtime outputs, credentials, virtual environments, uploaded source files, or generated reports in the skill package.
