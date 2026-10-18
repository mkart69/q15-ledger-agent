# Q15 Ledger Agent

A small FastAPI service for the ledger-agent challenge.

## Render configuration
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Environment variable: `LEDGER_URL` = the full personalized `questionData` URL for `questionId=q-ledger-agent-server` (keep this private; do not commit it).

The app fetches ledger metadata, then the export and exchange-rate links on startup. It exposes `POST /answer` accepting `{"question":"..."}`.
