# P0 Agent-Callable Pipeline Glue — 2026-07-18

## What P0 is

Make LLMcheck callable by other agents:

upload multi-format docs → dual engine (MinerU API ∥ local PPX) → cross select → clean/structure/gates → deliver Markdown only when `passed`.

Design: `docs/prd/2026-07-18-agent-callable-dual-engine-pipeline.md`

## This slice (pipeline report + docs)

Owned here:

- `llmcheck/pipeline.py` report schema: `schema_version`, `job_id`, `artifacts`
- optional `DocumentResult.cross_report_path` when preprocess tree has `cross/cross_report.json`
- README agent-callable section + intended `llmcheck agent ...` surface
- this progress note

Not owned here (sibling agents):

- `cross.py` winner selection
- `agent_api.py` / CLI agent subcommands implementation
- skill rewrite

## Archive

Obsolete prior designs are mapped to `old/` in the design doc (cleaning redesign, mineru-first redesign, superpowers plans/specs, `changes/`). Do not move more files in this slice.

## Verification

Pending full suite. Focused check for this slice:

```bash
.venv/bin/python -m pytest tests/test_pipeline_profiles.py tests/test_interfaces_profiles.py -q
```

Full suite and agent API / cross unit tests remain for sibling work + integration.
