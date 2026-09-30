# Database

Alembic owns schema changes. Run `uv run alembic upgrade head` after setting `DATABASE_URL`; Compose runs a migration service before API and worker startup.

Core entities are `Buyer`, `Conversation`, `Message`, `ProcurementRequest`, immutable `RequirementVersion`, `Supplier`, `SupplierCategory`, `Catalogue`, `CatalogueItem`, `RFQ`, `RFQSupplier`, `SupplierResponse`, `Quote`, `Negotiation`, `Approval`, `Job`, and `OutboundDelivery`.

Commercial values remain nullable when unknown. Requirement facts retain source/provenance; extracted quotes link to the exact source message. Catalogues/items track `last_verified_at`. External message IDs, RFQ send keys, requirement version numbers, approval interrupt keys, job keys, and outbound delivery keys have uniqueness constraints for idempotency.

Local media is stored by an opaque generated key under `MEDIA_STORAGE_DIR` (Compose shares a persistent volume across API and worker). The database keeps the external Cloud media ID and local object key separately. `GET /api/media/{object_key}` requires operator authentication and does not expose storage paths.

PostgreSQL-specific queue claiming uses row locks with `SKIP LOCKED`. Tests use SQLite for fast domain/API coverage; run Compose with PostgreSQL before a production/deployment claim.