# Complete Workflow and Architectural Description

This document gives a code-level view of the textile procurement agent. It follows the working modules in this repository and explains how requests flow from WhatsApp to database persistence, supplier matching, RFQ approval, and resumable graph execution.

## 1. Product scope and repository structure

The repository implements the MVP described in the business brief:

- supplier discovery and ranking for ladies-suit procurement,
- human-assisted RFQ approval,
- durable graph execution using LangGraph,
- PostgreSQL-backed persistence,
- mock buyer and supplier messaging adapters for local demo operation,
- admin APIs for request, supplier, catalogue, RFQ, and approval review.

The main code areas are:

- [app/main.py](../app/main.py): FastAPI application bootstrapping and router registration.
- [app/api](../app/api): API routes for customer-facing webhook and operator APIs.
- [app/agent](../app/agent): LangGraph orchestration and extraction logic.
- [app/domain](../app/domain): domain models, requirements, matching, statuses, and quote logic.
- [app/infrastructure](../app/infrastructure): DB/session, jobs, storage, messaging, and provider adapters.
- [app/workers](../app/workers): durable worker loop for background jobs.
- [app/seed.py](../app/seed.py): seed data generation for suppliers, buyers, and sample catalogues.
- [frontend](../frontend): admin dashboard shell.

## 2. Runtime architecture

The backend is structured around a few clear layers:

1. Transport layer
   - FastAPI app handles webhooks and authenticated management endpoints.
   - WhatsApp API traffic is normalized and stored before the graph operates.

2. Orchestration layer
   - LangGraph state machine decides workflow progression and pauses at human approval points.
   - The graph is executed through PostgreSQL-backed checkpoints and durable jobs.

3. Domain layer
   - requirement parsing, match scoring, quote validation, and status definitions live here.

4. Infrastructure layer
   - database sessions, idempotent jobs, file storage, and messaging providers live here.

5. Worker layer
   - background jobs claim work from PostgreSQL and call the domain/graph processor.

This keeps business logic independent from the specific communication provider or LLM implementation.

## 3. API and entry points

The service entry point is [app/main.py](../app/main.py). It creates the FastAPI app and registers routers:

- suppliers,
- catalogues,
- approvals,
- requests,
- RFQs,
- media,
- WhatsApp webhook.

The app also configures CORS using the environment setting in [app/settings.py](../app/settings.py). The operator auth token is read from environment and enforced by [app/api/auth.py](../app/api/auth.py).

### 3.1 WhatsApp webhook route

The webhook flow is defined in [app/api/routes/whatsapp.py](../app/api/routes/whatsapp.py).

Important behavior:

- `verify_webhook` handles the WhatsApp verification challenge and validates the token.
- `receive_webhook` validates an HMAC signature when `WHATSAPP_APP_SECRET` is configured.
- It parses inbound payloads from the WhatsApp provider, normalizes them into `IncomingWhatsAppMessage`, and rejects malformed payloads.
- It deduplicates incoming messages using the unique external message ID.
- It creates or resolves a buyer record and persists the message into the database.
- It enqueues a background job instead of running the graph inline.

This is essential for idempotency and safe retry behavior.

### 3.2 Operator management APIs

Management endpoints are implemented under [app/api/routes](../app/api/routes):

- [requests.py](../app/api/routes/requests.py): list and inspect request records, plus latest requirement versions and approvals.
- [approvals.py](../app/api/routes/approvals.py): review and resolve pending approvals such as supplier selection and RFQ approval.
- [suppliers.py](../app/api/routes/suppliers.py): manage suppliers and their metadata.
- [catalogues.py](../app/api/routes/catalogues.py): catalogue CRUD and ingestion-related endpoints.
- [rfqs.py](../app/api/routes/rfqs.py): RFQ details and status view.
- [media.py](../app/api/routes/media.py): media-related retrieval for uploaded or provider-contained assets.

The request detail view returns a compact but useful summary containing:

- raw buyer message,
- prior requirement versions,
- approvals,
- linked RFQs,
- associated messages and metadata.

## 4. Domain and persistence model

The central persistence schema is defined in [app/domain/models.py](../app/domain/models.py). The model set includes:

### 4.1 Buyer and conversation tracking

- `Buyer`: holds buyer identity, WhatsApp number, city/state, preferred categories, and request counters.
- `Conversation`: links a buyer or supplier to an external provider conversation ID.
- `Message`: stores text or media payloads, sender type, direction, message type, external IDs, and media object references.

