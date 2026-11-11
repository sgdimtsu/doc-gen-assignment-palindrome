# Run the pipeline and evaluation

## Setup

From the repository root:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

If `.env` does not already exist:

```bash
cp .env.example .env
```

Set these values in `.env`:

```dotenv
OPENAI_API_KEY=your-own-key
EXTRACTION_MODEL=gpt-4.1
```

## Extract

```bash
python -m agent_pipeline.extract --client client_01_clean
```

Replace `client_01_clean` with `client_02_medium`, `client_03_hard` or
`client_04_stretch` as needed.

Optional model override:

```bash
python -m agent_pipeline.extract --client client_01_clean --model gpt-4.1
```

Find the extraction in the run directory printed by the command:

```text
outputs/<client>/<run_id>/extraction.json
```

## Generate a report

```bash
python -m agent_pipeline.generate --client client_01_clean
```

The command prints the report path:

```text
outputs/<client>/<run_id>/report.md
```

## Evaluate a saved report

Use the matching client and the run directory printed by generation. Replace
`YOUR_RUN_ID` with the actual directory name:

```bash
python -m agent_pipeline.evaluate \
  --client client_01_clean \
  --run-dir outputs/client_01_clean/YOUR_RUN_ID
```

Evaluation checks the saved report without regenerating it. It uses code checks
and one paid LLM judge call against the original client sources. Keep the source
files unchanged between generation and evaluation.

Optional model override:

```bash
python -m agent_pipeline.evaluate \
  --client client_01_clean \
  --run-dir outputs/client_01_clean/YOUR_RUN_ID \
  --model gpt-4.1
```

Otherwise, evaluation uses `EVALUATION_MODEL` from the environment or `.env`,
falling back to `gpt-4.1`.

Results are saved to:

```text
outputs/<client>/<run_id>/evaluation.json
```

| Outcome | Meaning | Exit code |
|---|---|---|
| `pass` | All checks meet the evaluator's standard | 0 |
| `needs_review` | Quality concerns, uncertainty or incomplete evaluation | 1 |
| `fail` | A required code check or an assessed critical/requirement finding failed | 2 |

Read `code_checks`, `judgment.outcome_checks`, `judgment.criteria` and
`evaluation_errors` in the result file. Each expected outcome is assessed
separately. Explanations and source references are advisory; they are not checked
for exact quotation matches. An incomplete API response cannot produce a pass. Rerunning
evaluation overwrites `evaluation.json` in that directory.

The shared rubric is `evaluation/judge_prompt.md`; source-based client expectations
are in `evaluation/expectations.json`. These expectations are used only for
standalone evaluation, not production extraction or review.

No human-review/calibration step is included. An automated pass is not approval
to send financial advice to a customer.
