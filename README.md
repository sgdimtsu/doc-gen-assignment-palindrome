# doc-gen-assignment

A small config-driven pipeline that turns a client's source material into an advice report. Start
with `PROJECT_GUIDANCE.md`; it describes the exercise.

## Setup

```bash
# clone your fork, then:
cp .env.example .env          # put your OpenAI key in .env
uv sync                       # or: pip install -e .
```

## Run

One command runs extraction, advisory validation, reconciliation and report writing:

```bash
python -m agent_pipeline.generate --client client_01_clean
```

The command prints the new run directory and final `report.md` path beneath
`outputs/<client>/<run_id>/`. It uses live API calls for extraction/review and one
writer call. Intermediate extraction, validation and reconciliation files remain
in that same directory. Unresolved review issues do not block draft writing;
unusable input or a malformed API response can still cause a technical failure.

To write from an existing reconciliation without extracting again:

```bash
python -m agent_pipeline.generate --client client_03_hard --run-dir outputs/client_03_hard/38aa7eff8a754c88ad615c60179b90b9
```

If the specified run has extraction but no reconciliation, the command reconciles
it before writing. `--model` overrides extraction and writer models. Otherwise
extraction uses `EXTRACTION_MODEL`, then `OPENAI_MODEL`, then `gpt-4.1`; writing
uses `WRITER_MODEL` if set, otherwise the extraction model. Numeric stage limits
remain in extraction settings. `--data-dir` and `--output-dir` allow other folders.

Writer rules are in `config/template_config_v2.json`; it has no fixed section list.
The writer reads the selected client's `template_spec.md` for headings, order,
requirements and conditional inclusion, and `report_request.docx` for report scope
and instructions. Reconciled facts and all review concerns are supplied alongside
those documents. Each concern must have a plain-language review note in an
included section. Exact mandatory wording is preserved by code and the existing
formatter remains unchanged. The draft is explicitly labelled for human review.
The original `template_config.json` and example Markdown reports remain reference
artifacts; the generation command now uses V2.

Available clients live under `data/`:

- `client_01_clean`
- `client_02_medium`
- `client_03_hard`
- `client_04_stretch` (large and messy on purpose; a deliberate stretch)

Generated runs are ignored by Git, except for three retained examples: the final
clean report and two hard-client runs showing different extraction outcomes. The
two original Markdown reports remain as baseline references. Background experiment
files and older runs have been removed.

## Extraction agent

Install with Python 3.10 or newer and run:

```bash
python -m pip install -e ".[dev]"
python -m agent_pipeline.extract --client client_01_clean
```

The CLI accepts `--client`, `--model` and `--output-dir`. It loads `.env`;
existing environment variables take precedence. Model selection is `--model`,
`EXTRACTION_MODEL`, `OPENAI_MODEL`, then the code default `gpt-4o-mini`.
Set `EXTRACTION_MODEL=gpt-4.1` in `.env` for the stronger extraction baseline.
Edit numerical limits and reviewer settings in `extraction/models.py`.

One Agents SDK extractor uses Chat Completions and three tools defined in
`extraction/tools.py`: `list_files`, `read_source`, `validate_extraction`.
It reads `report_request.docx` and `template_spec.md` before other evidence.
Validation becomes available after all sources have been successfully read and
delivered. Prompts are in `EXTRACTION_SCHEMA.md` and `EXTRACTION_VALIDATION.md`.
Schemas are in `config/extraction_schema.json` and
`config/extraction_validation_schema.json`. SDK tracing is disabled.

Validation is advisory and has two purposes:

- Help repair extraction errors: omitted source facts, unsupported fields,
  rewritten evidence and lost qualifications.
- Preserve material missing information and ambiguity as draft review notes.

The reviewer receives account-reference and field-state definitions from the
extraction schema. It checks whether a null hides a detail stated in the same
source; field-specific feedback names the path, correction and supporting excerpt.
The extractor rereads that context before repairing. Retain/no-sale statements
do not require transaction amounts.

A source gap is not a request to invent a fact. Conflicting source assertions
remain separate; reconciliation resolves or flags them. Validation can identify
premature merging or discarded assertions, but does not choose a winning source.
The extractor has three validation submissions by default. It should resubmit
repairs while attempts remain, then return the best candidate even when concerns
remain. Rejected reviewer responses remain diagnostics, not accepted findings.

