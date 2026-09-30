# LangGraph Workflow

The procurement graph receives a persisted request, extracts structured requirement facts, applies buyer-profile destination only when explicit destination is absent, asks for missing category/quantity/destination, searches and scores catalogue offers, interrupts for supplier selection, interrupts again for RFQ approval, sends idempotently, and waits for durable response/deadline jobs.

The worker persists graph state with the PostgreSQL checkpointer and saves interrupt metadata as an `Approval` row containing request ID, thread ID, interrupt key, candidate/action payload, and decision payload. Operator decisions enqueue one idempotent resume job. `resume_graph` selects the graph by approval type, so RFQ, negotiation, and order confirmation interrupts resume independently.

Supplier replies are parsed into nullable quote fields with source message IDs and confidence. When all selected suppliers respond, the finalizer is expedited; otherwise it runs at the RFQ deadline. Comparison output preserves unknown currency, GST, stock, MOQ, and dispatch values. Buyer selection and negotiation are interpreted only against saved comparison keys. Negotiation and order placement each pause for a distinct human approval.

No HTTP request waits for a supplier. Queue leases and bounded retries survive worker restarts. An uncertain outbound provider send is marked for review rather than blindly retried.