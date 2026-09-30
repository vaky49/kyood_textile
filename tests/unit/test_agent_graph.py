from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agent.extraction import RuleBasedRequirementExtractor
from app.agent.graph import SupplierMessagingError, build_procurement_graph
from app.domain.matching import SupplierMatch
from app.domain.statuses import RequestStatus


class FakeSearcher:
    async def search(self, requirement: Any) -> list[SupplierMatch]:
        return [
            SupplierMatch(
                supplier_id="SUP-1",
                catalogue_item_id="ITEM-1",
                score=0.9,
                reasons=["MOQ fits requested quantity"],
            ),
            SupplierMatch(
                supplier_id="SUP-2",
                catalogue_item_id="ITEM-2",
                score=0.8,
                reasons=["Price is within range"],
            ),
        ]


class FakeMessaging:
    def __init__(self, failing_supplier: str | None = None) -> None:
        self.failing_supplier = failing_supplier
        self.sent: list[tuple[str, str]] = []

    async def send_rfq(self, supplier_id: str, requirement: Any, idempotency_key: str) -> None:
        self.sent.append((supplier_id, idempotency_key))
        if supplier_id == self.failing_supplier:
            raise SupplierMessagingError("mock provider failure")


def _build_graph(messaging: FakeMessaging | None = None) -> tuple[Any, FakeMessaging]:
    provider = messaging or FakeMessaging()
    graph = build_procurement_graph(
        extractor=RuleBasedRequirementExtractor(),
        searcher=FakeSearcher(),
        messaging=provider,
        checkpointer=InMemorySaver(),
    )
    return graph, provider


@pytest.mark.asyncio
async def test_graph_pauses_for_selection_and_rfq_approval() -> None:
    graph, messaging = _build_graph()
    config = {"configurable": {"thread_id": "request-1"}}
    initial = {
        "request_id": "request-1",
        "requirement_version_id": "version-1",
        "raw_message": "Need 100 cotton ladies suits around \u20b9500-600, delivery to Jaipur",
        "media_urls": [],
    }

    selection_pause = await graph.ainvoke(initial, config)
    assert selection_pause["__interrupt__"][0].value["action"] == "select_suppliers"
    assert selection_pause["requirement"]["subcategory"] is None

    approval_pause = await graph.ainvoke(
        Command(resume={"selected_supplier_ids": ["SUP-1", "SUP-2"]}),
        config,
    )
    assert approval_pause["__interrupt__"][0].value["action"] == "approve_rfq"
    assert approval_pause["status"] == RequestStatus.AWAITING_RFQ_APPROVAL.value

    complete = await graph.ainvoke(Command(resume={"approved": True}), config)

    assert complete["status"] == RequestStatus.WAITING_FOR_QUOTES.value
    assert complete["sent_supplier_ids"] == ["SUP-1", "SUP-2"]
    assert complete["failed_supplier_ids"] == []
    assert len({key for _, key in messaging.sent}) == 2
    assert all("version-1" not in key for _, key in messaging.sent)


@pytest.mark.asyncio
async def test_graph_asks_for_first_missing_essential_field() -> None:
    graph, _ = _build_graph()

    result = await graph.ainvoke(
        {
            "request_id": "request-2",
            "raw_message": "Cotton ladies suits",
            "media_urls": [],
        },
        {"configurable": {"thread_id": "request-2"}},
    )

    assert result["status"] == RequestStatus.NEEDS_CLARIFICATION.value
    assert result["missing_fields"] == ["quantity", "delivery_city"]
    assert result["clarification_question"] == "How many pieces do you need?"
    assert result.get("supplier_candidates") is None


@pytest.mark.asyncio
async def test_graph_isolates_supplier_send_failures_and_reuses_idempotency_key() -> None:
    graph, messaging = _build_graph(FakeMessaging(failing_supplier="SUP-2"))
    config = {"configurable": {"thread_id": "request-3"}}
    initial = {
        "request_id": "request-3",
        "raw_message": "Need 100 ladies suits delivery to Jaipur",
        "media_urls": [],
    }
    await graph.ainvoke(initial, config)
    await graph.ainvoke(
        Command(resume={"selected_supplier_ids": ["SUP-1", "SUP-2"]}),
        config,
    )

    result = await graph.ainvoke(Command(resume={"approved": True}), config)
    first_keys = dict(messaging.sent)
    request_state = await graph.aget_state(config)
    assert result["status"] == RequestStatus.WAITING_FOR_QUOTES.value
    assert result["sent_supplier_ids"] == ["SUP-1"]
    assert result["failed_supplier_ids"] == ["SUP-2"]
    assert request_state.values["rfq_id"] == result["rfq_id"]
    assert first_keys["SUP-1"].endswith(":SUP-1")


@pytest.mark.asyncio
async def test_invalid_supplier_selection_is_rejected() -> None:
    graph, messaging = _build_graph()
    config = {"configurable": {"thread_id": "request-4"}}
    await graph.ainvoke(
        {
            "request_id": "request-4",
            "raw_message": "Need 100 ladies suits delivery to Jaipur",
            "media_urls": [],
        },
        config,
    )

    result = await graph.ainvoke(
        Command(resume={"selected_supplier_ids": ["SUP-NOT-A-CANDIDATE"]}),
        config,
    )

    assert result["status"] == RequestStatus.CANCELLED.value
    assert messaging.sent == []