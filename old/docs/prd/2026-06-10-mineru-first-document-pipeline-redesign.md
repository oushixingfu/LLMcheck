# MinerU First Document Pipeline Redesign PRD

Status: draft for implementation  
Owner: product / architecture  
Date: 2026-06-10  
Supersedes: `docs/prd/2026-06-05-cleaning-and-llm-review-redesign.md` for the workflow described here

## Problem Statement

LLMcheck needs to become a full document-to-Markdown production pipeline, not only a Markdown cleanup and LLM review tool.

The target corpus is under `D:\pdf\output2\0`, and final Markdown must be delivered to `D:\pdf\output2\md`. Existing delivered Markdown files in `D:\pdf\output2\md` must be treated as already processed and skipped. The current observed corpus state on 2026-06-10 is:

- `D:\pdf\output2\0` exists and contains 112 supported source files.
- All 112 observed source files are `.md`.
- `D:\pdf\output2\md` exists and contains 9 Markdown outputs.
- The existing 9 outputs are numbered `100` through `108`, so skip logic must be based on output filename identity or source-to-output manifest, not on "first 9 files".

The redesigned flow must support both cases:

- New raw input files: PDF, images, Word, PowerPoint, and Excel files are converted through MinerU first.
- Existing Markdown input files: skip conversion and enter cleanup plus LLM verification directly.

Credentials may be configured plainly in the local workflow when the operator accepts that risk. They should not be committed to git-tracked project files by default. MinerU and LLM credentials must be accepted through CLI flags, environment variables, `.env`, or GUI inputs.

## Product Goals

1. Generate Markdown from supported raw source documents using MinerU standard API by default.
2. Split long PDF documents intelligently before MinerU submission, defaulting to 30-page chunks.
3. Respect MinerU API limits and quotas with bounded concurrency, resumable batches, cache reuse, and clear retry/fallback behavior.
4. Fall back to local PPX Markdown generation when MinerU is unavailable or blocked.
5. Clean generated or existing Markdown through a deterministic, auditable rule pipeline.
6. Use LLM review as a whole-document human-standard final gate, not as an uncontrolled rewriting step.
7. When LLM review finds recurring defects, route the document back to deterministic cleanup rule improvement or safe removal of useless artifacts.
8. Process documents by numeric filename order and avoid reprocessing already delivered books.
9. Compare `mimo-v2.5-pro` and `gpt-5.5` on the first three target books before setting the Stage 3 default reviewer.
10. If `mimo-v2.5-pro` and `gpt-5.5` quality is equivalent, prefer `mimo-v2.5-pro` and use `gpt-5.5` as fallback.
11. If both configured reviewer models are unavailable, stop and request updated LLM service information.
12. Produce only accepted Markdown in `D:\pdf\output2\md`.

## Non-Goals

- Do not commit API keys, tokens, service URLs, or model credentials to git-tracked files unless the operator explicitly accepts a local-only plaintext workflow file.
- Do not require database schema changes for the first implementation.
- Do not rewrite the whole GUI before the CLI pipeline is stable.
- Do not use LLM output as final truth when deterministic checks show structural or content-loss risk.
- Do not process multiple books concurrently for this corpus unless explicitly approved later.

## Evidence From Current Project

Current code already contains useful building blocks:

- `llmcheck/preprocess.py` supports `.pdf`, images, `.doc`, `.docx`, `.ppt`, `.pptx`, `.xls`, `.xlsx`, and `.md`.
- Default MinerU model is already `vlm`.
- Default PDF chunk size is already 30 pages.
- Default MinerU local upload batch size is already 50 files.
- MinerU batch submission, upload URL handling, polling, cache reuse, and resumable status files already exist.
- Current PDF preprocessing starts PPX as an audit/reference path, but does not formally fall back to PPX when MinerU is unavailable. This must change.
- Current batch orchestration supports `--book-concurrency`, `--start-index`, `--limit`, and summary/state files.
- Existing final gate and LLM review code should be reused instead of replaced wholesale.

Current gaps against the new requirement:

