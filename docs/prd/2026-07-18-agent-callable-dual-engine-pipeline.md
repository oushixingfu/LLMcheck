# Agent-Callable Dual-Engine Pipeline (P0)

Date: 2026-07-18  
Status: approved for implementation  
Scope: P0 only (cross select + gates + agent contract / SDK / CLI / skill)

## Goal

Make LLMcheck a process that other agents can call to:

**upload multi-format documents → dual-engine acquisition (MinerU API + local PPX) → cross-select initial MD → deterministic clean / structure / gates → deliver only passed Markdown**

## Decisions (locked)

1. Call surfaces: Skill + CLI/SDK first, then MCP (later)
2. Deploy: same-machine first, reserve remote HTTP
3. Delivery: only `status=passed` Markdown is agent-readable
4. Engines: MinerU API + local PPX only (no Docling/Marker in P0)
5. Cross means deterministic winner selection, not sentence fusion
6. Obsolete prior designs moved under `old/`

## Canonical stages

```text
0 Intake
1 Dual Acquisition (MinerU API ∥ local PPX)
2 Cross Select → process/preprocess/<doc>/cross/initial.md
3 Deterministic Clean
4 Structure Normalize
5 Pre-LLM Gate
6 Optional LLM (local-gate default for agent/local quality work)
7 Final Gate (cannot bypass)
8 Deliver md/ (+ optional pdf/) only when accepted
```

## Cross Select contract

Artifacts:

```text
process/preprocess/<doc>/
  mineru/mineru_vlm.md
  ppx/clean/ppx.md
  cross/
    cross_report.json
    initial.md
```

Modes:

- `selected_mineru` / `selected_ppx` when both candidates exist
- `mineru_only` / `ppx_fallback` when one side is missing
- `existing_md` for direct Markdown inputs
- `failed` when no usable candidate

Scoring (deterministic):

- empty / low readable chars → strong penalty
- mojibake / replacement chars / control chars → strong penalty
- readable char count / punctuation density → positive
- forced line breaks / repeated short lines → negative
- near-tie prefers MinerU

No silent merge of both texts in v1.

## Agent job report (`schema_version=1.0`)

Minimum fields:

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

Rules:

- Only `passed` rows may expose final Markdown content via agent APIs.
- `process/drafts` is never a delivery artifact.
- CLI exit: `0=passed`, `1=review/fail`, `2=config/dependency error` (agent subcommands).

## P0 public surfaces

### Python

`llmcheck.agent_api`:

- `list_profiles()`
- `submit_convert(...)` → job report (sync wrapper over existing pipeline for P0)
- `get_job(output_dir|report)`
- `get_final_markdown(document_id|path, *, max_chars=None)` → only if passed

### CLI

- `llmcheck agent convert --input ... --output-dir ...`
- `llmcheck agent status --output-dir ...`
- `llmcheck agent get-md --output-dir ... --document-id ...`
- `llmcheck agent profiles`

### Skill

Rewrite `skill/SKILL.md` to the agent contract above. Credentials via env vars.

## Non-goals for P0

- FastMCP server
- Remote HTTP `/v1`
- Replacing MinerU/PPX
- Sentence-level dual-engine fusion
- Multi-tenant auth / SaaS

## Verification

- Unit tests for cross scoring and winner selection
- Agent API tests: non-passed must not return markdown body
- Existing pipeline tests still pass or are updated for `schema_version` / cross paths
- Focused pytest run for new modules

## Archive map

Moved to `old/`:

- `docs/prd/2026-06-05-cleaning-and-llm-review-redesign.md`
- `docs/prd/2026-06-10-mineru-first-document-pipeline-redesign.md`
- `docs/superpowers/plans/`
- `docs/superpowers/specs/`
- `changes/`
