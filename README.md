# Ledger Agent — TDS GA1

FastAPI service for answering natural-language questions about the Acme Appliances order ledger.

## Before running

The app requires a private signed assignment discovery URL in the environment variable `LEDGER_API_URL`. **Do not commit that URL, response JSON, or the full order export to this public repository.**

The service downloads the ledger export and exchange rates at startup. It reads the export as NDJSON (one JSON object per line), keeps the latest `updated_at` row for each order ID, uses Asia/Kolkata dates, and converts amounts to USD using the rates endpoint. Only `paid` rows are counted as revenue.

## Run locally

Install dependencies:

```bash
uv sync
```

Create a local `.env` file (it is ignored by Git) and set:

```dotenv
LEDGER_API_URL=PASTE_YOUR_PRIVATE_SIGNED_DISCOVERY_URL_HERE
```

Then run:

```bash
uv run --env-file .env uvicorn main:app --reload --port 8000
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

Test a question:

```bash
curl -X POST http://127.0.0.1:8000/ \
  -H 'Content-Type: application/json' \
  -d '{"question":"What was the revenue in the North region in March 2026?"}'
```

The expected response shape is `{"answer": ...}`.

## Deploy

This repository includes `render.yaml` for Render. Create a Web Service from this repository, then add `LEDGER_API_URL` in the Render environment settings. Keep the value private. Once deployed, visit `/health`; it should show `status: ready` and a positive `orders_loaded` value.

## Important limitation

The current `main.py` uses a basic deterministic phrase parser; it does **not yet use an LLM** to turn arbitrary English questions into a structured query. Treat it as a starter implementation, not a verified final submission. Test against the assignment's required question types and add the required LLM/query-planning step before submitting if the prompt explicitly requires one.
