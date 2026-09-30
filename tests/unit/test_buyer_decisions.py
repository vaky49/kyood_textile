from decimal import Decimal

from app.domain.buyer_decisions import BuyerAction, parse_buyer_intent


def test_parses_selection_and_confirmation() -> None:
    assert parse_buyer_intent("B").model_dump() == {
        "action": BuyerAction.SELECT,
        "selection_key": "B",
        "target_price": None,
        "currency": None,
    }
    assert parse_buyer_intent("Confirm B").action is BuyerAction.CONFIRM
    assert parse_buyer_intent("please choose 2").selection_key == "2"


def test_parses_negotiation_without_assuming_currency() -> None:
    intent = parse_buyer_intent("Negotiate B at 480")

    assert intent.action is BuyerAction.NEGOTIATE
    assert intent.selection_key == "B"
    assert intent.target_price == Decimal(480)
    assert intent.currency is None


def test_parses_explicit_inr_negotiation_and_rejects_other_text() -> None:
    intent = parse_buyer_intent("Negotiate supplier A at Rs. 1,250")

    assert intent.target_price == Decimal(1250)
    assert intent.currency == "INR"
    assert parse_buyer_intent("Can you show more designs?").action is BuyerAction.UNKNOWN