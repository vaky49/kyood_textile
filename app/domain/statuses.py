from enum import StrEnum


class RequestStatus(StrEnum):
    NEW = "NEW"
    EXTRACTING = "EXTRACTING"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    READY_FOR_MATCHING = "READY_FOR_MATCHING"
    MATCHING = "MATCHING"
    AWAITING_SUPPLIER_SELECTION = "AWAITING_SUPPLIER_SELECTION"
    AWAITING_RFQ_APPROVAL = "AWAITING_RFQ_APPROVAL"
    RFQ_SENT = "RFQ_SENT"
    WAITING_FOR_QUOTES = "WAITING_FOR_QUOTES"
    PROCESSING_QUOTES = "PROCESSING_QUOTES"
    QUOTES_READY = "QUOTES_READY"
    AWAITING_BUYER_DECISION = "AWAITING_BUYER_DECISION"
    NEGOTIATING = "NEGOTIATING"
    AWAITING_ORDER_APPROVAL = "AWAITING_ORDER_APPROVAL"
    ORDER_CONFIRMED = "ORDER_CONFIRMED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class SenderType(StrEnum):
    BUYER = "buyer"
    SUPPLIER = "supplier"
    OPERATOR = "operator"
    SYSTEM = "system"


class MessageType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    DOCUMENT = "document"
    AUDIO = "audio"
    VIDEO = "video"
    LOCATION = "location"


class CatalogueSourceType(StrEnum):
    PDF = "pdf"
    IMAGE = "image"
    EXCEL = "excel"
    CSV = "csv"
    URL = "url"
    WHATSAPP = "whatsapp"
    MANUAL = "manual"


class CatalogueStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    NEEDS_REVIEW = "needs_review"


class RFQStatus(StrEnum):
    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    SENT = "sent"
    WAITING_FOR_QUOTES = "waiting_for_quotes"
    PROCESSING_QUOTES = "processing_quotes"
    QUOTES_READY = "quotes_ready"
    EXPIRED = "expired"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RFQSupplierStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    RESPONDED = "responded"
    FAILED = "failed"
    EXPIRED = "expired"


class SupplierResponseStatus(StrEnum):
    RECEIVED = "received"
    PARSED = "parsed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class NegotiationStatus(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    REJECTED = "rejected"
    SENT = "sent"
    RESPONDED = "responded"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class ApprovalType(StrEnum):
    SUPPLIER_SELECTION = "supplier_selection"
    RFQ = "rfq"
    NEGOTIATION = "negotiation"
    ORDER_CONFIRMATION = "order_confirmation"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYABLE_FAILED = "retryable_failed"
    SUCCEEDED = "succeeded"
    DEAD = "dead"


class DeliveryStatus(StrEnum):
    SENDING = "sending"
    SENT = "sent"
    NEEDS_REVIEW = "needs_review"