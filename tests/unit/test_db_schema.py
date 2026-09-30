from sqlalchemy import create_engine, inspect

from app.domain import models  # noqa: F401
from app.infrastructure.db.base import Base


def test_domain_schema_creates_with_required_uniqueness() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    assert {
        "buyers",
        "messages",
        "procurement_requests",
        "requirement_versions",
        "suppliers",
        "catalogue_items",
        "rfqs",
        "rfq_suppliers",
        "supplier_responses",
        "quotes",
        "approvals",
        "jobs",
    } <= table_names
    assert any(
        constraint["column_names"] == ["external_message_id"]
        for constraint in inspector.get_unique_constraints("messages")
    )
    assert any(
        constraint["column_names"] == ["idempotency_key"]
        for constraint in inspector.get_unique_constraints("rfq_suppliers")
    )
    assert any(
        constraint["column_names"] == ["request_id", "version"]
        for constraint in inspector.get_unique_constraints("requirement_versions")
    )
    assert any(
        constraint["column_names"] == ["interrupt_key"]
        for constraint in inspector.get_unique_constraints("approvals")
    )

    engine.dispose()