- No explicit "MinerU unavailable -> PPX becomes formal Markdown source" mode.
- No first-class manifest that distinguishes `mineru`, `ppx_fallback`, and `existing_markdown` as source-of-truth modes.
- Skip logic needs to treat existing files in final `md` output as completed, regardless of their numeric position.
- LLM model availability should be checked before large batch execution.
- The retry loop from LLM review back to cleanup rule improvement needs an explicit state machine, iteration cap, and report contract.

## Official MinerU Constraints

The implementation must track MinerU standard API behavior from the official documentation at `https://mineru.net/apiManage/docs`:

- Standard API requires `Authorization: Bearer <token>`.
- Supported file types include PDF, images `png/jpg/jpeg/jp2/webp/gif/bmp`, Doc, Docx, Ppt, PPTx, Xls, and Xlsx.
- Single file limit is 200 MB and 200 pages.
- `model_version` supports `pipeline`, `vlm`, and `MinerU-HTML`; this project defaults non-HTML files to `vlm`.
- Standard API is asynchronous: submit, upload if local batch, poll, then download result zip.
- For standard result zips, `full.md` is the Markdown result.
- Local batch upload link requests are limited to 50 files per request.
- Each account receives 1000 highest-priority pages per day; excess pages have lower priority.
- Relevant failure codes include token errors, file size/page count errors, model temporarily unavailable, queue full, retry limit reached, daily task limit reached, and conversion failures.

## Target User Stories

1. As an operator, I want to point the pipeline at `D:\pdf\output2\0`, so that books are processed in numeric filename order.
2. As an operator, I want existing delivered files in `D:\pdf\output2\md` to be skipped, so that reruns are safe.
3. As an operator, I want `.md` sources to bypass MinerU, so that already converted books are cleaned and verified directly.
4. As an operator, I want PDF/image/Office files to use MinerU `vlm`, so that Markdown generation uses the preferred OCR/layout model.
5. As an operator, I want PDFs split into 30-page chunks by default, so that 200-page API limits are not hit and retries are smaller.
6. As an operator, I want MinerU concurrency and batch size configurable, so that I can stay below account and API pressure limits.
7. As an operator, I want MinerU failures to fall back to PPX, so that work can continue when cloud parsing is unavailable.
8. As an operator, I want fallback usage recorded, so that lower-confidence outputs can be reviewed.
9. As an operator, I want cleanup rules to be deterministic and auditable, so that repeated defects become code fixes rather than one-off LLM edits.
10. As an operator, I want the LLM to read the whole document as a human reviewer, so that page-level and chunk-boundary defects are caught.
11. As an operator, I want LLM-discovered defects to feed back into cleanup, so that later books benefit from improved rules.
12. As an operator, I want the first three books reviewed by both `mimo-v2.5-pro` and `gpt-5.5`, so that the default reviewer is based on observed quality rather than assumption.
13. As an operator, I want `mimo-v2.5-pro` to become the preferred model only if its quality matches `gpt-5.5`, so that cost/speed/provider preference does not reduce acceptance quality.
14. As an operator, I want `gpt-5.5` to remain the fallback reviewer, so that the pipeline has a known backup model under the same service URL.
15. As an operator, I want the batch to stop when neither configured reviewer model is available, so that it does not silently switch to an unapproved reviewer.
16. As an operator, I want concise per-book reports, so that I can see whether a book was passed, skipped, failed, or needs manual review.
17. As an operator, I want credential handling to match the local workflow policy, so that plaintext local configuration is possible without accidentally publishing credentials.

## Solution Overview

The target pipeline is:

```text
discover sources
-> sort by leading filename number
-> skip if final md already exists or manifest says accepted
-> Stage 1: Markdown acquisition
   -> existing .md: use as acquired Markdown
   -> PDF/image/Office: MinerU vlm
   -> if MinerU unavailable: PPX fallback
-> Stage 2: deterministic cleanup
-> Stage 3: quality gates
-> Stage 4: whole-document LLM review with selected default model
-> if defects are cleanup-solvable: improve/apply deterministic cleanup and rerun
-> if defects are useless generated artifacts: safely remove and rerun
-> if defects are source loss or uncertain: manual review required
-> accepted final Markdown written to D:\pdf\output2\md
```

