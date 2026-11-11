# Run extraction

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
