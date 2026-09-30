from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agent.graph import SupplierMessagingProvider
from app.domain.statuses import RequestStatus


class DecisionState(TypedDict, total=False):
    request_id: str
    action: str
    supplier_id: str
    negotiation_id: str
    target_price: str
    currency: str
    negotiation_approved: bool
    order_approved: bool
    status: str
    errors: list[str]


def build_decision_graph(
    *,
    messaging: SupplierMessagingProvider,
    checkpointer: Any = None,
) -> Any:
    builder = StateGraph(DecisionState)

    async def approve_negotiation(state: DecisionState) -> dict[str, Any]:
        decision = interrupt(
            {
                "action": "approve_negotiation",
                "request_id": state["request_id"],
                "negotiation_id": state["negotiation_id"],
                "supplier_id": state["supplier_id"],
                "target_price": state["target_price"],
                "currency": state["currency"],
            }
        )
        approved = isinstance(decision, dict) and decision.get("approved") is True
        return {
            "negotiation_approved": approved,
            "status": (
                RequestStatus.NEGOTIATING.value
                if approved
                else RequestStatus.AWAITING_BUYER_DECISION.value
            ),
        }

    async def send_negotiation(state: DecisionState) -> dict[str, Any]:
        await messaging.send_negotiation(
            supplier_id=state["supplier_id"],
            target_price=state["target_price"],
            currency=state["currency"],
            idempotency_key=f"negotiation:{state['negotiation_id']}",
        )
        return {"status": RequestStatus.NEGOTIATING.value}

    async def approve_order(state: DecisionState) -> dict[str, Any]:
        decision = interrupt(
            {
                "action": "approve_order_confirmation",
                "request_id": state["request_id"],
                "supplier_id": state["supplier_id"],
            }
        )
        approved = isinstance(decision, dict) and decision.get("approved") is True
        return {
            "order_approved": approved,
            "status": (
                RequestStatus.ORDER_CONFIRMED.value
                if approved
                else RequestStatus.AWAITING_BUYER_DECISION.value
            ),
        }

    def route_action(state: DecisionState) -> str:
        if state.get("action") == "negotiate":
            return "approve_negotiation"
        if state.get("action") == "order_confirmation":
            return "approve_order"
        return END

    def route_negotiation(state: DecisionState) -> str:
        return "send_negotiation" if state.get("negotiation_approved") else END

    builder.add_node("approve_negotiation", approve_negotiation)
    builder.add_node("send_negotiation", send_negotiation)
    builder.add_node("approve_order", approve_order)
    builder.add_conditional_edges(START, route_action)
    builder.add_conditional_edges("approve_negotiation", route_negotiation)
    builder.add_edge("send_negotiation", END)
    builder.add_edge("approve_order", END)
    return builder.compile(checkpointer=checkpointer)