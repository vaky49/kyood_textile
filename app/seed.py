import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.models import (
    RFQ,
    Buyer,
    Catalogue,
    CatalogueItem,
    Conversation,
    Message,
    ProcurementRequest,
    Quote,
    RequirementVersion,
    RFQSupplier,
    Supplier,
    SupplierCategory,
    SupplierResponse,
)
from app.domain.requirements import AttributeSource, ProductCategory
from app.domain.statuses import (
    CatalogueSourceType,
    CatalogueStatus,
    MessageDirection,
    MessageType,
    RequestStatus,
    RFQStatus,
    RFQSupplierStatus,
    SenderType,
    SupplierResponseStatus,
)
from app.infrastructure.db.session import SessionFactory

SUPPLIER_NAMES = [
    "Surat Loom House",
    "Riverbend Suit Studio",
    "Aarohi Textile Works",
    "Mango Leaf Prints",
    "Saffron Thread Collective",
    "Blue Courtyard Fabrics",
    "Kite & Cotton Studio",
    "Monsoon Weaves",
    "Marigold Dress Materials",
    "Indigo Loom Traders",
    "Fieldnote Suit House",
    "Copperleaf Textiles",
    "Daybreak Prints",
    "Palm Court Garments",
    "Nila Weave Studio",
    "Softline Suit Makers",
    "Ahmedabad Pattern House",
    "Lattice Cotton Works",
    "Mira Fabric Collective",
    "Sundial Suit Studio",
]

BUYER_NAMES = [
    ("Maya Retail", "Jaipur Cloth House", "Jaipur", "Rajasthan"),
    ("Ishita Shah", "Pink City Fashions", "Jaipur", "Rajasthan"),
    ("Zoya Merchant", "Westside Suit Corner", "Udaipur", "Rajasthan"),
    ("Aditi Retail", "Springline Boutique", "Jodhpur", "Rajasthan"),
    ("Kavya Patel", "Everyday Suit Store", "Kota", "Rajasthan"),
]

CATEGORY_BY_INDEX = [
    ProductCategory.LADIES_SUIT,
    ProductCategory.SALWAR_SUIT,
    ProductCategory.DRESS_MATERIAL,
]
FABRICS = ["cotton", "rayon", "georgette", "silk", "cotton"]
COLORS = ["pink", "indigo", "sage", "coral", "white", "mustard", "blue"]
STYLES = ["printed", "embroidered", "daily wear", "festive", "block print"]


