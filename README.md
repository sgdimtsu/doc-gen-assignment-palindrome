# Updated pipeline

One extraction agent reads the client sources, supported structured facts are
preserved by code, and deterministic reconciliation prepares the information for
an LLM writer. The writer produces a draft using the client’s report specification.
Missing information and unresolved concerns remain visible for review.

## Setup

From the repository root, using Python 3.11:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

If `.env` does not already exist, copy `.env.example` to `.env`. Set:

```dotenv
OPENAI_API_KEY=your-own-key
EXTRACTION_MODEL=gpt-4.1
```

Generation and evaluation use paid API calls. Do not commit `.env`.

## Generate a report

```bash
python -m agent_pipeline.generate --client client_01_clean
```

Replace the client name as needed. The command prints the new run directory;
the report is `outputs/<client>/<run_id>/report.md`.

## Run extraction only

```bash
python -m agent_pipeline.extract --client client_01_clean
```

## Evaluate a saved report

Replace `YOUR_RUN_ID` with the directory printed by generation, using the same client:

```bash
python -m agent_pipeline.evaluation.evaluate \
  --client client_01_clean \
  --run-dir outputs/client_01_clean/YOUR_RUN_ID
```

Results are saved as `evaluation.json` in that run directory.

## Run tests

```bash
python -m pytest -q
```
