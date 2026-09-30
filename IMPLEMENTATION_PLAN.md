# Implementation Plan

## Architecture

Build a greenfield monorepo with a FastAPI backend, PostgreSQL, LangGraph orchestration, and a Next.js operator dashboard. Keep domain services independent of transport and model providers. Mock messaging and model adapters are the default so the demo runs without credentials.

- Python 3.12+; FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, LangGraph, pytest.
- PostgreSQL stores business records, LangGraph checkpoints, and durable worker jobs. A separate worker claims leased jobs with bounded retries; API handlers enqueue work and return promptly. No Redis dependency in the MVP.
- Local persistent media storage for development, behind a storage interface that can later support S3-compatible storage.
- Next.js operator UI consumes authenticated FastAPI endpoints. Buyer interaction is initially represented by mock messaging and later by a WhatsApp Cloud API adapter.
- LLM and vision calls are behind typed provider interfaces. The graph calls explicit application services; the model cannot directly manipulate the database or send messages.

## Database Schema

Use UUID primary keys, UTC timestamps, explicit status enums, and Alembic migrations. Keep commercial values nullable when unknown and attach provenance to extracted data.

- `Buyer`, `Conversation`, `Message`: WhatsApp identity, conversation channel, direction/type, external message ID, text/media reference; unique external message ID for webhook deduplication.
- `ProcurementRequest`, `RequirementVersion`: request lifecycle and immutable normalized buyer requirements; retain raw input and distinguish buyer-provided facts from inferred attributes.
- `Supplier`, `SupplierCategory`, `Catalogue`, `CatalogueItem`: supplier status/location/categories, catalogue freshness, item attributes, price/MOQ/stock where known, and source/provenance metadata.
- `RFQ`, `RFQSupplier`: normalized requirement, deadline, per-supplier delivery state, and unique idempotency key for each outbound send.
- `SupplierResponse`, `Quote`: original message/media references, nullable extracted commercial fields, confidence, validation/review state, and extraction timestamp.
- `Approval`: request, approval type, pending/approved/rejected status, actor, decision, and timestamps; graph interrupts wait on persisted approval records.
- `Job`: durable work type/payload reference, status, idempotency key, lease, attempt count, next retry, and error summary.

Do not persist whole catalogues or media contents in graph state. Seed only fictional suppliers and products.

## LangGraph State And Nodes

Use a typed state containing request/buyer/conversation IDs, current requirement-version ID, missing fields, candidate and selected supplier IDs, RFQ ID, response/quote IDs, approval reference, status, and errors. Store durable business data in PostgreSQL and use the PostgreSQL checkpointer for graph execution state.

Initial graph path:

1. `receive_request` records the inbound message and resolves the buyer.
2. `extract_requirement` validates structured text/image attributes and preserves provenance and confidence.
3. `check_missing_information` asks only for essential gaps; destination may come from the buyer profile, otherwise ask. Never default an unsupported subcategory.
4. `search_suppliers` and `rank_supplier_candidates` apply deterministic filters and transparent scoring.
5. `select_suppliers` and `human_approve_rfq` interrupt for operator action.
6. `create_rfq` and `send_supplier_rfq` persist idempotently and dispatch through the messaging interface.
7. `wait_for_supplier_responses` is represented by durable jobs/deadlines, not a long-running request or worker.
8. `parse_supplier_responses` and `validate_quotes` retain source references and flag uncertainty for review; one supplier failure must not fail the request.
9. `build_comparison` and `present_results_to_buyer` show sourced facts, unknowns, and catalogue freshness.
10. `wait_for_buyer_decision`, `human_approve_negotiation`, `confirm_supplier`, and `human_approve_order` support the explicit human-controlled decision path.

Treat buyer, supplier, and catalogue text as untrusted data in prompts. Never follow instructions embedded in supplier content. Model outputs must pass schema and business validation before persistence or action.

## API Endpoints

