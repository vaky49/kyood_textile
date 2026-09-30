# Architecture

FastAPI owns authenticated operator APIs and provider webhooks. Domain models/services own lifecycle rules, deterministic supplier scoring, requirements, quotes, and provenance. LangGraph is the resumable orchestration layer; the worker consumes durable PostgreSQL jobs and resumes graph threads after persisted human decisions.

```text
WhatsApp Cloud API / mock
        -> webhook adapter -> PostgreSQL messages + requests + jobs
        -> worker -> LangGraph + PostgreSQL checkpointer
        -> supplier/catalogue services -> persisted RFQ and approval interrupt
        -> provider messaging -> supplier response webhook -> quote parser
        -> response deadline job -> comparison -> buyer notification
        -> buyer selection/negotiation -> human approval -> order approval
```

PostgreSQL is the only required stateful service. It stores application data, checkpointer state, and leased jobs. Every inbound external message, RFQ supplier send, graph interrupt, supplier response, and outbound message has an idempotency/source key. Outbound sends persist `sending` before the network call; an ambiguous attempt is held for operator review rather than automatically duplicated.

The Next.js app consumes FastAPI over CORS. Operator API access uses a local shared token in the MVP; role-specific identity is not yet implemented. Mock messaging and rule-based extraction are defaults. WhatsApp Cloud, local media retrieval, and OpenAI-compatible text/vision extraction are adapters.

Supplier text/catalogues are untrusted data. LLM extraction has no database or messaging tools. Structured outputs are validated before persistence. Buyer facts, profile facts, model inferences, and image inferences have distinct provenance; supplier quotes always link to the original response message.

## Deliberate MVP Boundaries

No payments, logistics, ERP, marketplace, supplier/buyer apps, automated financial negotiation, or autonomous order placement. PDF/image catalogues are stored for review; CSV/XLSX rows are parsed with provenance. Catalogue data is not considered active/current until an operator verifies it.