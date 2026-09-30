from decimal import Decimal

from app.domain.matching import SupplierOffer, rank_supplier_offers
from app.domain.requirements import ProductCategory, Requirement


def test_ranks_matching_recent_offer_above_stale_offer() -> None:
    requirement = Requirement(
        category=ProductCategory.LADIES_SUIT,
        fabric="cotton",
        quantity=100,
        target_price_min=Decimal(500),
        target_price_max=Decimal(600),
        currency="INR",
    )
    offers = [
        SupplierOffer(
            supplier_id="SUP-STALE",
            catalogue_item_id="ITEM-STALE",
            category=ProductCategory.LADIES_SUIT,
            fabric="cotton",
            price=Decimal(550),
            currency="INR",
            moq=50,
            supplier_city="Surat",
            is_verified=True,
            catalogue_age_days=45,
            response_rate=0.8,
        ),
        SupplierOffer(
            supplier_id="SUP-FRESH",
            catalogue_item_id="ITEM-FRESH",
            category=ProductCategory.LADIES_SUIT,
            fabric="cotton",
            price=Decimal(550),
            currency="INR",
            moq=50,
            supplier_city="Surat",
            is_verified=True,
            catalogue_age_days=2,
            response_rate=0.8,
        ),
    ]

    matches = rank_supplier_offers(requirement, offers)

    assert [match.supplier_id for match in matches] == ["SUP-FRESH", "SUP-STALE"]
    assert matches[0].score > matches[1].score
    assert "Catalogue verified within 7 days" in matches[0].reasons
    assert "Catalogue information is stale" in matches[1].reasons


def test_excludes_inactive_and_wrong_category_offers() -> None:
    requirement = Requirement(category=ProductCategory.LADIES_SUIT)
    offers = [
        SupplierOffer(
            supplier_id="INACTIVE",
            catalogue_item_id="ITEM-1",
            category=ProductCategory.LADIES_SUIT,
            is_active=False,
        ),
        SupplierOffer(
            supplier_id="WRONG-CATEGORY",
            catalogue_item_id="ITEM-2",
            category=ProductCategory.DRESS_MATERIAL,
        ),
    ]

    assert rank_supplier_offers(requirement, offers) == []


def test_missing_price_and_moq_do_not_become_commercial_claims() -> None:
    requirement = Requirement(
        category=ProductCategory.LADIES_SUIT,
        quantity=100,
        target_price_max=Decimal(600),
        currency="INR",
    )
    offer = SupplierOffer(
        supplier_id="SUP-UNKNOWN",
        catalogue_item_id="ITEM-UNKNOWN",
        category=ProductCategory.LADIES_SUIT,
        supplier_city="Ahmedabad",
    )

    match = rank_supplier_offers(requirement, [offer])[0]

    assert "Comparable price not listed" in match.reasons
    assert "MOQ fits requested quantity" not in match.reasons
    assert "MOQ exceeds requested quantity" not in match.reasons


def test_zero_price_floor_is_a_valid_target() -> None:
    requirement = Requirement(
        category=ProductCategory.LADIES_SUIT,
        target_price_min=Decimal(0),
        currency="INR",
    )
    offer = SupplierOffer(
        supplier_id="SUP-ZERO",
        catalogue_item_id="ITEM-ZERO",
        category=ProductCategory.LADIES_SUIT,
        price=Decimal(0),
        currency="INR",
    )

    match = rank_supplier_offers(requirement, [offer])[0]

    assert "Listed price is within the target range" in match.reasons