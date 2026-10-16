# Textile Procurement Agent

An MVP for human-assisted supplier discovery, RFQs, and quote comparison for Indian ladies-suit retailers.

## Current status

The current implementation includes supplier and catalogue APIs, deterministic catalogue matching, WhatsApp webhook verification and deduplication, a resumable LangGraph intake-to-RFQ flow, persisted operator approvals, and a PostgreSQL-backed worker queue. The worker uses mock model/messaging providers by default. The operator dashboard and live provider adapters are not implemented yet.

## Local development

Requirements: Python 3.12+ and `uv`. Docker Compose is needed to run PostgreSQL in a container.

Requirements: Python 3.12+ and `uv`. Docker Compose is needed for the PostgreSQL-backed API and worker. Copy `.env.example` to `.env` and set a local `OPERATOR_API_TOKEN` before starting Docker Compose. Protected management endpoints require this value in the `X-Operator-Token` header.

```powershell
uv sync --group dev
uv run pytest
```

For a local API-only smoke run, start Uvicorn after setting `DATABASE_URL` to a reachable PostgreSQL instance and applying migrations with `uv run alembic upgrade head`:

```powershell
uv run uvicorn app.main:app --reload
```

Start PostgreSQL, apply migrations, and run the API and worker with Docker Compose:

```powershell
docker compose up --build
```

The API health endpoint is available at `http://localhost:8000/health`. Compose runs the migration service before the API and worker.

The inbound webhook accepts a normalized message payload at `POST /webhooks/whatsapp`. Configure `WHATSAPP_APP_SECRET` to enable HMAC signature validation and `WHATSAPP_VERIFY_TOKEN` for the provider verification challenge. With no external provider configured, local development uses mock graph extraction and supplier messaging. Do not commit real credentials.

## Additional architecture documentation

For a deeper code-level walkthrough of the workflow, storage model, queueing system, and LangGraph orchestration, see:

- [docs/complete-workflow-architecture.md](docs/complete-workflow-architecture.md)
- [docs/architecture.md](docs/architecture.md)
- [docs/langgraph-workflow.md](docs/langgraph-workflow.md)
- [docs/database.md](docs/database.md)
- [docs/local-development.md](docs/local-development.md)
- [docs/whatsapp-setup.md](docs/whatsapp-setup.md)