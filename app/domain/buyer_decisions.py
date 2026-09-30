import re
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class BuyerAction(StrEnum):
    SELECT = "select"
    NEGOTIATE = "negotiate"
    CONFIRM = "confirm"
    UNKNOWN = "unknown"


class BuyerIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: BuyerAction
    selection_key: str | None = None
    target_price: Decimal | None = None
    currency: str | None = None


_AMOUNT = r"(\d{1,7}(?:,\d{3})*(?:\.\d{1,2})?)"
_CURRENCY = r"(?:\u20b9|\binr\b|\brs\.?\s*)"


def parse_buyer_intent(message: str) -> BuyerIntent:
    normalized = message.strip().casefold()
    negotiation = re.search(
        rf"\bnegotiate\s+(?:supplier\s+)?([a-z]|\d{{1,2}})\s+"
        rf"(?:at|for)\s*(?:{_CURRENCY}\s*)?{_AMOUNT}\b",
        normalized,
    )
    if negotiation:
        target_text = negotiation.group(2).replace(",", "")
        prefix = normalized[: negotiation.start(2)]
        currency = "INR" if re.search(_CURRENCY, prefix) else None
        return BuyerIntent(
            action=BuyerAction.NEGOTIATE,
            selection_key=negotiation.group(1).upper(),
            target_price=Decimal(target_text),
            currency=currency,
        )

    confirmation = re.fullmatch(
        r"\s*(?:please\s+)?confirm(?:\s+(?:supplier\s+)?([a-z]|\d{1,2}))?\s*[.!]?\s*",
        normalized,
    )
    if confirmation:
        return BuyerIntent(
            action=BuyerAction.CONFIRM,
            selection_key=confirmation.group(1).upper() if confirmation.group(1) else None,
        )

    selection = re.fullmatch(
        r"\s*(?:(?:please\s+)?(?:select|choose|go\s+with)\s+)?"
        r"(?:supplier\s+)?([a-z]|\d{1,2})\s*[.!]?\s*",
        normalized,
    )
    if selection:
        return BuyerIntent(action=BuyerAction.SELECT, selection_key=selection.group(1).upper())
    return BuyerIntent(action=BuyerAction.UNKNOWN)