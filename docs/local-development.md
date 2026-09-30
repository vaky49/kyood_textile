# Local Development

## Requirements

- Python 3.12+ and `uv`
- Node.js 24+ and npm
- Docker Compose for PostgreSQL and the full local stack

## Start

1. Copy `.env.example` to `.env` and set `OPERATOR_API_TOKEN`.
2. Run `docker compose up --build`.
3. Open the operator console at `http://localhost:3000`; API docs are at `http://localhost:8000/docs`.
4. Seed fictional demo data with `uv run python -m app.seed`.

Compose starts PostgreSQL, applies Alembic migrations, then starts API, worker, and frontend. The API and worker share the `media_data` volume.

## Tests And Static Checks

```powershell
uv run pytest
uv run ruff check app tests migrations
uv lock --check
Push-Location frontend
npm run lint
npx tsc --noEmit
npm run build
Pop-Location
```

The unit/API/integration suite uses async SQLite and mocked external providers. It exercises schema, idempotency, matching, workflow interruptions/resume, message parsing, source provenance, and the mock procurement demo. Run provider contract tests only when credentials are configured.

## API-Only

Set `DATABASE_URL` to a reachable PostgreSQL database, run `uv run alembic upgrade head`, then `uv run uvicorn app.main:app --reload`. Start the console separately from `frontend/` with `npm run dev`.