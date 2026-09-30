from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.requirements import (
    AttributeSource,
    ProductCategory,
    Requirement,
    find_missing_fields,
    with_buyer_profile_destination,
)


def test_only_essential_fields_are_required() -> None:
    requirement = Requirement(fabric="cotton", target_price_min=Decimal(500))

    assert find_missing_fields(requirement) == ["category", "quantity", "delivery_city"]


def test_buyer_profile_supplies_destination_with_provenance() -> None:
    requirement = Requirement(category=ProductCategory.LADIES_SUIT, quantity=100)

    resolved = with_buyer_profile_destination(
        requirement,
        buyer_city="Jaipur",
        buyer_state="Rajasthan",
    )

    assert resolved.delivery_city == "Jaipur"
    assert resolved.delivery_state == "Rajasthan"
    assert resolved.fact_provenance["delivery_city"].source is AttributeSource.BUYER_PROFILE
    assert find_missing_fields(resolved) == []
    assert requirement.delivery_city is None


def test_subcategory_is_not_inferred() -> None:
    requirement = Requirement(
        category=ProductCategory.LADIES_SUIT,
        quantity=100,
        delivery_city="Jaipur",
    )

    assert requirement.subcategory is None


def test_target_price_range_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        Requirement(
            target_price_min=Decimal(600),
            target_price_max=Decimal(500),
        )


def test_quantity_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Requirement(quantity=0)