## Target State Machine

Each book must have one authoritative state:

```text
pending
-> skipped_existing
-> acquiring_markdown
-> acquired_by_mineru | acquired_by_ppx_fallback | acquired_existing_md
-> cleaning
-> quality_gate
-> llm_final_review
-> cleanup_revision_required
-> cleaning
-> accepted
-> delivered
```

Failure branches:

```text
acquiring_markdown -> mineru_failed -> ppx_fallback
ppx_fallback -> ppx_failed -> failed_acquisition
llm_final_review -> primary_model_unavailable -> retry_fallback_model
llm_final_review -> all_review_models_unavailable -> blocked_needs_llm_service
llm_final_review -> source_loss_or_uncertain -> manual_review_required
quality_gate -> deterministic_blocker -> cleanup_revision_required
```

Iteration limits:

- Default maximum cleanup/review loops per book: 3.
- If the same blocker repeats after 3 loops, mark `manual_review_required`.
- Batch should continue to the next book only when the failure is local to one book. If LLM service or MinerU account/authentication is globally invalid, stop the batch.

## Implementation Decisions

### 1. Markdown Acquisition Module

Build a stable acquisition interface that returns:

- acquisition status
- source path
- acquired Markdown path
- acquisition mode: `existing_md`, `mineru`, `ppx_fallback`
- source hashes
- page/chunk metadata when available
- external task IDs without unnecessary credential duplication
- warnings and confidence flags

This should encapsulate MinerU, PPX, caching, and fallback so downstream cleanup does not care how Markdown was produced.

### 2. MinerU Strategy

MinerU standard API is the primary converter for PDF, images, Doc, Docx, Ppt, PPTx, Xls, and Xlsx.

Defaults:

- model: `vlm`
- PDF page chunk size: 30
- local batch upload size: 50
- book concurrency for `D:\pdf\output2`: 1
- MinerU task concurrency: configurable and bounded
- polling with timeout, retry, backoff, and resumable status files

The implementation should preflight:

- API key exists.
- Model setting is valid.
- Batch size does not exceed official local upload limit.
- File extensions are supported.
- File size is below 200 MB.
- PDF page chunks do not exceed 200 pages.

### 3. PPX Fallback Strategy

PPX is no longer only an audit/reference path. It becomes a formal fallback when:

- MinerU API key is missing.
- MinerU returns auth/token errors.
- MinerU returns model unavailable, queue full, retry exhausted, task limit, or transient service errors.
- MinerU polling times out after configured retries.

PPX fallback must be recorded as lower-confidence acquisition. The rest of the cleanup and LLM review pipeline still applies. If PPX is also unavailable, the book fails acquisition with a clear diagnostic.

### 4. Existing Markdown Input

Existing `.md` files enter the pipeline as `existing_md`.

They must still receive:

- deterministic cleanup
- quality gate
- whole-document LLM review
- final delivery manifest

They must not call MinerU or PPX unless the operator explicitly forces reconversion from a raw source.

### 5. Skip And Ordering

Discovery order must use leading filename number first, then filename as tie-breaker.

Skip rules:

- If final output `D:\pdf\output2\md\<source-stem>.md` exists and is non-empty, mark `skipped_existing`.
- If a process manifest maps the source hash to an accepted delivered Markdown, mark `skipped_existing`.
- Do not assume "already generated 9 books" means "skip numeric 1-9"; current observed outputs are `100` to `108`.

### 6. Deterministic Cleanup

Cleanup should be rule-driven and report every change:

- rule id
- category
- risk level
- before/after hash
- affected count
- whether it can be repeated safely

Initial rule families:

- control and zero-width character removal
- line ending normalization
- Markdown table normalization
- HTML table artifact conversion
- MinerU noise block removal
- LaTeX unit artifact cleanup
- safe soft-wrap line merge
- repeated running header/footer removal
- abnormal CJK spaces
- forced line breaks
- page/chunk boundary artifacts
- empty decorative lines and useless OCR fragments

