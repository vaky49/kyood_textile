from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProductCategory(StrEnum):
    LADIES_SUIT = "ladies_suit"
    SALWAR_SUIT = "salwar_suit"
    DRESS_MATERIAL = "dress_material"


class AttributeSource(StrEnum):
    BUYER = "buyer"
    BUYER_PROFILE = "buyer_profile"
    MODEL_INFERENCE = "model_inference"
    IMAGE_INFERENCE = "image_inference"
    SUPPLIER = "supplier"
    SUPPLIER_CATALOGUE = "supplier_catalogue"


class AttributeProvenance(BaseModel):
    source: AttributeSource
    source_id: str | None = None
    confidence: Annotated[float, Field(ge=0, le=1)] | None = None


class Requirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: ProductCategory | None = None
    subcategory: str | None = None
    fabric: str | None = None
    quantity: Annotated[int, Field(gt=0)] | None = None
    target_price_min: Annotated[Decimal, Field(ge=0)] | None = None
    target_price_max: Annotated[Decimal, Field(ge=0)] | None = None
    currency: str | None = None
    style: str | None = None
    delivery_city: str | None = None
    delivery_state: str | None = None
    required_by: date | None = None
    fact_provenance: dict[str, AttributeProvenance] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_target_price_range(self) -> "Requirement":
        if (
            self.target_price_min is not None
            and self.target_price_max is not None
            and self.target_price_min > self.target_price_max
        ):
            raise ValueError("target_price_min cannot exceed target_price_max")
        return self


def with_buyer_profile_destination(
    requirement: Requirement,
    *,
    buyer_city: str | None,
    buyer_state: str | None = None,
) -> Requirement:
    if requirement.delivery_city is not None or buyer_city is None:
        return requirement

    provenance = dict(requirement.fact_provenance)
    provenance["delivery_city"] = AttributeProvenance(source=AttributeSource.BUYER_PROFILE)
    updates: dict[str, object] = {
        "delivery_city": buyer_city,
        "fact_provenance": provenance,
    }
    if requirement.delivery_state is None and buyer_state is not None:
        updates["delivery_state"] = buyer_state
        provenance["delivery_state"] = AttributeProvenance(
            source=AttributeSource.BUYER_PROFILE
        )
    return requirement.model_copy(update=updates)


def find_missing_fields(requirement: Requirement) -> list[str]:
    missing: list[str] = []
    if requirement.category is None:
        missing.append("category")
    if requirement.quantity is None:
        missing.append("quantity")
    if requirement.delivery_city is None:
        missing.append("delivery_city")
    return missing