This supports deduplication and later multi-message conversation state.

### 4.2 Request and requirement versioning

- `ProcurementRequest`: the top-level request record tied to a buyer and conversation.
- `RequirementVersion`: stores immutable requirement snapshots, including provenance metadata.

This satisfies the requirement to version buyer changes without overwriting the original requirement.

### 4.3 Supplier, catalogue, and inventory data

- `Supplier`: business record with contact data, city/state, market, supplier type, verification flags, and response rate.
- `SupplierCategory`: supplier category/subcategory mapping.
- `Catalogue`: metadata for a supplier catalogue, including source type, freshness, and status.
- `CatalogueItem`: structured catalogue product records containing category, fabric, price, MOQ, stock, image URL, and provenance.

This is directly aligned with the intended catalogue search and ranking pipeline.

### 4.4 RFQ, response, quote, approval, and job tracking

- `RFQ`: request-scope RFQ record with snapshot and idempotency key.
- `RFQSupplier`: supplier-level RFQ send state.
- `SupplierResponse`: raw supplier response plus parser output and confidence.
- `Quote`: normalized extracted commercial data such as price, MOQ, GST, dispatch days, and review state.
- `Approval`: graph interruption record for supplier selection, RFQ approval, negotiation approval, and order confirmation.
- `Job`: background-work queue record with retry and lease metadata.
- `OutboundDelivery`: track sent messages or outbound supplier communications.

This gives the system durable control over retries, review, and restart recovery.

## 5. Requirement model and extraction

The requirement schema is implemented in [app/domain/requirements.py](../app/domain/requirements.py).

Core logic:

- `Requirement` is a strict Pydantic model with a valid price range and required fields.
- `AttributeSource` distinguishes buyer, buyer-profile, model-inference, image-inference, supplier, and supplier-catalogue sources.
- `AttributeProvenance` records each extracted field's source and confidence.
- `with_buyer_profile_destination` fills in a delivery city/state from buyer profile data only when appropriate.
- `find_missing_fields` is the gate for clarification.

This is the main place where the system avoids silently inventing commercial facts.

### 5.1 Rule-based extraction

The current MVP uses rule-based extraction in [app/agent/extraction.py](../app/agent/extraction.py), not a live LLM. It recognizes patterns for:

- product category (`suit`, `salwar suit`, `dress material`),
- quantity (`100 suits`, `10 pcs`),
- fabric (`cotton`, `silk`, etc.),
- price ranges such as `₹500-600`,
- delivery city through a phrase like `delivery to Jaipur`,
- style markers such as `new designs`.

The extracted values are then validated by Pydantic and stored with provenance metadata.

## 6. Supplier matching and scoring

Supplier matching is implemented in [app/domain/matching.py](../app/domain/matching.py).

The scoring model is transparent and deterministic:

- `MatchWeights` defines weighted factors and validates total weight = 1.0.
- `rank_supplier_offers` filters active suppliers and ranks them by product, price, MOQ, location, catalogue freshness, response rate, and verification status.
- `SupplierMatch` stores each supplier's score and reasons.

A weighted score is used, but the buyer still makes the final decision. The system does not label a supplier as “best” without human choice.

The default weighting is roughly:

- product match: 0.30
- price match: 0.20
- MOQ match: 0.15
- location match: 0.10
- catalogue freshness: 0.10
- supplier response rate: 0.10
- supplier verification: 0.05

The actual `DatabaseSupplierSearcher` in [app/agent/runner.py](../app/agent/runner.py) builds offers from the database by joining `CatalogueItem`, `Supplier`, and `Catalogue` tables and then calls the ranking logic.

## 7. LangGraph workflows

The core workflow graph is built in [app/agent/graph.py](../app/agent/graph.py). It uses `StateGraph` and a typed state defined in [app/agent/state.py](../app/agent/state.py).

### 7.1 Procurement state

`ProcurementState` contains:

- request and buyer IDs,
- raw message and media URLs,
- buyer city/state,
- requirement payload,
- missing fields,
- clarification question,
- supplier candidates and selected suppliers,
- requirement version and RFQ IDs,
- RFQ send status,
- errors and status flags.

This keeps graph state focused on workflow progression rather than dragging in full catalogue records.

### 7.2 Nodes and transitions

The graph has these primary node responsibilities:

1. `receive_request`
   - validates that a request ID exists and sets initial state.