Rules that remove content must be conservative and easy to audit.

### 7. LLM Final Review And Model Selection

The LLM reviewer reads the whole document and returns JSON only.

Required LLM service behavior:

- Base URL is supplied by config.
- API key is supplied by config.
- Candidate models are `mimo-v2.5-pro` and `gpt-5.5` on the same service URL.
- Before changing the default, run the same first three books through both models and compare acceptance status, deterministic final gate status, issue count, blocking issue categories, and manual spot-check notes.
- If quality is equivalent, set `mimo-v2.5-pro` as the preferred reviewer and `gpt-5.5` as fallback.
- If `mimo-v2.5-pro` quality is worse or produces unstable JSON/review behavior, keep `gpt-5.5` as preferred until the defect is understood.
- If both models are unavailable, stop and request updated LLM service information. Do not silently switch to an unapproved model.

Review output must classify issues as:

- `accepted`
- `cleanup_rule_required`
- `safe_artifact_removal_required`
- `manual_review_required`
- `source_conversion_failed`
- `model_unavailable`

The reviewer must identify:

- location hint
- exact observed anomaly
- severity
- whether source meaning is at risk
- recommended deterministic rule family when applicable
- whether the issue is recurring

LLM may propose fixes, but deterministic cleanup owns repeated text transformations.

### 8. Cleanup Feedback Loop

When LLM review finds issues:

- If issue is a deterministic cleanup defect, add or apply a cleanup rule and rerun cleanup plus gates.
- If issue is useless generated content, remove it only when a safe rule can identify it repeatably.
- If issue may remove source meaning, mark manual review required.

The pipeline must not hide failures by directly asking LLM to rewrite the entire document.

### 9. Reports And Artifacts

Each book should write process artifacts under a process root, not directly into final output until accepted.

Required reports:

- acquisition manifest
- MinerU task/chunk manifest
- PPX fallback manifest when used
- cleanup report
- deterministic quality report
- LLM review report
- cleanup loop history
- final delivery manifest
- batch summary

Reports should avoid copying full credentials by default and include enough evidence to resume or debug.

Final Markdown path for this workflow:

- `D:\pdf\output2\md`

Recommended process root:

- `D:\pdf\output2\process`

## CLI Requirements

Add or standardize a command shape equivalent to:

```powershell
llmcheck batch `
  --input "D:\pdf\output2\0" `
  --output-md-dir "D:\pdf\output2\md" `
  --process-dir "D:\pdf\output2\process" `
  --profile chinese_medicine_reference `
  --book-concurrency 1 `
  --mineru-model vlm `
  --pdf-page-chunk-size 30 `
  --mineru-batch-size 50 `
  --llm-model mimo-v2.5-pro `
  --fallback-models gpt-5.5
```

The existing `--output-dir` behavior may be retained for backward compatibility, but this workflow needs explicit final and process destinations to avoid the existing source-under-output discovery trap.

Required CLI additions or clarifications:

- `--output-md-dir`
- `--process-dir`
- `--skip-existing`
- `--mineru-fallback ppx`
- `--max-cleanup-loops`
- `--preflight-only`
- `--dry-run`
- `--stop-on-global-service-error`
- `--model-compare mimo-v2.5-pro,gpt-5.5`
- `--model-compare-limit 3`

## GUI Requirements

GUI changes are secondary to CLI stability.

After CLI implementation is verified, expose:

- input directory
- final Markdown directory
- process directory
- model/service preflight result
- skip-existing toggle
- MinerU settings
- PPX fallback settings
- book-level progress
- accepted/skipped/failed/manual-review counts
- current blocker with masked diagnostics

## Testing Decisions

Tests must verify external behavior and report contracts, not private implementation details.

Required unit tests:

