# LLMcheck Domain-Neutral Document Upgrade Design

Date: 2026-06-01

## Goal

Upgrade LLMcheck from a Chinese-medicine-specific cleanup pipeline into a domain-neutral document conversion, cleanup, acceptance, and standard-document delivery system.

The upgraded system should:

- Accept Markdown, PDF, image, and Office inputs across domains.
- Convert and clean source material into readable, logically structured Markdown.
- Produce delivery-grade Markdown and text PDF files.
- Avoid final outputs that still contain mojibake, OCR garbage, abnormal spaces, forced physical line breaks, broken paragraphs, incoherent section order, or obviously illogical text flow.
- Keep domain rules configurable instead of hard-coded into prompts or deterministic cleanup.

## Current State

Relevant current files:

- `llmcheck/llm.py`: correction, acceptance, and repair prompt builders.
- `llmcheck/quality.py`: deterministic cleanup and quality checks.
- `llmcheck/pipeline.py`: process orchestration, reports, draft/final output.
- `llmcheck/preprocess.py`: input discovery, MinerU/PPX conversion.
- `README.md` and `skill/SKILL.md`: user-facing workflow and agent workflow.

Current domain coupling:

- `llm.py` tells the model it is a "中医 Markdown 文本" correction/acceptance worker.
- prompt rules reference medical cases, prescriptions, doses, diagnoses, and medical substance.
- `quality.py` includes local structure glue rules based on body-region labels.
- README and skill docs describe the project primarily as Chinese medicine source-book tooling.

Current strengths to preserve:

- Clear separation between preprocessing, LLM correction, LLM acceptance, local repair, PDF delivery, and reports.
- JSON-only LLM contracts.
- Chunked/concurrent correction and acceptance.
- Acceptance-before-final-output.
- Process output isolation under `process/`.
- Existing GUI, CLI, and agent skill packaging.

## Recommended Approach

Use a profile-driven document pipeline.

Instead of one hard-coded "Chinese medicine" prompt, introduce a `DocumentProfile` that defines:

- domain label: general, academic, legal, technical, medical, finance, government, training material, archive, etc.
- language and script hints.
- preservation rules: what must not be changed.
- structure expectations: headings, lists, tables, footnotes, code blocks, formulas, citations.
- forbidden transformations: summarization, modernization, inferred facts, legal/medical interpretation.
- acceptance rubric: human readability, structural coherence, OCR cleanup, formatting integrity.
- optional glossary or protected terms.
- optional domain-specific glue split rules.

The default profile should be `general_standard_document`, not medical. Chinese medicine becomes one optional profile, not the base identity of the product.

## Alternative Approaches Considered

### Option A: Prompt-only Generalization

Replace "中医" wording in prompts with generic "document" wording and add stricter acceptance text.

Pros:

- Fastest implementation.
- Low risk to current architecture.

Cons:

- Domain-specific deterministic rules remain mixed into generic cleanup.
- Future domains will keep adding prompt branches.
- Harder to test and reason about because behavior is still implicit.

### Option B: Profile-Driven Pipeline

Add document profiles and route prompt generation, deterministic rules, and acceptance criteria through a profile object.

Pros:

- Cleanly removes hard-coded domain assumptions.
- Makes future domains explicit and testable.
- Keeps current pipeline structure.
- Lets GUI/CLI expose domain presets without changing core logic each time.

Cons:

- Requires more files and schema tests.
- Needs careful default behavior so existing users are not surprised.

### Option C: Full Document Compiler

Build a multi-stage compiler with layout graph extraction, section planning, whole-document semantic modeling, and final rendering.

Pros:

- Best long-term quality ceiling.
- Strongest protection against incoherent structure.

Cons:

- Too large for the next practical upgrade.
- Harder to ship safely without a benchmark corpus.

Recommendation: implement Option B now, and design it so selected pieces of Option C can be added later.

## Target Architecture

### 1. Document Profiles

Create `llmcheck/profiles.py`.

Core types:

```python
@dataclass(frozen=True)
class DocumentProfile:
    id: str
    label: str
    description: str
    language_hint: str
    preservation_rules: tuple[str, ...]
    structure_rules: tuple[str, ...]
    cleanup_rules: tuple[str, ...]
    forbidden_changes: tuple[str, ...]
    acceptance_checks: tuple[str, ...]
    protected_terms: tuple[str, ...] = ()
    glue_markers: tuple[str, ...] = ()
```

Built-in profiles:

- `general_standard_document`: default. For books, reports, manuals, scanned archives, teaching materials, policy docs, and ordinary long-form documents.
- `academic_paper`: citations, abstracts, references, formula/table preservation.
- `technical_manual`: commands, code blocks, numbered procedures, warnings.
- `legal_contract`: clauses, numbering, party names, dates, amounts, obligations.
- `financial_report`: tables, units, currency, periods, figures.
- `medical_reference`: clinical terms, dosages, case records. This can cover the existing Chinese medicine use case without making the whole product medical.

