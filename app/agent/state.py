from typing import Any, TypedDict


class ProcurementState(TypedDict, total=False):
    request_id: str
    buyer_id: str
    raw_message: str
    media_urls: list[str]
    buyer_city: str | None
    buyer_state: str | None
    requirement: dict[str, Any]
    missing_fields: list[str]
    clarification_question: str | None
    supplier_candidates: list[dict[str, Any]]
    selected_supplier_ids: list[str]
    requirement_version_id: str
    rfq_approved: bool
    rfq_id: str
    sent_supplier_ids: list[str]
    failed_supplier_ids: list[str]
    status: str
    errors: list[str]