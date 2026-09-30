from decimal import Decimal

from app.domain.quotes import RuleBasedSupplierQuoteParser


def test_parses_explicit_inr_quote_without_inventing_available_stock() -> None:
    quote = RuleBasedSupplierQuoteParser().parse(
        "₹560 per piece, 100 pcs minimum, dispatch in 2 days"
    )

    assert quote.price == Decimal(560)
    assert quote.currency == "INR"
    assert quote.price_unit == "piece"
    assert quote.moq == 100
    assert quote.available_quantity is None
    assert quote.dispatch_days == 2
    assert quote.gst_included is None
    assert quote.needs_human_review is False


def test_parses_gst_and_stock_only_when_supplier_states_them() -> None:
    quote = RuleBasedSupplierQuoteParser().parse(
        "495 + GST. 200 pcs minimum. Stock: 180 pcs. Dispatch tomorrow."
    )

    assert quote.price == Decimal(495)
    assert quote.currency is None
    assert quote.gst_included is False
    assert quote.moq == 200
    assert quote.available_quantity == 180
    assert quote.dispatch_days == 1
    assert quote.needs_human_review is True


def test_unstated_commercial_fields_remain_unknown() -> None:
    quote = RuleBasedSupplierQuoteParser().parse("See attached catalogue")

    assert quote.price is None
    assert quote.currency is None
    assert quote.gst_included is None
    assert quote.moq is None
    assert quote.available_quantity is None
    assert quote.dispatch_days is None
    assert quote.needs_human_review is True


def test_inclusive_gst_is_not_confused_with_exclusive_gst() -> None:
    parser = RuleBasedSupplierQuoteParser()

    inclusive = parser.parse("INR 550 including GST per set")
    exclusive = parser.parse("Rs. 550 plus GST per set")

    assert inclusive.gst_included is True
    assert inclusive.price_unit == "set"
    assert exclusive.gst_included is False
    assert exclusive.price_unit == "set"