- supported file discovery includes PDF/images/Office/Markdown
- numeric filename sorting
- existing final Markdown skip
- MinerU batch size capped at 50
- PDF page segmentation defaults to 30 pages
- MinerU unavailable routes to PPX fallback
- PPX unavailable produces acquisition failure
- `.md` input bypasses MinerU and PPX
- LLM model unavailable blocks batch with a clear status
- model comparison runs both `mimo-v2.5-pro` and `gpt-5.5` on identical source books
- `mimo-v2.5-pro` is selected as preferred only when comparison quality is equivalent
- `gpt-5.5` is used as fallback when the preferred model is unavailable
- cleanup loop stops after configured max loops
- reports follow the local credential-handling policy

Required integration tests with fakes:

- raw PDF -> fake MinerU -> cleanup -> fake accepted LLM -> final md
- raw PDF -> fake MinerU transient failure -> fake PPX -> cleanup -> final md
- existing md -> cleanup -> fake LLM issue -> second cleanup -> accepted
- existing output present -> skipped without LLM call

Manual smoke test for the target corpus:

```powershell
D:\codex\LLMcheck\.venv\Scripts\python.exe -m llmcheck.cli batch `
  --input "D:\pdf\output2\0" `
  --output-md-dir "D:\pdf\output2\md" `
  --process-dir "D:\pdf\output2\process" `
  --profile chinese_medicine_reference `
  --book-concurrency 1 `
  --limit 1 `
  --llm-model mimo-v2.5-pro `
  --fallback-models gpt-5.5
```

## Rollout Plan

### Phase 1: Contract And Preflight

- Add explicit output/process directory contract.
- Add preflight for MinerU and LLM service.
- Add secret masking in reports.
- Add skip-existing based on final output and manifest.

### Phase 2: Acquisition Refactor

- Introduce acquisition manifest.
- Treat existing Markdown as first-class acquisition mode.
- Convert MinerU/PPX into one formal acquisition interface.
- Implement PPX fallback as formal mode.

### Phase 3: Cleanup Loop

- Split deterministic cleanup rules into auditable rule families.
- Add loop history and max loop guard.
- Cover known blockers: forced line breaks and abnormal CJK spaces.

### Phase 4: LLM Final Review

- Add whole-document reviewer contract.
- Run a first-three-book comparison of `mimo-v2.5-pro` against `gpt-5.5`.
- Prefer `mimo-v2.5-pro` only if quality is equivalent; otherwise keep `gpt-5.5`.
- Add model availability and fallback checks.
- Route findings to deterministic cleanup, safe artifact removal, or manual review.

### Phase 5: Target Corpus Execution

- Run `D:\pdf\output2\0` by numeric order.
- Keep `--book-concurrency 1`.
- Skip existing outputs in `D:\pdf\output2\md`.
- Write final accepted Markdown only.
- Produce batch summary and per-book delivery manifests.

## Acceptance Criteria

The redesign is accepted when:

1. A dry run reports the 112 current sources and the 9 existing final Markdown outputs without reprocessing them.
2. Existing Markdown inputs bypass MinerU/PPX and still receive cleanup plus LLM final review.
3. Raw PDF/image/Office inputs use MinerU `vlm` first.
4. MinerU unavailability falls back to PPX and records the fallback.
5. The first three target books have comparable reports for `mimo-v2.5-pro` and `gpt-5.5`.
6. `mimo-v2.5-pro` becomes preferred only if the comparison shows equivalent quality.
7. If the preferred reviewer is unavailable, the pipeline falls back to `gpt-5.5`.
8. If both reviewer models are unavailable, the batch stops and asks for updated LLM service information.
9. Accepted books are written to `D:\pdf\output2\md`.
10. Reports do not copy full API keys or bearer tokens unless the operator explicitly enables verbose credential diagnostics.
11. Focused tests pass.
12. At least one target-corpus book runs end-to-end with evidence in process reports before broad execution.

## Open Questions

1. Should final PDFs still be generated in `D:\pdf\output2\pdf`, or is this redesign Markdown-only for now?
2. Should PPX fallback be automatic for authentication/quota failures, or should auth/quota failures stop the batch because they are global service configuration problems?
3. Should the current 9 delivered files be trusted as accepted, or should they be revalidated later in a separate audit mode?
