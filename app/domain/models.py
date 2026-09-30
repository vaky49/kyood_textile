from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.requirements import ProductCategory
from app.domain.statuses import (
    ApprovalStatus,
    ApprovalType,
    CatalogueSourceType,
    CatalogueStatus,
    DeliveryStatus,
    JobStatus,
    MessageDirection,
    MessageType,
    NegotiationStatus,
    RequestStatus,
    RFQStatus,
    RFQSupplierStatus,
    SenderType,
    SupplierResponseStatus,
)
from app.infrastructure.db.base import Base
from app.infrastructure.db.types import enum_column


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Buyer(TimestampMixin, Base):
    __tablename__ = "buyers"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str | None] = mapped_column(String(200))
    business_name: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(32))
    whatsapp_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(100))
    preferred_categories: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    number_of_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    number_of_quotes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    number_of_orders: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("channel", "external_conversation_id"),
        CheckConstraint(
            "(buyer_id IS NULL) <> (supplier_id IS NULL)",
            name="exactly_one_conversation_party",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    buyer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("buyers.id", ondelete="CASCADE"), index=True
    )
    supplier_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str] = mapped_column(String(32), default="whatsapp", nullable=False)
    external_conversation_id: Mapped[str] = mapped_column(String(200), nullable=False)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    direction: Mapped[MessageDirection] = mapped_column(
        enum_column(MessageDirection, "message_direction"), nullable=False
    )
    sender_type: Mapped[SenderType] = mapped_column(
        enum_column(SenderType, "message_sender_type"), nullable=False
    )
    message_type: Mapped[MessageType] = mapped_column(
        enum_column(MessageType, "message_type"), nullable=False
    )
    text: Mapped[str | None] = mapped_column(Text)
    media_url: Mapped[str | None] = mapped_column(Text)
    external_media_id: Mapped[str | None] = mapped_column(String(200))
    media_object_key: Mapped[str | None] = mapped_column(String(200))
    external_message_id: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ProcurementRequest(TimestampMixin, Base):
    __tablename__ = "procurement_requests"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    buyer_id: Mapped[UUID] = mapped_column(
        ForeignKey("buyers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), index=True
    )
    source_message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), unique=True
    )
    status: Mapped[RequestStatus] = mapped_column(
        enum_column(RequestStatus, "request_status"), default=RequestStatus.NEW, nullable=False
    )
    raw_message: Mapped[str | None] = mapped_column(Text)
    comparison_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    buyer_decision: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class RequirementVersion(Base):
    __tablename__ = "requirement_versions"
    __table_args__ = (UniqueConstraint("request_id", "version"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("procurement_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    fact_provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Supplier(TimestampMixin, Base):
    __tablename__ = "suppliers"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    business_name: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(32))
    whatsapp_number: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(320))
    city: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(100), nullable=False)
    market: Mapped[str | None] = mapped_column(String(100))
    supplier_type: Mapped[str | None] = mapped_column(String(100))
    gst_number: Mapped[str | None] = mapped_column(String(32))
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    response_rate: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))


class SupplierCategory(Base):
    __tablename__ = "supplier_categories"

    supplier_id: Mapped[UUID] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), primary_key=True
    )
    category: Mapped[ProductCategory] = mapped_column(
        enum_column(ProductCategory, "product_category"), primary_key=True
    )
    subcategory: Mapped[str] = mapped_column(String(100), primary_key=True, default="")


class Catalogue(TimestampMixin, Base):
    __tablename__ = "catalogues"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    supplier_id: Mapped[UUID] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_type: Mapped[CatalogueSourceType] = mapped_column(
        enum_column(CatalogueSourceType, "catalogue_source_type"), nullable=False
    )
    source_url: Mapped[str | None] = mapped_column(Text)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[CatalogueStatus] = mapped_column(
        enum_column(CatalogueStatus, "catalogue_status"),
        default=CatalogueStatus.ACTIVE,
        nullable=False,
    )


