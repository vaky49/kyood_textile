from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agent.extraction import RequirementExtractor
from app.agent.state import ProcurementState
from app.domain.matching import SupplierMatch
from app.domain.requirements import Requirement, find_missing_fields, with_buyer_profile_destination
from app.domain.statuses import RequestStatus


class SupplierSearcher(Protocol):
    async def search(self, requirement: Requirement) -> list[SupplierMatch]: ...


class SupplierMessagingProvider(Protocol):
    async def send_rfq(
        self,
        supplier_id: str,
        requirement: Requirement,
        idempotency_key: str,
    ) -> None: ...

    async def send_negotiation(
        self,
        supplier_id: str,
        target_price: str,
        currency: str,
        idempotency_key: str,
    ) -> None: ...


class SupplierMessagingError(RuntimeError):
    pass


class CompiledProcurementGraph(Protocol):
    async def ainvoke(
        self,
        input: ProcurementState | dict[str, Any],
        config: dict[str, Any],
    ) -> ProcurementState: ...


def build_procurement_graph(
    *,
    extractor: RequirementExtractor,
    searcher: SupplierSearcher | Callable[[Requirement], Awaitable[list[SupplierMatch]]],
    messaging: SupplierMessagingProvider,
    checkpointer: Any = None,
) -> Any:
    builder = StateGraph(ProcurementState)

    async def receive_request(state: ProcurementState) -> dict[str, Any]:
        if not state.get("request_id"):
            return {"status": RequestStatus.FAILED.value, "errors": ["request_id is required"]}
        return {"status": RequestStatus.EXTRACTING.value}

    async def extract_requirement(state: ProcurementState) -> dict[str, Any]:
        requirement = await extractor.extract(
            state.get("raw_message", ""),
            state.get("media_urls", []),
        )
        return {
            "requirement": requirement.model_dump(mode="json"),
            "status": RequestStatus.EXTRACTING.value,
        }

    async def resolve_and_check_missing(state: ProcurementState) -> dict[str, Any]:
        requirement = Requirement.model_validate(state["requirement"])
        requirement = with_buyer_profile_destination(
            requirement,
            buyer_city=state.get("buyer_city"),
            buyer_state=state.get("buyer_state"),
        )
        missing = find_missing_fields(requirement)
        return {
            "requirement": requirement.model_dump(mode="json"),
            "missing_fields": missing,
            "status": (
                RequestStatus.NEEDS_CLARIFICATION.value
                if missing
                else RequestStatus.MATCHING.value
            ),
        }

    async def ask_buyer_clarification(state: ProcurementState) -> dict[str, Any]:
        questions = {
            "category": "What type of suit or dress material do you need?",
            "quantity": "How many pieces do you need?",
            "delivery_city": "Which city should we deliver to?",
        }
        first_missing = state["missing_fields"][0]
        return {
            "clarification_question": questions[first_missing],
            "status": RequestStatus.NEEDS_CLARIFICATION.value,
        }

    async def search_suppliers(state: ProcurementState) -> dict[str, Any]:
        requirement = Requirement.model_validate(state["requirement"])
        matches = await _search(searcher, requirement)
        candidates = [match.model_dump(mode="json") for match in matches]
        return {
            "supplier_candidates": candidates,
            "status": (
                RequestStatus.MATCHING.value
                if candidates
                else RequestStatus.NEEDS_CLARIFICATION.value
            ),
            "clarification_question": (
                None
                if candidates
                else "I couldn't find a matching supplier. Would you like to adjust the requirement?"
            ),
        }

    async def select_suppliers(state: ProcurementState) -> dict[str, Any]:
        selection = interrupt(
            {
                "action": "select_suppliers",
                "request_id": state["request_id"],
                "candidates": state["supplier_candidates"],
            }
        )
        candidate_ids = {candidate["supplier_id"] for candidate in state["supplier_candidates"]}
        selected_ids = selection.get("selected_supplier_ids", []) if isinstance(selection, dict) else []
        if (
            not isinstance(selected_ids, list)
            or not selected_ids
            or any(
                not isinstance(supplier_id, str) or supplier_id not in candidate_ids
                for supplier_id in selected_ids
            )
        ):
            return {
                "selected_supplier_ids": [],
                "status": RequestStatus.CANCELLED.value,
                "errors": ["Supplier selection was empty or included an unknown supplier"],
            }
        return {
            "selected_supplier_ids": list(dict.fromkeys(selected_ids)),
            "status": RequestStatus.AWAITING_RFQ_APPROVAL.value,
        }

    async def approve_rfq(state: ProcurementState) -> dict[str, Any]:
        decision = interrupt(
            {
                "action": "approve_rfq",
                "request_id": state["request_id"],
                "selected_supplier_ids": state["selected_supplier_ids"],
                "requirement": state["requirement"],
            }
        )
        if not isinstance(decision, dict) or decision.get("approved") is not True:
            return {"rfq_approved": False, "status": RequestStatus.CANCELLED.value}
        return {"rfq_approved": True, "status": RequestStatus.AWAITING_RFQ_APPROVAL.value}

    async def send_supplier_rfqs(state: ProcurementState) -> dict[str, Any]:
        request_id = state["request_id"]
        version_id = state.get("requirement_version_id", "v1")
        rfq_id = state.get("rfq_id") or str(
            uuid5(NAMESPACE_URL, f"rfq:{request_id}:{version_id}")
        )
        requirement = Requirement.model_validate(state["requirement"])
        sent: list[str] = []
        failed: list[str] = []
        errors = list(state.get("errors", []))
        for supplier_id in state["selected_supplier_ids"]:
            idempotency_key = f"rfq:{rfq_id}:supplier:{supplier_id}"
            try:
                await messaging.send_rfq(supplier_id, requirement, idempotency_key)
                sent.append(supplier_id)
            except SupplierMessagingError as error:
                failed.append(supplier_id)
                errors.append(f"RFQ send failed for supplier {supplier_id}: {type(error).__name__}")
        return {
            "rfq_id": rfq_id,
            "sent_supplier_ids": sent,
            "failed_supplier_ids": failed,
            "errors": errors,
            "status": RequestStatus.RFQ_SENT.value if sent else RequestStatus.FAILED.value,
        }

    async def wait_for_supplier_responses(state: ProcurementState) -> dict[str, Any]:
        if state["sent_supplier_ids"]:
            return {"status": RequestStatus.WAITING_FOR_QUOTES.value}
        return {"status": RequestStatus.FAILED.value}

    def route_after_receive(state: ProcurementState) -> str:
        return "extract_requirement" if state["status"] != RequestStatus.FAILED.value else END

    def route_after_missing_check(state: ProcurementState) -> str:
        return "ask_buyer_clarification" if state["missing_fields"] else "search_suppliers"

    def route_after_search(state: ProcurementState) -> str:
        return "select_suppliers" if state["supplier_candidates"] else END

    def route_after_selection(state: ProcurementState) -> str:
        return "approve_rfq" if state["selected_supplier_ids"] else END

    def route_after_approval(state: ProcurementState) -> str:
        return "send_supplier_rfqs" if state.get("rfq_approved") else END

    builder.add_node("receive_request", receive_request)
    builder.add_node("extract_requirement", extract_requirement)
    builder.add_node("resolve_and_check_missing", resolve_and_check_missing)
    builder.add_node("ask_buyer_clarification", ask_buyer_clarification)
    builder.add_node("search_suppliers", search_suppliers)
    builder.add_node("select_suppliers", select_suppliers)
    builder.add_node("approve_rfq", approve_rfq)
    builder.add_node("send_supplier_rfqs", send_supplier_rfqs)
    builder.add_node("wait_for_supplier_responses", wait_for_supplier_responses)
    builder.add_edge(START, "receive_request")
    builder.add_conditional_edges("receive_request", route_after_receive)
    builder.add_edge("extract_requirement", "resolve_and_check_missing")
    builder.add_conditional_edges("resolve_and_check_missing", route_after_missing_check)
    builder.add_edge("ask_buyer_clarification", END)
    builder.add_conditional_edges("search_suppliers", route_after_search)
    builder.add_conditional_edges("select_suppliers", route_after_selection)
    builder.add_conditional_edges("approve_rfq", route_after_approval)
    builder.add_edge("send_supplier_rfqs", "wait_for_supplier_responses")
    builder.add_edge("wait_for_supplier_responses", END)
    return builder.compile(checkpointer=checkpointer)


async def _search(
    searcher: SupplierSearcher | Callable[[Requirement], Awaitable[list[SupplierMatch]]],
    requirement: Requirement,
) -> list[SupplierMatch]:
    if hasattr(searcher, "search"):
        return await searcher.search(requirement)
    return await searcher(requirement)