async def seed_database(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    async with session_factory() as session:
        suppliers = await _seed_suppliers(session)
        catalogues, item_count = await _seed_catalogues(session, suppliers)
        buyers = await _seed_buyers(session)
        demo_created = await _seed_demo_rfq(session, buyers[0], suppliers, catalogues)
        await session.commit()
        return {
            "suppliers": len(suppliers),
            "catalogue_items": item_count,
            "buyers": len(buyers),
            "demo_rfq_created": int(demo_created),
        }


async def _seed_suppliers(session: AsyncSession) -> list[Supplier]:
    suppliers: list[Supplier] = []
    for index, business_name in enumerate(SUPPLIER_NAMES):
        supplier_id = _seed_id(f"supplier:{index + 1}")
        supplier = await session.get(Supplier, supplier_id)
        if supplier is None:
            city = "Surat" if index % 2 == 0 else "Ahmedabad"
            supplier = Supplier(
                id=supplier_id,
                business_name=business_name,
                contact_name=f"Contact {index + 1}",
                whatsapp_number=f"+91000000{index + 1:04d}",
                city=city,
                state="Gujarat",
                market="Textile market",
                supplier_type="Manufacturer",
                is_verified=index % 3 != 0,
                is_active=True,
                response_rate=Decimal("0.700") + Decimal(index % 3) / Decimal(10),
            )
            session.add(supplier)
            for category in ProductCategory:
                session.add(SupplierCategory(supplier_id=supplier.id, category=category))
        suppliers.append(supplier)
    await session.flush()
    return suppliers


async def _seed_catalogues(
    session: AsyncSession,
    suppliers: list[Supplier],
) -> tuple[list[Catalogue], int]:
    now = datetime.now(UTC)
    catalogues: list[Catalogue] = []
    created_items = 0
    for supplier_index, supplier in enumerate(suppliers):
        catalogue_id = _seed_id(f"catalogue:{supplier_index + 1}")
        catalogue = await session.get(Catalogue, catalogue_id)
        if catalogue is None:
            catalogue = Catalogue(
                id=catalogue_id,
                supplier_id=supplier.id,
                source_type=CatalogueSourceType.MANUAL,
                source_url="seed:fictional-supplier-catalogue",
                last_verified_at=now - timedelta(days=supplier_index % 35),
                status=CatalogueStatus.ACTIVE,
            )
            session.add(catalogue)
            for item_index in range(5):
                global_index = supplier_index * 5 + item_index
                category = CATEGORY_BY_INDEX[global_index % len(CATEGORY_BY_INDEX)]
                fabric = FABRICS[global_index % len(FABRICS)]
                title = f"{COLORS[global_index % len(COLORS)].title()} {fabric} {category.value.replace('_', ' ')}"
                session.add(
                    CatalogueItem(
                        id=_seed_id(f"catalogue-item:{global_index + 1}"),
                        catalogue_id=catalogue.id,
                        supplier_id=supplier.id,
                        sku=f"DEMO-{global_index + 1:03d}",
                        title=title,
                        description="Fictional development catalogue item; confirm commercial terms with supplier.",
                        category=category,
                        subcategory=("3_piece_suit" if global_index % 2 == 0 else None),
                        fabric=fabric,
                        color=COLORS[global_index % len(COLORS)],
                        style=STYLES[global_index % len(STYLES)],
                        price=Decimal(450 + (global_index % 16) * 10),
                        currency="INR",
                        moq=25 + (global_index % 5) * 25,
                        stock_status="reported_in_seed_data",
                        available_quantity=100 + (global_index % 8) * 25,
                        last_verified_at=catalogue.last_verified_at,
                        fact_provenance={
                            field: {
                                "source": AttributeSource.SUPPLIER_CATALOGUE.value,
                                "source_id": str(catalogue.id),
                            }
                            for field in (
                                "title",
                                "category",
                                "fabric",
                                "color",
                                "style",
                                "price",
                                "currency",
                                "moq",
                                "available_quantity",
                            )
                        },
                    )
                )
                created_items += 1
        catalogues.append(catalogue)
    await session.flush()
    return catalogues, created_items


async def _seed_buyers(session: AsyncSession) -> list[Buyer]:
    buyers: list[Buyer] = []
    for index, (name, business, city, state) in enumerate(BUYER_NAMES):
        buyer_id = _seed_id(f"buyer:{index + 1}")
        buyer = await session.get(Buyer, buyer_id)
        if buyer is None:
            buyer = Buyer(
                id=buyer_id,
                name=name,
                business_name=business,
                whatsapp_number=f"+91111111{index + 1:04d}",
                city=city,
                state=state,
                preferred_categories=[ProductCategory.LADIES_SUIT.value],
            )
            session.add(buyer)
        buyers.append(buyer)
    await session.flush()
    return buyers


async def _seed_demo_rfq(
    session: AsyncSession,
    buyer: Buyer,
    suppliers: list[Supplier],
    catalogues: list[Catalogue],
) -> bool:
    request_id = _seed_id("request:demo-quote-ready")
    existing = await session.get(ProcurementRequest, request_id)
    if existing is not None:
        return False

    now = datetime.now(UTC)
    conversation = Conversation(
        id=_seed_id("conversation:demo-buyer"),
        buyer_id=buyer.id,
        channel="whatsapp",
        external_conversation_id=buyer.whatsapp_number,
    )
    session.add(conversation)
    await session.flush()
    source_message = Message(
        id=_seed_id("message:demo-buyer"),
        conversation_id=conversation.id,
        direction=MessageDirection.INBOUND,
        sender_type=SenderType.BUYER,
        message_type=MessageType.TEXT,
        text="Need 100 cotton ladies suits around INR 500-600, delivery Jaipur",
        external_message_id="seed-demo-buyer-message",
        created_at=now - timedelta(hours=2),
    )
    session.add(source_message)
    request = ProcurementRequest(
        id=request_id,
        buyer_id=buyer.id,
        conversation_id=conversation.id,
        raw_message=source_message.text,
        status=RequestStatus.AWAITING_BUYER_DECISION,
        comparison_payload=None,
    )
    session.add(request)
    await session.flush()
    requirement = {
        "category": ProductCategory.LADIES_SUIT.value,
        "subcategory": None,
        "fabric": "cotton",
        "quantity": 100,
        "target_price_min": "500",
        "target_price_max": "600",
        "currency": "INR",
        "delivery_city": "Jaipur",
        "delivery_state": "Rajasthan",
    }
    session.add(
        RequirementVersion(
            id=_seed_id("requirement:demo-v1"),
            request_id=request.id,
            version=1,
            payload=requirement,
            fact_provenance={
                key: {"source": "buyer"}
                for key in ("category", "fabric", "quantity", "target_price_min", "target_price_max", "delivery_city")
            },
        )
    )
    rfq = RFQ(
        id=_seed_id("rfq:demo-quote-ready"),
        request_id=request.id,
        buyer_id=buyer.id,
        status=RFQStatus.QUOTES_READY,
        requirement_snapshot=requirement,
        idempotency_key="seed:rfq:demo-quote-ready",
        sent_at=now - timedelta(hours=1),
        deadline=now + timedelta(hours=3),
    )
    session.add(rfq)
    await session.flush()
    quotes: list[dict[str, object]] = []
    for index in range(4):
        supplier = suppliers[index]
        rfq_supplier = RFQSupplier(
            id=_seed_id(f"rfq-supplier:demo:{index + 1}"),
            rfq_id=rfq.id,
            supplier_id=supplier.id,
            status=RFQSupplierStatus.RESPONDED,
            idempotency_key=f"seed:rfq:{rfq.id}:supplier:{supplier.id}",
            sent_at=rfq.sent_at,
            responded_at=now - timedelta(minutes=50 - index * 7),
        )
        session.add(rfq_supplier)
        supplier_conversation = Conversation(
            id=_seed_id(f"conversation:demo-supplier:{index + 1}"),
            supplier_id=supplier.id,
            channel="whatsapp_supplier",
            external_conversation_id=supplier.whatsapp_number or str(supplier.id),
        )
        session.add(supplier_conversation)
        await session.flush()
        price = Decimal(495 + index * 20)
        raw_text = f"INR {price} per piece, MOQ 50 pcs, dispatch in {index + 1} days"
        supplier_message = Message(
            id=_seed_id(f"message:demo-supplier:{index + 1}"),
            conversation_id=supplier_conversation.id,
            direction=MessageDirection.INBOUND,
            sender_type=SenderType.SUPPLIER,
            message_type=MessageType.TEXT,
            text=raw_text,
            external_message_id=f"seed-demo-supplier-message-{index + 1}",
            created_at=rfq_supplier.responded_at,
        )
        session.add(supplier_message)
        response = SupplierResponse(
            id=_seed_id(f"supplier-response:demo:{index + 1}"),
            rfq_supplier_id=rfq_supplier.id,
            supplier_id=supplier.id,
            source_message_id=supplier_message.id,
            raw_text=raw_text,
            parsed_payload={
                "price": str(price),
                "currency": "INR",
                "price_unit": "piece",
                "gst_included": None,
                "moq": 50,
                "available_quantity": None,
                "dispatch_days": index + 1,
            },
            status=SupplierResponseStatus.PARSED,
            extraction_confidence=Decimal("0.89"),
            received_at=rfq_supplier.responded_at,
        )
        session.add(response)
        await session.flush()
        quote = Quote(
            id=_seed_id(f"quote:demo:{index + 1}"),
            supplier_response_id=response.id,
            supplier_id=supplier.id,
            price=price,
            currency="INR",
            price_unit="piece",
            gst_included=None,
            moq=50,
            dispatch_days=index + 1,
            extraction_confidence=Decimal("0.89"),
            needs_human_review=False,
        )
        session.add(quote)
        quotes.append(
            {
                "quote_id": str(quote.id),
                "supplier_id": str(supplier.id),
                "supplier": supplier.business_name,
                "selection_key": chr(ord("A") + index),
                "price": str(price),
                "currency": "INR",
                "price_unit": "piece",
                "gst_included": None,
                "moq": 50,
                "available_quantity": None,
                "dispatch_days": index + 1,
                "needs_human_review": False,
                "source_message_id": str(supplier_message.id),
                "received_at": rfq_supplier.responded_at.isoformat(),
            }
        )
    request.comparison_payload = {
        "rfq_id": str(rfq.id),
        "quotes": quotes,
        "quote_count": len(quotes),
        "generated_at": now.isoformat(),
    }
    buyer.number_of_requests += 1
    buyer.number_of_quotes += len(quotes)
    buyer.last_request_at = now
    await session.flush()
    return True


def _seed_id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"kyood-seed:{name}")


async def main() -> None:
    counts = await seed_database(SessionFactory)
    print(
        "Seeded fictional demo data: "
        f"{counts['suppliers']} suppliers, {counts['catalogue_items']} catalogue items, "
        f"5 buyers; demo RFQ created={bool(counts['demo_rfq_created'])}"
    )


if __name__ == "__main__":
    asyncio.run(main())