class CatalogueItem(TimestampMixin, Base):
    __tablename__ = "catalogue_items"
    __table_args__ = (
        CheckConstraint("price IS NULL OR price >= 0", name="nonnegative_price"),
        CheckConstraint("moq IS NULL OR moq > 0", name="positive_moq"),
        CheckConstraint(
            "available_quantity IS NULL OR available_quantity >= 0",
            name="nonnegative_available_quantity",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    catalogue_id: Mapped[UUID] = mapped_column(
        ForeignKey("catalogues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    supplier_id: Mapped[UUID] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sku: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[ProductCategory] = mapped_column(
        enum_column(ProductCategory, "catalogue_item_category"), nullable=False, index=True
    )
    subcategory: Mapped[str | None] = mapped_column(String(100))
    fabric: Mapped[str | None] = mapped_column(String(100), index=True)
    color: Mapped[str | None] = mapped_column(String(100))
    style: Mapped[str | None] = mapped_column(String(200))
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    moq: Mapped[int | None] = mapped_column(Integer)
    stock_status: Mapped[str | None] = mapped_column(String(50))
    available_quantity: Mapped[int | None] = mapped_column(Integer)
    image_url: Mapped[str | None] = mapped_column(Text)
    item_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)
    fact_provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RFQ(TimestampMixin, Base):
    __tablename__ = "rfqs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("procurement_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    buyer_id: Mapped[UUID] = mapped_column(
        ForeignKey("buyers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[RFQStatus] = mapped_column(
        enum_column(RFQStatus, "rfq_status"), default=RFQStatus.DRAFT, nullable=False
    )
    requirement_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RFQSupplier(Base):
    __tablename__ = "rfq_suppliers"
    __table_args__ = (UniqueConstraint("rfq_id", "supplier_id"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    rfq_id: Mapped[UUID] = mapped_column(
        ForeignKey("rfqs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    supplier_id: Mapped[UUID] = mapped_column(
        ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[RFQSupplierStatus] = mapped_column(
        enum_column(RFQSupplierStatus, "rfq_supplier_status"),
        default=RFQSupplierStatus.PENDING,
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SupplierResponse(Base):
    __tablename__ = "supplier_responses"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    rfq_supplier_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("rfq_suppliers.id", ondelete="CASCADE"), index=True
    )
    supplier_id: Mapped[UUID] = mapped_column(
        ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    raw_text: Mapped[str | None] = mapped_column(Text)
    parsed_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[SupplierResponseStatus] = mapped_column(
        enum_column(SupplierResponseStatus, "supplier_response_status"),
        default=SupplierResponseStatus.RECEIVED,
        nullable=False,
    )
    extraction_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Negotiation(TimestampMixin, Base):
    __tablename__ = "negotiations"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("procurement_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rfq_supplier_id: Mapped[UUID] = mapped_column(
        ForeignKey("rfq_suppliers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    buyer_message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="RESTRICT"), nullable=False
    )
    response_message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="RESTRICT"), unique=True
    )
    target_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    target_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[NegotiationStatus] = mapped_column(
        enum_column(NegotiationStatus, "negotiation_status"),
        default=NegotiationStatus.AWAITING_APPROVAL,
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    supplier_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    supplier_currency: Mapped[str | None] = mapped_column(String(3))
    supplier_gst_included: Mapped[bool | None] = mapped_column(Boolean)
    supplier_response_text: Mapped[str | None] = mapped_column(Text)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Quote(Base):
    __tablename__ = "quotes"
    __table_args__ = (
        CheckConstraint("price IS NULL OR price >= 0", name="nonnegative_price"),
        CheckConstraint("moq IS NULL OR moq > 0", name="positive_moq"),
        CheckConstraint(
            "available_quantity IS NULL OR available_quantity >= 0",
            name="nonnegative_available_quantity",
        ),
        CheckConstraint("dispatch_days IS NULL OR dispatch_days >= 0", name="nonnegative_dispatch_days"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    supplier_response_id: Mapped[UUID] = mapped_column(
        ForeignKey("supplier_responses.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    supplier_id: Mapped[UUID] = mapped_column(
        ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    price_unit: Mapped[str | None] = mapped_column(String(32))
    gst_included: Mapped[bool | None] = mapped_column(Boolean)
    moq: Mapped[int | None] = mapped_column(Integer)
    available_quantity: Mapped[int | None] = mapped_column(Integer)
    dispatch_days: Mapped[int | None] = mapped_column(Integer)
    payment_terms: Mapped[str | None] = mapped_column(Text)
    shipping_terms: Mapped[str | None] = mapped_column(Text)
    extraction_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    needs_human_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("procurement_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    approval_type: Mapped[ApprovalType] = mapped_column(
        enum_column(ApprovalType, "approval_type"), nullable=False
    )
    thread_id: Mapped[str] = mapped_column(String(200), nullable=False)
    interrupt_key: Mapped[str] = mapped_column(String(300), unique=True, nullable=False)
    status: Mapped[ApprovalStatus] = mapped_column(
        enum_column(ApprovalStatus, "approval_status"),
        default=ApprovalStatus.PENDING,
        nullable=False,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    decision_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    decided_by: Mapped[str | None] = mapped_column(String(200))
    decision_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("attempt_count >= 0", name="nonnegative_attempt_count"),
        CheckConstraint("max_attempts > 0", name="positive_max_attempts"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    job_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        enum_column(JobStatus, "job_status"), default=JobStatus.QUEUED, nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text)


class OutboundDelivery(Base):
    __tablename__ = "outbound_deliveries"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    recipient: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(
        enum_column(DeliveryStatus, "delivery_status"),
        default=DeliveryStatus.SENDING,
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))