from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.requirements import ProductCategory, Requirement


class SupplierOffer(BaseModel):
    model_config = ConfigDict(frozen=True)

    supplier_id: str
    catalogue_item_id: str
    category: ProductCategory
    fabric: str | None = None
    price: Annotated[Decimal, Field(ge=0)] | None = None
    currency: str | None = None
    moq: Annotated[int, Field(gt=0)] | None = None
    supplier_city: str | None = None
    is_active: bool = True
    is_verified: bool = False
    catalogue_age_days: Annotated[int, Field(ge=0)] | None = None
    response_rate: Annotated[float, Field(ge=0, le=1)] | None = None


class MatchWeights(BaseModel):
    model_config = ConfigDict(frozen=True)

    product_match: Annotated[float, Field(ge=0)] = 0.30
    price_match: Annotated[float, Field(ge=0)] = 0.20
    moq_match: Annotated[float, Field(ge=0)] = 0.15
    location_match: Annotated[float, Field(ge=0)] = 0.10
    catalogue_freshness: Annotated[float, Field(ge=0)] = 0.10
    supplier_response_rate: Annotated[float, Field(ge=0)] = 0.10
    supplier_verification: Annotated[float, Field(ge=0)] = 0.05

    @model_validator(mode="after")
    def weights_must_sum_to_one(self) -> "MatchWeights":
        if abs(sum(self.model_dump().values()) - 1.0) > 1e-9:
            raise ValueError("match weights must sum to 1")
        return self


class SupplierMatch(BaseModel):
    supplier_id: str
    catalogue_item_id: str
    score: Annotated[float, Field(ge=0, le=1)]
    reasons: list[str]


def rank_supplier_offers(
    requirement: Requirement,
    offers: list[SupplierOffer],
    *,
    weights: MatchWeights | None = None,
    preferred_supplier_cities: tuple[str, ...] = ("Surat", "Ahmedabad"),
) -> list[SupplierMatch]:
    selected_weights = weights or MatchWeights()
    preferred_cities = {city.casefold() for city in preferred_supplier_cities}
    ranked: list[SupplierMatch] = []

    for offer in offers:
        if not offer.is_active:
            continue
        if requirement.category is not None and offer.category is not requirement.category:
            continue

        factors: dict[str, float | None] = {
            "product_match": _product_score(requirement, offer),
            "price_match": _price_score(requirement, offer),
            "moq_match": _moq_score(requirement, offer),
            "location_match": _location_score(offer, preferred_cities),
            "catalogue_freshness": _freshness_score(offer),
            "supplier_response_rate": offer.response_rate,
            "supplier_verification": 1.0 if offer.is_verified else 0.0,
        }
        active_weight = sum(
            getattr(selected_weights, name)
            for name, value in factors.items()
            if value is not None
        )
        if active_weight == 0:
            continue

        score = sum(
            getattr(selected_weights, name) * value
            for name, value in factors.items()
            if value is not None
        ) / active_weight
        ranked.append(
            SupplierMatch(
                supplier_id=offer.supplier_id,
                catalogue_item_id=offer.catalogue_item_id,
                score=round(score, 4),
                reasons=_match_reasons(requirement, offer, factors),
            )
        )

    return sorted(ranked, key=lambda match: (-match.score, match.supplier_id, match.catalogue_item_id))


def _product_score(requirement: Requirement, offer: SupplierOffer) -> float | None:
    if requirement.category is None:
        return None
    if requirement.fabric is None or offer.fabric is None:
        return 1.0
    return 1.0 if requirement.fabric.casefold() == offer.fabric.casefold() else 0.0


def _price_score(requirement: Requirement, offer: SupplierOffer) -> float | None:
    has_target = requirement.target_price_min is not None or requirement.target_price_max is not None
    if not has_target or offer.price is None or offer.currency is None:
        return None
    if requirement.currency is not None and offer.currency.casefold() != requirement.currency.casefold():
        return 0.0

    lower = (
        requirement.target_price_min
        if requirement.target_price_min is not None
        else requirement.target_price_max
    )
    upper = (
        requirement.target_price_max
        if requirement.target_price_max is not None
        else requirement.target_price_min
    )
    assert lower is not None and upper is not None
    if lower <= offer.price <= upper:
        return 1.0
    distance = lower - offer.price if offer.price < lower else offer.price - upper
    scale = max(abs(lower), abs(upper), Decimal(1))
    return max(0.0, 1.0 - float(distance / scale))


def _moq_score(requirement: Requirement, offer: SupplierOffer) -> float | None:
    if requirement.quantity is None or offer.moq is None:
        return None
    return 1.0 if offer.moq <= requirement.quantity else 0.0


def _location_score(offer: SupplierOffer, preferred_cities: set[str]) -> float | None:
    if not preferred_cities or offer.supplier_city is None:
        return None
    return 1.0 if offer.supplier_city.casefold() in preferred_cities else 0.0


def _freshness_score(offer: SupplierOffer) -> float | None:
    if offer.catalogue_age_days is None:
        return None
    if offer.catalogue_age_days <= 7:
        return 1.0
    if offer.catalogue_age_days <= 30:
        return 0.5
    return 0.0


def _match_reasons(
    requirement: Requirement,
    offer: SupplierOffer,
    factors: dict[str, float | None],
) -> list[str]:
    reasons: list[str] = []
    if factors["product_match"] == 1.0:
        reasons.append("Product category matches")
    if requirement.fabric is not None:
        if offer.fabric is None:
            reasons.append("Fabric not listed; confirmation needed")
        elif factors["product_match"] == 1.0:
            reasons.append("Fabric matches")
        else:
            reasons.append("Fabric does not match")
    if factors["price_match"] == 1.0:
        reasons.append("Listed price is within the target range")
    elif requirement.target_price_min is not None or requirement.target_price_max is not None:
        if offer.price is None or offer.currency is None:
            reasons.append("Comparable price not listed")
        elif factors["price_match"] == 0.0:
            reasons.append("Listed price is outside the target or currency differs")
        else:
            reasons.append("Listed price is near the target range")
    if factors["moq_match"] == 1.0:
        reasons.append("MOQ fits requested quantity")
    elif factors["moq_match"] == 0.0:
        reasons.append("MOQ exceeds requested quantity")
    if factors["location_match"] == 1.0:
        reasons.append("Supplier is in a preferred sourcing city")
    elif factors["location_match"] == 0.0:
        reasons.append("Supplier is outside preferred sourcing cities")
    if factors["catalogue_freshness"] == 1.0:
        reasons.append("Catalogue verified within 7 days")
    elif factors["catalogue_freshness"] == 0.5:
        reasons.append("Catalogue verified within 30 days")
    elif factors["catalogue_freshness"] == 0.0:
        reasons.append("Catalogue information is stale")
    if factors["supplier_response_rate"] is not None:
        reasons.append("Supplier response history is available")
    reasons.append("Supplier is verified" if offer.is_verified else "Supplier is not verified")
    return reasons