2. `extract_requirement`
   - invokes the extractor over raw message and media URLs.

3. `resolve_and_check_missing`
   - merges profile destination data,
   - checks for missing essential fields,
   - decides whether the request is ready for matching or needs clarification.

4. `ask_buyer_clarification`
   - asks only for the first missing critical field.

5. `search_suppliers`
   - calls the supplier searcher with the requirement.

6. `select_suppliers`
   - interrupts for a human supplier-selection approval.

7. `approve_rfq`
   - interrupts for RFQ approval before supplier outreach.

8. `send_supplier_rfqs`
   - iterates through selected suppliers,
   - sends a store-backed idempotent RFQ for each supplier,
   - records successes and failures.

9. `wait_for_supplier_responses`
   - transitions the request to a waiting status if outbound RFQs were sent.

The graph uses conditional routing to decide whether to continue matching or pause for clarification.

### 7.3 Approval-resume semantics

The design supports human interrupts using `langgraph.types.interrupt`. For example:

- supplier selection interrupt,
- RFQ approval interrupt,
- negotiation approval interrupt,
- order confirmation interrupt.

The approval records are stored in `Approval` rows in [app/domain/models.py](../app/domain/models.py), and operator decisions are exposed through [app/api/routes/approvals.py](../app/api/routes/approvals.py). The route saves the decision and enqueues a `resume_graph` job to continue execution.

## 8. Decision graph for negotiation and order confirmation

The negotiation and order-confirmation flow is handled separately in [app/agent/decision_graph.py](../app/agent/decision_graph.py).

This graph is intentionally small and scoped to the approval path:

- `approve_negotiation`: pauses for a human to approve a negotiated price before sending any message.
- `send_negotiation`: sends the supplier negotiation if approved.
- `approve_order`: pauses again for an explicit order-confirmation approval.

This satisfies the product requirement that negotiation and order confirmation are not left as implicit or fully automatic actions.

## 9. Durable job queue and worker execution

The durable work queue is implemented in [app/infrastructure/db/jobs.py](../app/infrastructure/db/jobs.py) and executed by [app/workers/runner.py](../app/workers/runner.py).

### 9.1 Job model and semantics

The `Job` table includes:

- `job_type`,
- immutable `idempotency_key`,
- `payload`,
- `run_after`,
- `lease_until`,
- `attempt_count`,
- `max_attempts`,
- `status`.

This enables safe retries and prevents duplicate work when the same event is retried by the HTTP layer or a worker restart.

### 9.2 Claim and retry behavior

The queue uses `claim_next_job`:

- selects queued or retryable jobs that are due,
- marks them `RUNNING`,
- assigns a lease window,
- handles worker crash recovery by allowing a previously leased job to become re-claimable when the lease expires.

This aligns with the requirement that no HTTP request blocks while waiting for supplier responses.

### 9.3 Worker cycle

`DurableWorker.run_once` claims a job and invokes the `ProcurementJobProcessor`.

If processing succeeds, the worker marks the job `SUCCEEDED`; if it fails with a permanent error, it marks it `DEAD`; otherwise it schedules retry with exponential backoff.

This makes the system restart-safe and resilient to transient provider failure.

## 10. Job processor and orchestration flow

The central orchestration logic lives in [app/agent/runner.py](../app/agent/runner.py).

`ProcurementJobProcessor` supports the following job types:

- `process_buyer_message`
- `decision_workflow`
- `parse_supplier_response`
- `finalize_rfq`
- `process_request`
- `resume_graph`

### 10.1 Initial request processing

For `process_request`, the processor:

- resolves the request ID,
- builds the initial graph state,
- invokes the procurement graph,
- stores any interrupt payloads for later operator action.

### 10.2 Resumption flow

If a job is `resume_graph`, the processor loads the relevant approval information, finds the thread ID, and replays the graph with the decision payload. This allows the workflow to continue after a human decision.

### 10.3 Negotiation and order confirmation

When the approval type calls for negotiation or order confirmation, the processor uses `build_decision_graph` rather than the procurement graph.

This ensures that seller-decided actions are separated cleanly from the intake/matching flow.

## 11. Buyer and supplier messaging providers

Messaging is abstracted in [app/infrastructure/messaging/protocols.py](../app/infrastructure/messaging/protocols.py) and implemented by:

