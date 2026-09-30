import re
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class ParsedSupplierQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    price_unit: str | None = None
    gst_included: bool | None = None
    moq: int | None = Field(default=None, gt=0)
    available_quantity: int | None = Field(default=None, ge=0)
    dispatch_days: int | None = Field(default=None, ge=0)
    payment_terms: str | None = None
    shipping_terms: str | None = None
    confidence: float = Field(ge=0, le=1)
    needs_human_review: bool


class SupplierQuoteParser(Protocol):
    def parse(self, message: str) -> ParsedSupplierQuote: ...


class RuleBasedSupplierQuoteParser:
    _amount = r"(\d{1,7}(?:,\d{3})*(?:\.\d{1,2})?)"

    def parse(self, message: str) -> ParsedSupplierQuote:
        normalized = message.casefold()
        currency_match = re.search(rf"(?:\u20b9|\binr\b|\brs\.?\s*)\s*{self._amount}", normalized)
        if currency_match:
            price = _decimal(currency_match.group(1))
            currency = "INR"
        else:
            price_match = re.search(
                rf"(?:\bprice\s*(?:is|:)?|\brate\s*(?:is|:)?|\bat\s*)\s*{self._amount}"
                rf"|\b{self._amount}\s*\+\s*gst\b",
                normalized,
            )
            amount = next((value for value in price_match.groups() if value), None) if price_match else None
            price = _decimal(amount) if amount else None
            currency = None

        minimum_match = re.search(
            rf"(?:\bmoq\s*[:=]?\s*|\bminimum(?:\s+of)?\s+){self._amount}"
            r"\s*(?:pcs?|pieces?|sets?)?\s*(?:minimum|\bmin\b)?",
            normalized,
        )
        if minimum_match is None:
            minimum_match = re.search(
                rf"\b{self._amount}\s*(?:pcs?|pieces?|sets?)\s+(?:minimum|min)\b",
                normalized,
            )
        moq = int(_decimal(minimum_match.group(1))) if minimum_match else None

        stock_match = re.search(
            rf"(?:\bstock\s*[:=]?\s*|\bavailable\s*[:=]?\s*){self._amount}"
            r"\s*(?:pcs?|pieces?|sets?)?\b|"
            rf"\b{self._amount}\s*(?:pcs?|pieces?|sets?)\s+(?:available|in stock)\b",
            normalized,
        )
        stock_amount = next((value for value in stock_match.groups() if value), None) if stock_match else None
        available_quantity = int(_decimal(stock_amount)) if stock_amount else None

        if re.search(r"\b(?:inclusive of|including|incl\.?)\s+gst\b", normalized):
            gst_included: bool | None = True
        elif re.search(r"(?:\+\s*gst|plus\s+gst|excluding\s+gst|excl\.\s*gst)", normalized):
            gst_included = False
        else:
            gst_included = None

        if re.search(r"\bper\s+(?:piece|pc|unit)\b|/\s*(?:piece|pc|unit)\b", normalized):
            price_unit = "piece"
        elif re.search(r"\bper\s+set\b|/\s*set\b", normalized):
            price_unit = "set"
        else:
            price_unit = None

        dispatch_days = _parse_dispatch_days(normalized)
        known_fields = sum(
            value is not None
            for value in (
                price,
                currency,
                gst_included,
                moq,
                available_quantity,
                dispatch_days,
                price_unit,
            )
        )
        confidence = min(1.0, round(0.35 + known_fields * 0.09, 2))
        needs_human_review = price is None or currency is None or confidence < 0.65

        return ParsedSupplierQuote(
            price=price,
            currency=currency,
            price_unit=price_unit,
            gst_included=gst_included,
            moq=moq,
            available_quantity=available_quantity,
            dispatch_days=dispatch_days,
            confidence=confidence,
            needs_human_review=needs_human_review,
        )


def _decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", ""))


def _parse_dispatch_days(normalized: str) -> int | None:
    if re.search(r"\b(?:dispatch|dispatches|ship|ships)\s+(?:by\s+)?tomorrow\b", normalized):
        return 1
    match = re.search(
        r"\b(?:dispatch|ship|ready|delivery)\s+(?:in|within)\s+(\d+)\s+days?\b",
        normalized,
    )
    if match:
        return int(match.group(1))
    match = re.search(r"\bdispatch\s+(\d+)\s+days?\b", normalized)
    return int(match.group(1)) if match else None