- `GET /health` and `GET, POST /webhooks/whatsapp` for health and inbound provider events. Webhook ingestion verifies signatures in live mode, deduplicates external IDs, enqueues work, and responds quickly.
- `GET /api/requests`, `GET /api/requests/{id}`, and `POST /api/requests/{id}/resume` for request review and authenticated workflow resumption.
- `GET, POST /api/suppliers`, `GET /api/suppliers/{id}`, `GET, POST /api/catalogues`, and catalogue upload/search endpoints.
- `GET /api/rfqs`, `GET /api/rfqs/{id}` for RFQ tracking.
- `GET /api/approvals`, `POST /api/approvals/{id}/approve`, and `POST /api/approvals/{id}/reject` for operator decisions.
- Enforce admin/operator authentication on management and resume endpoints. Keep provider webhook authentication separate from dashboard authentication.

## Implementation Phases

1. Foundation: this architecture record, package/configuration, Docker Compose, PostgreSQL, migrations, health endpoint, test setup, and minimal frontend shell.
2. Domain/catalogue: models, CRUD/search APIs, deterministic ranking, provenance/freshness rules, fictional seed script, and unit tests.
3. Intake/matching: typed requirement schemas, mock LLM/vision and messaging interfaces, graph intake/clarification/matching, and persisted request flow.
4. RFQ/responses: approvals with graph interrupt/resume, idempotent sends, mock supplier replies, quote extraction/validation, deadlines, comparison, and workflow integration tests.
5. Operator/live adapters: Next.js request/supplier/catalogue/RFQ/approval screens, auth, then optional WhatsApp and configured LLM/vision providers.
6. Hardening: restart/resume and duplicate-event tests, retry and human-review handling, structured logs/metrics, docs, and full demo acceptance.

Each phase must leave a runnable application and pass its focused tests before expanding scope.

## Assumptions

- The repository is greenfield; `AGENTS.md` is the sole existing project artifact.
- PostgreSQL is the only required stateful service for MVP; job claiming uses database transactions/leases and is safe under worker retries.
- Destination can be inferred from an existing buyer profile, not geographic guesswork. If unavailable, ask the buyer.
- Unknown suit type, fabric, price, GST, stock, MOQ, dispatch, and terms remain unknown unless supported by a source.
- The example requirement's “3-piece” subcategory is not a default because it is absent from the buyer's example message.
- Seed suppliers and catalogue records are fictional. The demo uses mock providers; live credentials are optional.
- No payments, logistics, ERP, arbitrary web scraping, separate buyer/supplier apps, autonomous financial negotiation, or autonomous order placement.

## Risks And Mitigations

- **Duplicate webhook or RFQ sends:** unique external IDs and durable idempotency records; integration tests exercise retries.
- **Graph/process restart loses progress:** PostgreSQL checkpointer, persisted approvals, durable jobs, and restart/resume tests.
- **Commercial hallucinations or stale catalogue data:** nullable fields, field-level provenance, quote validation, freshness warnings, and human review for uncertain extraction.
- **Supplier content prompt injection:** treat external content only as data; restrict graph actions to explicit validated services.
- **Background work blocks API or waits forever:** enqueue quickly, use worker leases/retries, and finalize at configurable RFQ deadlines.
- **Provider credentials unavailable:** complete and test the workflow with deterministic mock adapters before enabling live adapters.
- **Python/dependency compatibility:** lock tested dependency versions and run the test suite on Python 3.12 and the selected local interpreter before declaring compatibility.
- **Unsettled operational rules:** deployment target/storage, WhatsApp account configuration, LLM vendor/model, clarification policy, and catalogue ownership are integration-time decisions; isolate these behind configuration/interfaces.

## Acceptance Gates

- Docker Compose starts API, worker, PostgreSQL, and frontend; migrations apply and `/health` responds.
- Unit tests cover requirement validation, missing-field detection, matching/scoring, provenance, quote normalization/validation, freshness, statuses, and idempotency.
- Integration tests cover mocked text/image intake through RFQ approval, supplier responses, comparison, negotiation approval, order approval, and graph resume after worker restart.
- Failure tests cover duplicate webhooks/sends, malformed model output, supplier timeout/failure, uncertain quote fields, and deadline finalization.
- The operator can inspect source messages for quotes and approve or reject pending workflow steps; unprovided commercial facts remain null.