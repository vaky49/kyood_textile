import re
from decimal import Decimal
from typing import Protocol

from app.domain.requirements import (
    AttributeProvenance,
    AttributeSource,
    ProductCategory,
    Requirement,
)


class RequirementExtractor(Protocol):
    async def extract(self, raw_message: str, media_urls: list[str]) -> Requirement: ...


class RuleBasedRequirementExtractor:
    async def extract(self, raw_message: str, media_urls: list[str]) -> Requirement:
        text = raw_message.strip()
        normalized = text.casefold()
        extracted: dict[str, object] = {}

        if "dress material" in normalized:
            extracted["category"] = ProductCategory.DRESS_MATERIAL
        elif "salwar suit" in normalized:
            extracted["category"] = ProductCategory.SALWAR_SUIT
        elif re.search(r"\bsuits?\b", normalized):
            extracted["category"] = ProductCategory.LADIES_SUIT

        subcategory = re.search(r"\b([23])[- ]piece\b", normalized)
        if subcategory:
            extracted["subcategory"] = f"{subcategory.group(1)}_piece_suit"

        quantity = re.search(
            r"\b(?:need|want|require|order)\s+(\d{1,7})\b",
            normalized,
        )
        if quantity is None:
            quantity = re.search(r"\b(\d{1,7})\s*(?:pcs?|suits?|sets?)\b", normalized)
        if quantity:
            extracted["quantity"] = int(quantity.group(1))

        fabric = re.search(r"\b(cotton|silk|rayon|georgette|chiffon|linen)\b", normalized)
        if fabric:
            extracted["fabric"] = fabric.group(1)

        price_range = re.search(
            r"(?:\u20b9\s*|rs\.?\s*|inr\s*)(\d+(?:\.\d+)?)\s*(?:-|to)\s*"
            r"(?:\u20b9\s*|rs\.?\s*|inr\s*)?(\d+(?:\.\d+)?)",
            normalized,
        )
        if price_range:
            extracted["target_price_min"] = Decimal(price_range.group(1))
            extracted["target_price_max"] = Decimal(price_range.group(2))
            extracted["currency"] = "INR"
        else:
            price = re.search(r"(?:\u20b9\s*|rs\.?\s*|inr\s*)(\d+(?:\.\d+)?)", normalized)
            if price:
                target = Decimal(price.group(1))
                extracted["target_price_min"] = target
                extracted["target_price_max"] = target
                extracted["currency"] = "INR"

        delivery_city = re.search(
            r"\bdelivery\s+to\s+([a-z][a-z .'-]*?)(?:[,!.?]|$)",
            normalized,
        )
        if delivery_city:
            extracted["delivery_city"] = delivery_city.group(1).strip().title()

        if re.search(r"\bnew\s+designs?\b", normalized):
            extracted["style"] = "new designs"

        provenance = {
            name: AttributeProvenance(source=AttributeSource.BUYER)
            for name in extracted
        }
        return Requirement(**extracted, fact_provenance=provenance)