CLI/GUI should accept `--profile`, defaulting to `general_standard_document`.

### 2. Prompt Builder Refactor

Keep `llmcheck/llm.py` as the LLM client module, but move prompt composition into `llmcheck/prompts.py` or a focused prompt section.

Correction prompt should become:

- Role: "document cleanup and structural normalization editor."
- Task: return complete corrected text for the current chunk.
- Prohibit summary/diff-only output.
- Prohibit invention, domain interpretation, and unsupported facts.
- Require human-readable paragraphing.
- Require removal of physical line breaks inside ordinary paragraphs.
- Preserve headings, lists, tables, citations, code, formulas, and page evidence.
- Use profile-specific preservation rules.

Acceptance prompt should become stricter:

- classify issues by category and severity.
- reject mojibake, repeated replacement characters, abnormal spacing, physical line breaks, broken tables, inconsistent heading levels, and incoherent paragraph flow.
- reject text that is readable sentence-by-sentence but has impossible section transitions caused by bad merge/splitting.
- require a short "why deliverable / why not deliverable" verdict.

Repair prompt should become issue-targeted:

- receive the acceptance issue.
- receive neighboring chunk excerpts and audit text.
- return only the repaired current chunk.
- preserve source evidence and avoid rewriting content beyond the issue.

### 3. Deterministic Quality Layers

Split current `quality.py` responsibilities:

- `normalization.py`: BOM, control chars, Unicode normalization, whitespace cleanup, line ending normalization.
- `structure.py`: heading/list/table/code-block detection and protected block segmentation.
- `quality.py`: report quality errors and hints.
- `local_repair.py`: deterministic targeted repair.

Initial implementation can keep one file internally if needed, but the design boundary should be explicit.

Add quality checks for:

- mojibake patterns: `锟斤拷`, `�`, suspicious Latin-1/GBK artifacts.
- replacement-character density.
- abnormal whitespace: repeated spaces inside CJK text, tab leakage, zero-width chars.
- physical line breaks inside normal paragraphs.
- isolated punctuation lines.
- table header/body mismatch.
- heading level jumps that likely come from OCR artifacts.
- paragraphs that are too long because multiple sections were glued.
- paragraphs that are too short in sequence because a sentence was shattered.
- duplicate repeated lines from OCR.
- cross-page header/footer remnants.
- dangling list/table/code structures.

Quality reports should separate:

- `blocking_errors`: cannot deliver.
- `repairable_errors`: can retry local or LLM repair.
- `warnings`: should be visible but not necessarily blocking.

### 4. Standard Document Finalization

Add a finalization stage after chunk acceptance:

```text
preprocess -> deterministic clean -> chunk correction -> chunk acceptance/repair
-> merge accepted chunks -> whole-document finalization -> whole-document acceptance
-> md + text PDF
```

The current pipeline mostly validates chunks. The upgrade should add a whole-document pass because many failures only appear after merge:

- duplicated headings.
- broken section order.
- inconsistent title hierarchy.
- missing blank lines around headings/tables.
- chunk boundary paragraph splits.
- repeated page headers/footers.
- illogical transitions created by merge order or segmentation.

Whole-document finalization should be conservative:

- normalize heading spacing.
- join chunk-boundary paragraph fragments when safe.
- remove duplicated running headers/footers.
- normalize table spacing.
- create a final `process/reports/*.finalization.json`.

Whole-document acceptance should be required before writing `md/` and `文字版pdf/`.

### 5. Human Reading Habit Rules

Define a concrete "human-readable standard document" rubric:

- A heading introduces the following content and is separated by blank lines.
- A normal paragraph is a coherent unit, not one OCR line per visual row.
- Lists stay lists; procedure steps preserve numbering.
- Tables remain valid Markdown tables or are converted into readable plain text when table reconstruction fails.
- Page numbers and source markers are preserved only when useful; repeated page headers/footers are removed or moved to provenance.
- Code, formulas, citations, monetary values, dates, names, and legal/technical identifiers are protected.
- Uncertain content is preserved and reported, not silently guessed.
- The final document can be read continuously without visible conversion artifacts.

### 6. CLI and GUI Changes

CLI:

```bash
llmcheck run --profile general_standard_document ...
llmcheck batch --profile technical_manual ...
llmcheck profiles
```

GUI:

- Add a profile selector.
- Show profile description and preservation rules.
- Keep advanced settings collapsed.
- Show final acceptance result separately from chunk acceptance.
- Surface blocking errors grouped by category.

Default:

- `general_standard_document`

Backward compatibility:

- Existing command lines still work.
- Chinese medicine behavior can be selected through `--profile medical_reference` or `--profile chinese_medicine_reference` if we keep that name as an alias.

### 7. Reports and Artifacts

Add or extend reports:

