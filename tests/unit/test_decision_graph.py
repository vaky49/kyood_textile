from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agent.decision_graph import build_decision_graph
from app.domain.statuses import RequestStatus


class FakeDecisionMessaging:
    def __init__(self) -> None:
        self.negotiations: list[tuple[str, str, str, str]] = []

    async def send_rfq(self, supplier_id: str, requirement: Any, idempotency_key: str) -> None:
        return None

    async def send_negotiation(
        self,
        supplier_id: str,
        target_price: str,
        currency: str,
        idempotency_key: str,
    ) -> None:
        self.negotiations.append((supplier_id, target_price, currency, idempotency_key))


@pytest.mark.asyncio
async def test_negotiation_interrupts_before_supplier_message() -> None:
    messaging = FakeDecisionMessaging()
    graph = build_decision_graph(messaging=messaging, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "negotiation-1"}}
    initial = {
        "request_id": "request-1",
        "action": "negotiate",
        "supplier_id": "supplier-1",
        "negotiation_id": "negotiation-1",
        "target_price": "490",
        "currency": "INR",
    }

    paused = await graph.ainvoke(initial, config)
    assert paused["__interrupt__"][0].value["action"] == "approve_negotiation"
    assert messaging.negotiations == []

    result = await graph.ainvoke(Command(resume={"approved": True}), config)

    assert result["status"] == RequestStatus.NEGOTIATING.value
    assert messaging.negotiations == [
        ("supplier-1", "490", "INR", "negotiation:negotiation-1")
    ]


@pytest.mark.asyncio
async def test_order_confirmation_requires_human_approval() -> None:
    graph = build_decision_graph(
        messaging=FakeDecisionMessaging(),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "order-1"}}
    paused = await graph.ainvoke(
        {
            "request_id": "request-2",
            "action": "order_confirmation",
            "supplier_id": "supplier-2",
        },
        config,
    )
    assert paused["__interrupt__"][0].value["action"] == "approve_order_confirmation"

    result = await graph.ainvoke(Command(resume={"approved": False}), config)

    assert result["status"] == RequestStatus.AWAITING_BUYER_DECISION.value