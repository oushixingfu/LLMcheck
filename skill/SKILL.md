---
name: llmcheck
description: Convert PDF/Markdown/image/Office documents into gate-checked Markdown via LLMcheck. Use when an agent must run document conversion, inspect job status, fetch only passed final md/, or list profiles through the agent JSON contract.
---

# LLMcheck

Use this skill when the user or workflow needs stable document → Markdown conversion with MinerU API acquisition, optional local PPX (opt-in only), deterministic cleaning, structure normalization, quality gates, and optional LLM review.

## Install (once per machine)

Agent Skills install (Claude Code / Codex / Cursor-compatible):

```bash
npx skills@latest add oushixingfu/LLMcheck -g -y
```

Manual Claude Code install:

```bash
git clone https://github.com/oushixingfu/LLMcheck.git /tmp/LLMcheck
cp -R /tmp/LLMcheck/skill ~/.claude/skills/llmcheck
```

Install the Python runtime (required — this skill drives the CLI/package):

```bash
python3.12 -m pip install "git+https://github.com/oushixingfu/LLMcheck.git"
# or from a local clone:
# pip install -e /path/to/LLMcheck
llmcheck agent profiles
```

## Iron rules

1. Prefer `llmcheck agent convert|status|get-md|profiles` (JSON on stdout).
2. Only trust final Markdown under `md/` when document `status == "passed"`.
3. **`passed` is not enough for semantic use:** delivery MD must be machine-readable **semantic units** (heading hierarchy + body under each body heading; no whole-book mega-line collapse). Spot-check `max_line`, heading count, and empty shells before treating a book as QA/claim-ready.
4. Never treat `process/drafts/**` as delivery content.
5. If structure collapsed after convert (few headings, mega-line body) while `process/clean/*.cleaned.md` still looks structured: **rebuild from cleaned / re-run local-gate after code fix — do not re-run MinerU by default**.
6. Credentials come from environment variables — do not hardcode secrets.
7. Keep `--output-dir` outside the source tree when possible.

## Credentials (env)

```bash
export LLM_API_URL="https://api.example.com/v1"   # or OpenAI-compatible base
export LLM_API_KEY="..."
export LLM_MODEL="deepseek-v4-pro"                # optional override
export MINERU_CLOUD_API_TOKEN="..."               # PDF / image / Office
# optional aliases also accepted: LLMCHECK_*, OPENAI_*, MINERU_API_KEY
```

Agent convert defaults to `--llm-mode local-gate` so deterministic clean/gates can run without LLM credentials. Use `review-first` only when an LLM reviewer is configured.

## Short workflow

```bash
llmcheck agent profiles

llmcheck agent convert \
  --input /path/to/file-or-dir \
  --output-dir /path/to/output \
  --profile general_standard_document \
  --llm-mode local-gate
# PPX is OFF by default. Only add when the task explicitly requires local PPX:
# --enable-ppx --mineru-fallback ppx

llmcheck agent status --output-dir /path/to/output

llmcheck agent get-md \
  --output-dir /path/to/output \
  --document-id <document_id> \
  --max-chars 4000
```

Python equivalent:

```python
from llmcheck import agent_api

print(agent_api.list_profiles())
report = agent_api.submit_convert(
    input_path="/path/to/file",
    output_dir="/path/to/output",
    llm_mode="local-gate",
)
job = agent_api.get_job(output_dir="/path/to/output")
doc_id = job["documents"][0]["document_id"]
md = agent_api.get_final_markdown(output_dir="/path/to/output", document_id=doc_id)
```

## Pipeline (what the tool does)

```text
upload/path
→ acquisition (MinerU API; optional local PPX only if --enable-ppx)
→ Cross Select → process/.../cross/initial.md
→ deterministic clean → process/.../clean/*.cleaned.md
→ structure finalize (must not collapse headings into mega-lines)
→ pre-LLM gate (includes mega_line / low_heading_density)
→ optional LLM (local-gate skips)
→ final gate
→ md/ only if accepted
```

**Delivery contract (semantic units):**

- Hierarchy should support later QA/claim extraction (`#` major / `##` section / `###` entry as profile allows).
- Body headings should carry complete following content until the next heading (no intentional empty shells except true parents).
- Soft line-join must not glue an entire book into one line.
- If pre_llm/final is worse than cleaned on heading count / max line length, pipeline prefers cleaned (structure guard).

## Delivery policy

| Status | Agent may use |
|--------|----------------|
| `passed` | `md/<id>.md` via path or `get-md` body — still spot-check structure before large-scale extraction |
| `review_required` / `failed` / `pre_llm_quality_failed` | reports under `process/` only — no final body |
| collapsed body but cleaned OK | re-run convert/local-gate after fix, or rebuild from `process/clean/*.cleaned.md`; **do not re-MinerU first** |

`get-md` returns text only for passed documents.

## JobReport (`schema_version=1.0`)

```json
{
  "schema_version": "1.0",
  "job_id": "...",
  "status": "passed|review_required|failed|running",
  "profile_id": "general_standard_document",
  "documents": [
    {
      "document_id": "...",
      "status": "passed",
      "final_markdown_path": ".../md/x.md",
      "final_markdown_sha256": "...",
      "final_acceptance_report_path": "...",
      "cross_report_path": "...",
      "error": ""
    }
  ],
  "artifacts": {
    "md_dir": ".../md",
    "process_dir": ".../process"
  }
}
```

## Exit codes

- `0` — passed / successful read
- `1` — review_required, failed, or non-passed get-md
- `2` — clear config/input error

## Profiles

Default: `general_standard_document`.

Also: `academic_paper`, `technical_manual`, `legal_contract`, `financial_report`, `medical_reference`, `chinese_medicine_reference`.

## Supported inputs

- Markdown: `.md`
- PDF: `.pdf`
- Images: `.png`, `.jpg`, `.jpeg`, `.jp2`, `.webp`, `.gif`, `.bmp`
- Office: `.doc`, `.docx`, `.ppt`, `.pptx`, `.xls`, `.xlsx`

## Trigger phrases

Use this skill when the user says things like:

- "用 LLMcheck 转成 md"
- "convert this PDF to clean markdown"
- "run document quality gates"
- "llmcheck agent convert …"

## Notes

- Plain MD upgrades (no MinerU): run structure finalize + local-gate; first-heading/prefix cleanup is part of finalize for textbook exports.

- Runtime dependency: Python ≥3.12 package `LLMcheck` from this repository.
- **Local PPX is OFF by default.** Only pass `--enable-ppx` (and optionally `--mineru-fallback ppx`) when the user/task explicitly requires PPX. Default dual-start of PPX can freeze the machine.
- MinerU token is required for non-Markdown conversion without cache.
- Prefer **one book at a time** when iterating cleaning/layout rules; do not fan out whole series by default.
- Human CLI (`llmcheck run` / `batch` / `gui`) still exists; agents should prefer `llmcheck agent *`.
- Design: `docs/prd/2026-07-18-agent-callable-dual-engine-pipeline.md` in the repo.