- `process/reports/<doc>.profile.json`: selected profile and profile version.
- `process/reports/<doc>.quality.json`: deterministic quality checks before correction.
- `process/reports/<doc>.llm_correction.json`: current correction report.
- `process/reports/<doc>.llm_acceptance.json`: current chunk acceptance report.
- `process/reports/<doc>.finalization.json`: whole-document finalization actions.
- `process/reports/<doc>.final_acceptance.json`: whole-document acceptance.
- `process/reports/<doc>.delivery_manifest.json`: final md/pdf paths, hashes, profile id, acceptance status.

The final `md/` and `文字版pdf/` directories should only receive documents with passed final acceptance.

### 8. Testing Strategy

Add test fixtures across domains:

- general scanned book with forced line breaks.
- technical manual with code blocks and numbered steps.
- legal/contract sample with clauses and dates.
- academic paper sample with references and tables.
- finance/report sample with figures and units.
- Chinese medicine sample as a profile-specific regression, not the default.

Test categories:

- prompt generation includes profile rules and excludes hard-coded medical identity under default profile.
- deterministic cleanup removes mojibake/abnormal spaces/forced line breaks.
- protected blocks are not damaged.
- acceptance blocks final output when gibberish or bad physical line breaks remain.
- local repair handles issue-targeted excerpts.
- whole-document finalization repairs chunk-boundary artifacts.
- final delivery is withheld when final acceptance fails.
- GUI exposes profile selector.
- skill docs describe domain-neutral use.

## Suggested Optimization Backlog

1. Profile presets and aliases

   Let users choose `general`, `academic`, `technical`, `legal`, `finance`, `medical`, and project-specific custom profiles.

2. Auto profile suggestion

   Inspect file name, headings, table density, code blocks, and vocabulary to suggest a profile, but require explicit confirmation for high-stakes profiles.

3. Protected span detection

   Detect and freeze code blocks, formulas, citations, table cells, dates, amounts, IDs, URLs, and page references before LLM correction.

4. Chunk overlap and boundary repair

   Add configurable overlap snippets and a merge-time boundary repair pass to reduce paragraph splits and repeated headings.

5. Whole-document final acceptance

   Promote acceptance from chunk-level only to chunk-level plus whole-document-level.

6. Quality score dashboard

   Report readability, OCR residue, structure integrity, table integrity, and uncertainty scores in GUI.

7. Golden corpus regression suite

   Maintain small representative fixtures for every supported profile and assert no regression in final Markdown.

8. Diffable correction reports

   Store concise before/after excerpts and reasons for every meaningful modification.

9. Source anchoring

   Keep page/chunk provenance so uncertain or high-impact edits can be traced to source segments.

10. Safer PDF text rendering

   Validate generated text PDFs for extractable text, page count, and non-empty content.

11. Custom profile files

   Allow `--profile-file profile.json` so teams can define domain rules without code changes.

12. Retry policy by issue type

   Different categories should trigger different repairs: local whitespace repair, table repair, LLM repair, or manual review.

13. Prompt versioning

   Include prompt version and profile version in every report so output can be reproduced and audited.

14. Reviewer mode

   GUI can show final Markdown, quality errors, and source/audit excerpts side-by-side for manual approval.

15. Packaging automation

   Add a release script that runs tests, builds the GUI exe, repacks the skill, verifies hashes, and updates README artifact metadata.

## Proposed Implementation Phases

### Phase 1: Domain Neutralization

- Add `DocumentProfile`.
- Refactor prompts to accept profile.
- Replace medical default with `general_standard_document`.
- Keep medical behavior as optional profile.
- Update README and skill docs.

### Phase 2: Quality Gates

- Add deterministic checks for mojibake, abnormal whitespace, forced line breaks, duplicate OCR lines, and malformed structures.
- Make blocking quality errors prevent final delivery.
- Expand local repair categories.

### Phase 3: Whole-Document Finalization

- Merge chunks through a finalizer.
- Add whole-document acceptance.
- Add finalization and final acceptance reports.

### Phase 4: GUI and Release Polish

- Add profile selector to GUI.
- Add quality dashboard fields.
- Add release script for exe and skill artifacts.
- Rebuild and publish both delivery versions.

## Open Questions

1. Should the first upgraded release support only built-in profiles, or also custom `profile.json` files?
2. Should final whole-document acceptance call the LLM by default, or should it be optional for cost control?
3. Which non-medical domains should be included in the first golden fixture set?
4. Do we keep the current package name `LLMcheck`, or rename the user-facing product to something broader later?

## Success Criteria

- Default prompts and docs no longer identify the tool as Chinese-medicine-specific.
- Chinese medicine remains supported through a profile, not hard-coded as the base behavior.
- Final delivery is blocked if deterministic or LLM acceptance finds mojibake, abnormal spaces, forced line breaks, broken structure, or incoherent document flow.
- GUI/CLI expose profile selection.
- Reports record profile id, prompt/profile versions, finalization actions, and final acceptance result.
- Tests cover at least three non-medical document types and one medical regression profile.
- Both delivery artifacts are rebuilt after implementation: GUI exe and skill tarball.