Each command prints one directory: `outputs/<client>/<run_id>/` containing:

- `summary.json`: execution state, separate validation state, errors and usage.
- `validation.json`: review attempts, findings and rejected-review diagnostics.
- `extraction.json`: structured output when execution completed.
- `partial_extraction.json`: available structured candidate if execution stopped.
- `model_response.json`: diagnostics when a structured result could not be saved.

`Extraction: complete` means execution completed, not that every fact is correct.
Validation can separately be `passed`, `failed`, `incomplete`, `not_reviewed` or
`stale`. Stale means the final candidate differs from the reviewed candidate.
No earlier successful run is substituted. Exit 1 denotes incomplete execution or
unusable output, not simply unsuccessful validation. The standalone extraction command does not write a report; use the generation
command above for the full pipeline.

## Reconciliation

Use the explicit directory printed by extraction:

```bash
python -m agent_pipeline.reconcile --run-dir outputs/client_01_clean/ACTUAL_RUN_ID
```

This deterministic command makes no API calls and writes `reconciliation.json`.
It accepts available structurally valid extraction, including partial runs, and
carries execution concerns and validation findings forward as review issues.
Validation failures, missing information and conflicts do not block a draft.
Unreadable JSON, invalid structure and broken references still produce a technical
error because there is no usable input to reconcile.

Rules match exact account IDs first, with unique owner/platform/type fallback.
Scope, action and other references without a holder field can match a unique
identified account by the stated platform and type; multiple matches and explicit
ambiguity remain unresolved. `owner_text` records a source-stated name or
joint-ownership phrase. It can distinguish individual ISAs; common first names
that still identify several accounts remain unresolved. A generic ISA label can
match a subtype only when exactly one candidate fits; GIA and General Investment
Account are treated as equivalent labels. Missing IDs alone are not ambiguity. Matching never fills IDs into the original extraction;
proposed accounts remain separate. Requests establish scope, account records
establish identity and meeting notes establish agreed actions. Valuations use
explicit dates, preserve approximate wording, and remain unresolved when dates or
values conflict. No holdings totals, allocation calculations, tax calculations,
projection keyword heuristics or generic policy interpreter are included.

Output retains report facts, section tags, source wording, uncertainty and small
observation-ID references. Secondary instructions and unresolved issues remain
review notes. Source gaps and ambiguous facts are preserved; no replacement value
is invented. Validation concerns are labelled as concerns, not proven source facts.
A stale review produces one notice, with earlier findings retained in validation.json
rather than repeated as current errors. Duplicate code/model findings for a current
candidate are shown once. Missing fee requirements remain explicit review gaps. The writer renders review notes beside affected content without adding a new
section to the client specification.

## Tests

```bash
python -m pytest -q
```

Tests use synthetic evidence and fake API responses; they do not spend API credits.
They cover repairs, missing/ambiguous information, failed or incomplete reviews,
retry limits, advisory continuation and reconciliation conflict handling. They
check implementation behaviour, not real-model accuracy. For a live clean-case
check, run the extraction command above and inspect its run directory.

## How it fits together

```text
client source files → extraction + advisory validation → reconciliation
                                                              ↓
client template_spec.md + report_request.docx + V2 rules → writer
                                                              ↓
                                                     run/report.md
```

Account references now require `owner_text` (string or null). Existing extraction
examples remain historical artifacts; rerun extraction to produce the updated
contract. Do not add guessed ownership or IDs to old results.

## Report evaluation

Run the manually authored clean-client checklist against a generated report:

```bash
python -m agent_pipeline.evaluate --report outputs/client_01_clean/71518aa17e0745d7b387ce7487afd362/report.md --checks evaluation/client_01_clean.json
```

It checks selected request details, scope and presentation regressions. These
text checks are a starting point, not proof of correct advice or complete
extraction. Compare facts, qualifications and outstanding concerns against the
original sources manually; the extraction reviewer does not evaluate final prose.
The retained report predates the latest writer changes and may fail these checks.

Every new extraction also saves `final_checks` in `summary.json`. These code
checks run on the final candidate without another model call. Reconciliation
retains their findings even when the earlier LLM review is stale. Validation
remains advisory. Older saved runs without final checks remain readable.