- [mock.py](../app/infrastructure/messaging/mock.py): deterministic demo messaging,
- [whatsapp_cloud.py](../app/infrastructure/messaging/whatsapp_cloud.py): real WhatsApp Cloud API client.

### 11.1 Mock provider

The mock provider stores outbound messages in memory and suppresses duplicate sends by idempotency key. It is the default for local demo operation when no real WhatsApp credentials are configured.

### 11.2 WhatsApp Cloud provider

The Cloud provider implements:

- `send_text`,
- `send_media`,
- `get_media` (to fetch media metadata and bytes),
- request signing/headers for the Facebook Graph API,
- error wrapping around API failures.

This keeps the application provider-independent while still supporting a real WhatsApp channel later.

## 12. Status model and progress lifecycle

The available request and workflow statuses are defined in [app/domain/statuses.py](../app/domain/statuses.py).

Key statuses:

- `NEW`, `EXTRACTING`, `NEEDS_CLARIFICATION`, `MATCHING`, `AWAITING_SUPPLIER_SELECTION`, `AWAITING_RFQ_APPROVAL`, `RFQ_SENT`, `WAITING_FOR_QUOTES`, `PROCESSING_QUOTES`, `QUOTES_READY`, `AWAITING_BUYER_DECISION`, `NEGOTIATING`, `AWAITING_ORDER_APPROVAL`, `ORDER_CONFIRMED`, `COMPLETED`, `CANCELLED`, `FAILED`.

These are explicit workflow states; the implementation does not infer state solely from chat text. This is required for traceability and restart safety.

## 13. Security, data integrity, and assumptions

The repository is intentionally designed around a few safety rules:

- inbound WhatsApp messages are validated and deduplicated,
- all outbound sends are idempotent with unique keys,
- approval decisions are persisted before resumption,
- supplier and catalogue data are treated as untrusted content,
- only explicit functions are called by the workflow layer,
- the LLM/provider layer does not open arbitrary database access or messaging actions.

The system also uses a strict requirement schema to keep buyer and supplier facts separate from AI-inference metadata. This prevents commercial hallucination from being treated as a fact.

## 14. Frontend/admin layer

The frontend is a Next.js application under [frontend](../frontend). It provides a minimal admin UI around the FastAPI endpoints. The expected pages are:

- Requests dashboard,
- request detail view,
- supplier view,
- catalogues view,
- RFQ tracking,
- approvals queue.

This is deliberately thin and serves as an operator dashboard rather than a buyer app.

## 15. Request lifecycle example

A typical buyer request follows this path:

1. Buyer message arrives at WhatsApp webhook.
2. The message is validated and ingested into the database.
3. A `process_request` job is queued.
4. The worker starts the LangGraph workflow.
5. The extractor parses requirement fields from the message.
6. Missing fields are checked; the graph may ask the buyer for clarification.
7. Supplier search applies structured filters and deterministic ranking.
8. The graph interrupts for supplier selection.
9. Operator approves the supplier set.
10. Another interrupt asks for RFQ approval.
11. The RFQ is created and sent idempotently to each selected supplier.
12. Supplier replies are persisted and later parsed into quote records.
13. Comparison results are generated and presented back to the buyer.
14. Buyer selection or negotiation uses a distinct approval step.
15. Final order confirmation requires a human review step.

## 16. What is already implemented versus what remains conceptual

The current codebase has a strong MVP structure and several concrete implementations:

Implemented in the repository:

- FastAPI bootstrap and router registration.
- requirement schema and match scoring.
- LangGraph approving/resumable workflow skeleton.
- job queue with durable leases and retries.
- supplier and catalogue model schema.
- webhook validation and deduplication.
- mock messaging and provider abstraction.
- admin request and approval routes.

Conceptual or to be expanded in later phases:

- richer quote parsing beyond rule-based extraction,
- OCR/vision pipeline for uploaded images,
- full search embedding strategy using vector similarity,
- browser-based admin features for every workflow stage,
- full WhatsApp live integration and human approval UI polishing.

## 17. Design conclusions

This repository is already aligned with the MVP requirements in the brief:

- workflow is stateful and resumable,
- approval is treated as an actual graph interruption,
- queue and datastore are durable,
- cost-sensitive model and transport abstraction is present,
- requirement and quote data carry provenance,
- supplier matching is explicit and transparent,
- duplicate events and retries are planned for instead of being accidentally processed twice.

The overall architecture is intentionally conservative: it prioritizes correctness, observability, and human control over fully autonomous procurement actions.
